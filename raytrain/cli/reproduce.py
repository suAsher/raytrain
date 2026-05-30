"""
`raytrain reproduce <mlflow_run_id>`: re-fetch the exact code bundle that was
submitted for a given MLflow run.

At submit time `raytrain submit` tags the MLflow run with code provenance
(see `raytrain/cli/submit.py`):

    raytrain.code_uri         s3://raytrain-code/<user>/<job>.zip
    raytrain.code_hash        sha256 of the code zip
    raytrain.code_size_bytes  size in bytes

This command reads those tags back, downloads the zip from MinIO into
``/tmp/raytrain-reproduce-<hash>/`` and (by default) unzips it so you can
inspect / re-run the exact code.

Lifecycle note
--------------
The ``raytrain-code`` bucket has a 7-day lifecycle (see docs/ops-guide.md §9).
For runs older than 7 days the zip has been auto-deleted, so the download will
fail with ``NoSuchKey`` / ``NoSuchBucket``. We turn that into a friendly error
that points users at git-based reproduction using the recorded code_hash.

Testability
-----------
The two external seams — fetching MLflow tags and building the MinIO client —
are module-level functions (`_get_run_tags` and `_make_minio_client`) so tests
can monkeypatch them and run hermetically without network access.
"""
from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import click

from ..minio_util import make_client as make_minio_client, parse_s3_uri
from ..mlflow_util import get_run_tags
from ..user_config import UserConfig


# tag keys written by `raytrain submit`
TAG_CODE_URI = "raytrain.code_uri"
TAG_CODE_HASH = "raytrain.code_hash"
TAG_CODE_SIZE = "raytrain.code_size_bytes"

# S3 error codes that mean "the object/bucket is gone" (lifecycle-expired or
# never existed). Treated as a friendly "expired" error.
_GONE_CODES = {"NoSuchKey", "NoSuchBucket", "NoSuchObject"}


# ---------------------------------------------------------------------------- #
# Injectable seams (monkeypatched in tests)
# ---------------------------------------------------------------------------- #


def _get_run_tags(run_id: str, user_cfg: UserConfig) -> dict[str, str]:
    """Fetch a run's tags from MLflow. Thin wrapper so tests can patch it."""
    return get_run_tags(
        run_id,
        tracking_uri=user_cfg.mlflow.tracking_uri,
        username=user_cfg.mlflow.username,
        password=user_cfg.mlflow.password,
    )


def _make_minio_client(user_cfg: UserConfig):
    """Build a MinIO client from user config. Thin wrapper so tests can patch."""
    return make_minio_client(
        endpoint=user_cfg.minio.endpoint,
        access_key=user_cfg.minio.access_key,
        secret_key=user_cfg.minio.secret_key,
        secure=user_cfg.minio.secure,
        region=user_cfg.minio.region,
    )


# ---------------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------------- #


def _dest_hash(code_hash: str | None, run_id: str) -> str:
    """Short, stable hash for the destination directory name.

    Prefer the recorded code_hash (first 12 chars); otherwise derive a short
    hash of the run_id so the path is still deterministic.
    """
    if code_hash:
        return code_hash[:12]
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:12]


def _zip_filename(key: str) -> str:
    """The zip filename to write locally, derived from the s3 object key."""
    name = Path(key).name
    return name or "code.zip"


# ---------------------------------------------------------------------------- #
# Command
# ---------------------------------------------------------------------------- #


@click.command(help="Download (and unzip) the code bundle for an MLflow run, "
                    "for exact reproduction.")
@click.argument("mlflow_run_id")
@click.option("--dest", "dest_base", default="/tmp", show_default=True,
              type=click.Path(),
              help="Base directory for the reproduce dir. The bundle lands in "
                   "<dest>/raytrain-reproduce-<hash>/.")
@click.option("--no-unzip", is_flag=True,
              help="Download the zip only; do not extract it.")
@click.option("--mlflow-uri", default=None,
              help="Override the MLflow tracking URI (defaults to user config).")
def reproduce(mlflow_run_id, dest_base, no_unzip, mlflow_uri):
    # a. Load user config.
    user_cfg = UserConfig.load()
    if mlflow_uri:
        user_cfg.mlflow.tracking_uri = mlflow_uri

    # b. Fetch the run's tags.
    click.echo(f"[1/3] fetching MLflow tags for run {mlflow_run_id}")
    tags = _get_run_tags(mlflow_run_id, user_cfg) or {}
    code_uri = (tags.get(TAG_CODE_URI) or "").strip()
    code_hash = (tags.get(TAG_CODE_HASH) or "").strip()

    if not code_uri:
        raise click.ClickException(
            f"run {mlflow_run_id} has no '{TAG_CODE_URI}' tag.\n"
            "This is expected for older runs or runs submitted with "
            "--no-code-sync (code-in-image). There is no code zip to "
            "download; reproduce from git using the run's commit instead."
        )

    # c. Compute + create the destination dir.
    short = _dest_hash(code_hash or None, mlflow_run_id)
    dest = Path(dest_base).expanduser() / f"raytrain-reproduce-{short}"
    dest.mkdir(parents=True, exist_ok=True)

    # d. Parse the code_uri and download the single zip object.
    try:
        bucket, key = parse_s3_uri(code_uri)
    except ValueError as exc:
        raise click.ClickException(
            f"run {mlflow_run_id} has an invalid '{TAG_CODE_URI}' tag "
            f"({code_uri!r}): {exc}"
        ) from exc

    zip_path = dest / _zip_filename(key)
    click.echo(f"[2/3] downloading {code_uri}")

    # Import lazily / locally so the S3Error type matches the minio client
    # built by _make_minio_client. Kept here (not module top) to avoid a hard
    # import in environments where minio isn't installed for unrelated CLI use.
    from minio.error import S3Error

    client = _make_minio_client(user_cfg)
    try:
        client.fget_object(bucket, key, str(zip_path))
    except S3Error as exc:
        # Never leave a half-written file behind.
        _cleanup(zip_path)
        code = getattr(exc, "code", None)
        if code in _GONE_CODES:
            raise click.ClickException(
                f"the code zip for run {mlflow_run_id} has expired.\n"
                f"  code_uri : {code_uri}\n"
                f"  code_hash: {code_hash or '(unknown)'}\n"
                "\n"
                "The 'raytrain-code' bucket has a 7-day lifecycle "
                "(see docs/ops-guide.md §9), so the object was auto-deleted. "
                "For runs older than 7 days, reproduce from git using the "
                "commit associated with this run; the recorded code_hash "
                f"above ({code_hash or 'n/a'}) identifies the exact bundle."
            ) from exc
        raise click.ClickException(
            f"failed to download {code_uri} ({code}): {exc}"
        ) from exc
    except Exception as exc:  # network / client errors
        _cleanup(zip_path)
        raise click.ClickException(
            f"failed to download {code_uri}: {exc}"
        ) from exc

    # e. Unzip unless --no-unzip.
    extracted = False
    if not no_unzip:
        click.echo(f"[3/3] extracting into {dest}")
        try:
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(dest)
            extracted = True
        except zipfile.BadZipFile as exc:
            raise click.ClickException(
                f"downloaded zip is corrupt ({zip_path}): {exc}"
            ) from exc
    else:
        click.echo("[3/3] skipping extraction (--no-unzip)")

    # f. Summary.
    click.echo("")
    click.secho("reproduced", fg="green", bold=True)
    click.echo(f"  run_id   : {mlflow_run_id}")
    click.echo(f"  code_uri : {code_uri}")
    click.echo(f"  code_hash: {code_hash or '(unknown)'}")
    click.echo(f"  zip      : {zip_path}")
    if extracted:
        click.echo(f"  extracted: {dest}")
    click.echo("")
    click.echo(f"  cd {dest}")


def _cleanup(path: Path) -> None:
    """Remove a (possibly half-written) file, ignoring errors."""
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass
