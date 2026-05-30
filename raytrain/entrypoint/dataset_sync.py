"""
Sync MinIO datasets into the node-local cache and symlink them into the repo's
workdir so the training code sees the expected on-disk layout.

Runs on each worker node (inside the NodeLauncher actor) once at startup.
Multiple jobs can safely sync the same dataset concurrently; we use a simple
lockfile + a "completed" sentinel so we skip if already cached.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..minio_util import download_prefix, make_client, parse_s3_uri


@dataclass
class DatasetSpec:
    name: str
    s3: str
    mount: str  # path relative to workdir where symlink is placed


def _lock_path(cache_root: Path, name: str) -> Path:
    return cache_root / ".locks" / f"{name}.lock"


def _done_path(cache_root: Path, name: str) -> Path:
    return cache_root / ".done" / f"{name}.done"


def _acquire_lock(lock_file: Path, timeout_s: int = 7200) -> int:
    """Blocking lock via O_EXCL + polling. Returns fd; caller must close."""
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout_s
    while True:
        try:
            return os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if time.time() > deadline:
                raise TimeoutError(f"could not acquire lock: {lock_file}")
            time.sleep(3)


def _release_lock(fd: int, lock_file: Path) -> None:
    try:
        os.close(fd)
    finally:
        try:
            lock_file.unlink()
        except FileNotFoundError:
            pass


def sync_datasets(
    specs: Iterable[DatasetSpec],
    workdir: str | Path,
    cache_root: str | Path = "/mnt/ray-cache/datasets",
    minio_endpoint: str | None = None,
    minio_access_key: str | None = None,
    minio_secret_key: str | None = None,
    logger=print,
) -> None:
    """Idempotent; safe to call many times per node."""
    workdir = Path(workdir)
    cache_root = Path(cache_root)
    cache_root.mkdir(parents=True, exist_ok=True)

    endpoint = minio_endpoint or os.environ.get("S3_ENDPOINT_URL") \
        or os.environ.get("AWS_ENDPOINT_URL")
    ak = minio_access_key or os.environ.get("AWS_ACCESS_KEY_ID")
    sk = minio_secret_key or os.environ.get("AWS_SECRET_ACCESS_KEY")
    if not (endpoint and ak and sk):
        raise RuntimeError(
            "MinIO credentials not present in env; cannot sync datasets.")

    cli = make_client(endpoint, ak, sk)

    for spec in specs:
        bucket, prefix = parse_s3_uri(spec.s3)
        dest = cache_root / spec.name
        done = _done_path(cache_root, spec.name)
        if done.exists():
            logger(f"[dataset-sync] cache hit for {spec.name} at {dest}")
        else:
            lock = _lock_path(cache_root, spec.name)
            fd = _acquire_lock(lock)
            try:
                # re-check after acquiring lock
                if not done.exists():
                    logger(f"[dataset-sync] downloading {spec.s3} -> {dest}")
                    n = download_prefix(cli, bucket, prefix, dest,
                                        progress_cb=None, skip_existing=True)
                    logger(f"[dataset-sync] downloaded {n} new files for {spec.name}")
                    done.parent.mkdir(parents=True, exist_ok=True)
                    done.write_text(f"{spec.s3}\n")
                else:
                    logger(f"[dataset-sync] {spec.name} completed by another job")
            finally:
                _release_lock(fd, lock)

        # symlink/shadow into workdir at the requested mount path
        target = workdir / spec.mount
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.exists():
            # if it's an existing real dir and non-empty, refuse to overwrite;
            # otherwise replace the symlink to point at the fresh cache.
            if target.is_symlink():
                target.unlink()
            elif target.is_dir() and not any(target.iterdir()):
                target.rmdir()
            else:
                logger(f"[dataset-sync] note: {target} exists and is non-empty; "
                       f"leaving in place, training code will use it as-is")
                continue
        os.symlink(dest, target)
        logger(f"[dataset-sync] symlinked {target} -> {dest}")
