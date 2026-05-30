"""
code_sync —— 把当前目录打包上传 MinIO，供 Ray ``runtime_env.working_dir`` 拉取。

主要职责：
    1. 把 workdir 打包成 zip（按默认 + ``.raytrainignore`` + 调用方指定的额外
       排除规则）。
    2. 计算 SHA256 指纹（用于 dedup 与 MLflow 审计）。
    3. 上传到 MinIO（带 5xx / 网络错误的 3 次指数退避重试，4xx 直接抛）。
    4. 提供 200 MiB 上限保护，超限时给出 top-10 大文件提示。

设计要点：
    - 不引入 docker。
    - 不依赖任何 K8s API。
    - 调用方只需要构造好 minio_client，本模块负责所有 zip / hash / upload。
    - 异常被分类成 :class:`CodeSyncError` 子类，CLI 可分别给出提示。
"""
from __future__ import annotations

import hashlib
import io
import os
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

try:
    import pathspec  # type: ignore
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "raytrain.code_sync requires `pathspec`. "
        "Run `pip install -e .` to pick up the new dependency."
    ) from exc

from minio import Minio
from minio.error import S3Error


# 上限：单个 code zip 不超过 200 MiB。超过往往说明用户把数据 / checkpoint /
# .git 大文件打进来了，应该让用户用 .raytrainignore 把它排除掉，而不是上传。
DEFAULT_MAX_SIZE_BYTES = 200 * 1024 * 1024

# 默认 code zip 排除规则。和 .gitignore 同语法。
# 经验性的"几乎不会进 code zip 的东西"。
DEFAULT_IGNORES: tuple[str, ...] = (
    # vcs
    ".git/",
    ".hg/",
    ".svn/",
    # python
    "__pycache__/",
    "*.pyc",
    "*.pyo",
    "*.pyd",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".tox/",
    "*.egg-info/",
    "build/",
    "dist/",
    # virtualenv
    ".venv/",
    "venv/",
    "env/",
    # IDE
    ".idea/",
    ".vscode/",
    # node
    "node_modules/",
    # data / checkpoints / experiment outputs（这些都不应该进 code zip）
    "data/",
    "datasets/",
    "exp/",
    "outputs/",
    "logs/",
    "wandb/",
    "*.ckpt",
    "*.pth",
    "*.pt",
    "*.tar",
    "*.safetensors",
    # 临时 / OS
    ".DS_Store",
    "Thumbs.db",
    "*.swp",
    # raytrain 自身的临时
    ".raytrain-cache/",
)

# 默认 zip key 模板：raytrain-code/<user>/<job>.zip
DEFAULT_OBJECT_KEY_TEMPLATE = "{user}/{job_name}.zip"
DEFAULT_BUCKET = "raytrain-code"

# 上传重试相关
_RETRY_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_BASE = 2.0  # 第 N 次重试 sleep 2 ** N 秒（2 / 4 / 8）


# ---------------------------------------------------------------------------- #
# Errors
# ---------------------------------------------------------------------------- #


class CodeSyncError(RuntimeError):
    """code_sync 顶层错误，CLI 可以统一兜住。"""


class CodeSyncTooLargeError(CodeSyncError):
    """zip 文件超出 ``max_size_bytes`` 上限。"""

    def __init__(
        self,
        size_bytes: int,
        limit_bytes: int,
        top_files: list[tuple[Path, int]],
    ) -> None:
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes
        self.top_files = top_files
        msg = self._format_message()
        super().__init__(msg)

    def _format_message(self) -> str:
        size_mib = self.size_bytes / 1024 / 1024
        limit_mib = self.limit_bytes / 1024 / 1024
        lines = [
            f"code zip is {size_mib:.1f} MiB, exceeds limit {limit_mib:.0f} MiB.",
            "",
            "top-10 largest files in the bundle (consider adding to .raytrainignore):",
        ]
        for p, sz in self.top_files:
            lines.append(f"  {sz / 1024 / 1024:7.2f} MiB  {p}")
        return "\n".join(lines)


class CodeSyncUploadError(CodeSyncError):
    """上传 MinIO 失败（5xx 重试耗尽 / 4xx 直接失败）。"""


# ---------------------------------------------------------------------------- #
# Result
# ---------------------------------------------------------------------------- #


@dataclass
class CodeBundle:
    """
    `build_code_zip` 的产物：本地临时 zip 路径 + 元数据。

    上传后由 `upload_code_zip` 填上 ``s3_uri``。
    """

    zip_path: Path
    sha256: str
    size_bytes: int
    file_count: int
    s3_uri: str | None = None
    excluded_top_files: list[tuple[Path, int]] = field(default_factory=list)

    @property
    def size_mib(self) -> float:
        return self.size_bytes / 1024 / 1024


# ---------------------------------------------------------------------------- #
# Public API
# ---------------------------------------------------------------------------- #


def load_pathspec(
    workdir: Path,
    extra_excludes: Iterable[str] | None = None,
) -> "pathspec.PathSpec":
    """
    构建当前 workdir 的排除规则。优先级（后写覆盖前写）：

        1. DEFAULT_IGNORES（内置）
        2. workdir/.gitignore（如果存在）
        3. workdir/.raytrainignore（如果存在）
        4. extra_excludes（调用方传入）

    返回一个 PathSpec 对象，可以用 ``.match_file(rel_path)`` 判断。
    """
    patterns: list[str] = list(DEFAULT_IGNORES)

    gitignore = workdir / ".gitignore"
    if gitignore.is_file():
        patterns.extend(_read_pattern_file(gitignore))

    rtignore = workdir / ".raytrainignore"
    if rtignore.is_file():
        patterns.extend(_read_pattern_file(rtignore))

    if extra_excludes:
        patterns.extend(extra_excludes)

    return pathspec.PathSpec.from_lines("gitignore", patterns)


def build_code_zip(
    workdir: str | os.PathLike[str],
    job_name: str,
    user: str,
    extra_excludes: Iterable[str] | None = None,
    max_size_bytes: int = DEFAULT_MAX_SIZE_BYTES,
    out_dir: str | os.PathLike[str] | None = None,
) -> CodeBundle:
    """
    把 ``workdir`` 打包成 zip，按默认 + ``.gitignore`` + ``.raytrainignore``
    + ``extra_excludes`` 排除文件。

    Parameters
    ----------
    workdir
        要打包的项目根目录。
    job_name
        生成的 zip 文件名前缀，最终命名 ``raytrain-code-{job_name}.zip``。
    user
        提交者用户名，仅作为生成 zip 内的元数据，不影响打包内容本身。
    extra_excludes
        额外排除模式（gitignore 风格）。
    max_size_bytes
        zip 大小上限。超出抛 :class:`CodeSyncTooLargeError`。
    out_dir
        zip 输出目录，默认 ``$TMPDIR``。

    Returns
    -------
    CodeBundle
        本地 zip 路径、sha256、大小等。

    Raises
    ------
    CodeSyncTooLargeError
        zip 大小超出上限时。
    FileNotFoundError
        ``workdir`` 不存在时。
    """
    wd = Path(workdir).expanduser().resolve()
    if not wd.is_dir():
        raise FileNotFoundError(f"workdir does not exist or is not a directory: {wd}")

    spec = load_pathspec(wd, extra_excludes=extra_excludes)

    # 收集要打包的文件 + 体积，方便超限时给 top-10 列表
    file_sizes: list[tuple[Path, int]] = []
    for p in wd.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(wd)
        rel_str = rel.as_posix()
        # PathSpec 的 match_file 对目录要带 trailing slash
        if spec.match_file(rel_str):
            continue
        # 处理符号链接：默认跟随到真实文件，但循环 / 失效软链直接跳过
        try:
            sz = p.stat().st_size
        except OSError:
            continue
        file_sizes.append((rel, sz))

    file_sizes.sort(key=lambda t: t[1], reverse=True)

    if out_dir is None:
        out_path = Path(_default_tmpdir()) / f"raytrain-code-{job_name}.zip"
    else:
        out_path = Path(out_dir) / f"raytrain-code-{job_name}.zip"
        out_path.parent.mkdir(parents=True, exist_ok=True)

    # 流式写入 zip + 同时计算 sha256
    hasher = hashlib.sha256()
    total_size = 0
    written = 0

    with zipfile.ZipFile(
        out_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as zf:
        # 写入一个简单的 metadata，作为 zip 的一员，便于事后排查
        meta = (
            f"raytrain.code_sync\nuser={user}\njob_name={job_name}\n"
            f"created_at={int(time.time())}\n"
        )
        zf.writestr(".raytrain-code-meta", meta)
        hasher.update(meta.encode("utf-8"))
        total_size += len(meta)

        for rel, sz in file_sizes:
            abs_p = wd / rel
            arcname = rel.as_posix()
            try:
                zf.write(abs_p, arcname=arcname)
            except OSError as exc:
                # 单个文件读不动（权限 / 软链失效），跳过但记录
                # 不阻塞整体打包
                continue
            with abs_p.open("rb") as fh:
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    hasher.update(chunk)
            total_size += sz
            written += 1

            # 早期检查：如果原始内容已经超过上限的 1.5 倍，直接放弃
            # （compression 一般压缩到 0.3-0.7，1.5x 是宽松上界）
            if total_size > max_size_bytes * 1.5:
                # 出 zip 文件后再 stat 取实际大小（压缩后）
                break

    final_size = out_path.stat().st_size
    if final_size > max_size_bytes:
        # 删掉超大 zip 防止占满 /tmp
        try:
            out_path.unlink()
        except OSError:
            pass
        raise CodeSyncTooLargeError(
            size_bytes=final_size,
            limit_bytes=max_size_bytes,
            top_files=file_sizes[:10],
        )

    return CodeBundle(
        zip_path=out_path,
        sha256=hasher.hexdigest(),
        size_bytes=final_size,
        file_count=written,
        excluded_top_files=file_sizes[:10],
    )


def upload_code_zip(
    bundle: CodeBundle,
    minio_client: Minio,
    bucket: str = DEFAULT_BUCKET,
    object_key: str | None = None,
    user: str | None = None,
    job_name: str | None = None,
    dedup: bool = False,
) -> str:
    """
    把 :class:`CodeBundle` 上传到 MinIO，返回 ``s3://<bucket>/<key>``。

    Parameters
    ----------
    bundle
        :func:`build_code_zip` 返回的对象。
    minio_client
        已配置好的 minio 客户端。
    bucket
        目标 bucket。默认 ``raytrain-code``，假定 admin 已经预创建并配好
        7 天 lifecycle policy（详见 ``deploy/setup-code-bucket.sh``）。
    object_key
        显式指定对象 key；不指定时由 ``user`` + ``job_name`` 拼。
    user, job_name
        在 ``object_key=None`` 时用来拼默认 key
        ``{user}/{job_name}.zip``。
    dedup
        启用按 sha256 去重。会先 HEAD ``_blobs/{sha256}.zip``，命中则跳过 PUT。

    Returns
    -------
    str
        最终生效的 ``s3://...`` URI（也已经写入 ``bundle.s3_uri``）。

    Raises
    ------
    CodeSyncUploadError
        上传失败（5xx 重试耗尽 / 4xx 直接失败）。
    """
    if object_key is None:
        if not user or not job_name:
            raise ValueError(
                "either object_key or both (user, job_name) must be provided"
            )
        object_key = DEFAULT_OBJECT_KEY_TEMPLATE.format(user=user, job_name=job_name)

    # dedup：先看 _blobs/<sha>.zip 是否存在
    if dedup:
        blob_key = f"_blobs/{bundle.sha256}.zip"
        if _object_exists(minio_client, bucket, blob_key):
            uri = f"s3://{bucket}/{blob_key}"
            bundle.s3_uri = uri
            return uri
        # 不存在则继续上传到 blob_key（而不是 user/job_name 路径）
        object_key = blob_key

    last_exc: Exception | None = None
    for attempt in range(_RETRY_MAX_ATTEMPTS):
        try:
            minio_client.fput_object(
                bucket_name=bucket,
                object_name=object_key,
                file_path=str(bundle.zip_path),
                content_type="application/zip",
                metadata={
                    "x-amz-meta-raytrain-sha256": bundle.sha256,
                    "x-amz-meta-raytrain-size": str(bundle.size_bytes),
                },
            )
            uri = f"s3://{bucket}/{object_key}"
            bundle.s3_uri = uri
            return uri
        except S3Error as exc:
            # 4xx 直接报错，不重试（凭据错 / bucket 不存在）
            code = getattr(exc, "code", None)
            if code in {
                "NoSuchBucket",
                "AccessDenied",
                "InvalidAccessKeyId",
                "SignatureDoesNotMatch",
            }:
                raise CodeSyncUploadError(
                    f"upload rejected by MinIO ({code}): {exc}"
                ) from exc
            last_exc = exc
        except Exception as exc:  # 网络层错误
            last_exc = exc

        if attempt < _RETRY_MAX_ATTEMPTS - 1:
            sleep_s = _RETRY_BACKOFF_BASE ** (attempt + 1)
            time.sleep(sleep_s)

    # 重试耗尽
    raise CodeSyncUploadError(
        f"upload failed after {_RETRY_MAX_ATTEMPTS} attempts: {last_exc}"
    ) from last_exc


# ---------------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------------- #


def _read_pattern_file(path: Path) -> list[str]:
    """读 .gitignore / .raytrainignore，跳过空行和注释。"""
    out: list[str] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            out.append(line)
    except OSError:
        # 读不到就当没这文件
        return []
    return out


def _default_tmpdir() -> str:
    """优先用 ``RAYTRAIN_TMPDIR``，其次 ``TMPDIR``，最后 ``/tmp``。"""
    return os.environ.get("RAYTRAIN_TMPDIR") or os.environ.get("TMPDIR") or "/tmp"


def _object_exists(client: Minio, bucket: str, key: str) -> bool:
    try:
        client.stat_object(bucket, key)
        return True
    except S3Error:
        return False
