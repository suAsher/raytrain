"""
`raytrain data {push,pull,ls}`: convenience wrappers around MinIO.

Useful for bootstrapping: building a dataset on H20-2, uploading to MinIO,
then letting the driver sync it onto GPU workers on demand.
"""
from __future__ import annotations

from pathlib import Path

import click

from ..minio_util import (
    download_prefix,
    list_prefix,
    make_client,
    parse_s3_uri,
    upload_dir,
    ensure_bucket,
)
from ..user_config import UserConfig


def _client():
    u = UserConfig.load()
    return make_client(
        endpoint=u.minio.endpoint,
        access_key=u.minio.access_key,
        secret_key=u.minio.secret_key,
        secure=u.minio.secure,
        region=u.minio.region,
    )


@click.group(help="MinIO helpers (push / pull / list objects).")
def data():
    pass


@data.command(help="Upload a local directory to an s3:// prefix.")
@click.argument("local", type=click.Path(exists=True, file_okay=False))
@click.argument("s3_uri")
def push(local, s3_uri):
    cli = _client()
    bucket, prefix = parse_s3_uri(s3_uri)
    ensure_bucket(cli, bucket)
    n = upload_dir(cli, local, bucket, prefix,
                   progress_cb=lambda p, k: click.echo(f"  {p} -> {bucket}/{k}"))
    click.secho(f"uploaded {n} files", fg="green")


@data.command(help="Download an s3:// prefix to a local directory.")
@click.argument("s3_uri")
@click.argument("local", type=click.Path(file_okay=False))
def pull(s3_uri, local):
    cli = _client()
    bucket, prefix = parse_s3_uri(s3_uri)
    Path(local).mkdir(parents=True, exist_ok=True)
    n = download_prefix(cli, bucket, prefix, local,
                        progress_cb=lambda k, d: click.echo(f"  {bucket}/{k} -> {d}"))
    click.secho(f"downloaded {n} files", fg="green")


@data.command(help="List objects under an s3:// prefix.")
@click.argument("s3_uri")
def ls(s3_uri):
    cli = _client()
    bucket, prefix = parse_s3_uri(s3_uri)
    for k in list_prefix(cli, bucket, prefix):
        click.echo(f"s3://{bucket}/{k}")
