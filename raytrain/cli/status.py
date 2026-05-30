"""
`raytrain list`, `raytrain stop`, `raytrain mlflow`.
"""
from __future__ import annotations

from datetime import datetime

import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from ..kube import delete_rayjob, get_rayjob, list_rayjobs, load_kube
from ..user_config import UserConfig


@click.command(help="List RayJobs you submitted in the configured namespace.")
@click.option("--all-users", is_flag=True, help="Show jobs from all users.")
@click.option("--namespace", default=None)
@click.option("--cluster-mode", default=None,
              type=click.Choice(["per_job", "shared"]),
              help="Override default_cluster_mode for this command.")
@click.option("--gpu-type", default="h20",
              help="(shared mode) which cluster to query.")
def list_jobs(all_users, namespace, cluster_mode, gpu_type):
    user_cfg = UserConfig.load()
    mode = cluster_mode or user_cfg.default_cluster_mode or "per_job"

    if mode == "shared":
        _list_shared(user_cfg, gpu_type)
        return

    ns = namespace or user_cfg.namespace
    load_kube()
    items = list_rayjobs(ns, owner=None if all_users else user_cfg.user_name)

    console = Console()
    table = Table(show_header=True, header_style="bold")
    table.add_column("name"); table.add_column("owner"); table.add_column("gpu")
    table.add_column("nodes"); table.add_column("status"); table.add_column("age")
    table.add_column("run_id")
    for it in items:
        meta = it.get("metadata", {})
        labels = meta.get("labels", {}) or {}
        annos = meta.get("annotations", {}) or {}
        status = (it.get("status") or {}).get("jobStatus") \
            or (it.get("status") or {}).get("jobDeploymentStatus") or "-"
        created = meta.get("creationTimestamp", "")
        age = _age(created)
        table.add_row(
            escape("[per-job] " + meta.get("name", "?")),
            labels.get("raytrain.owner", "-"),
            labels.get("raytrain.gpu_type", "-"),
            annos.get("raytrain.num_nodes", "-"),
            str(status),
            age,
            labels.get("raytrain.run_id", "-")[:8],
        )

    # Best-effort merge: also surface shared-cluster jobs in the same view so
    # `raytrain list` shows both sources at once (the `[per-job]`/`[shared]`
    # prefixes distinguish them). This never fails the per_job listing: if the
    # platform isn't configured or is unreachable, we simply show nothing extra.
    for it in _try_list_shared(user_cfg, gpu_type):
        meta = it.get("metadata", {}) or {}
        table.add_row(
            escape("[shared] " + it.get("submission_id", "?")),
            meta.get("raytrain.user", "-"),
            gpu_type,
            "-",
            it.get("status", "-"),
            "-",
            "-",
        )
    console.print(table)


def _try_list_shared(user_cfg, gpu_type: str) -> list[dict]:
    """Best-effort shared-cluster listing for the merged per_job view.

    Returns shared jobs when a platform endpoint + token are configured and the
    server is reachable; otherwise returns an empty list. Never raises so the
    primary per_job listing is never broken by shared-side problems.
    """
    if not getattr(user_cfg, "submission_server", "") or \
            not getattr(user_cfg, "token", ""):
        return []
    try:
        from ..platform_client import PlatformClient
        with PlatformClient(user_cfg.submission_server, user_cfg.token) as pc:
            return pc.list_jobs(gpu_type)
    except Exception:
        return []


def _list_shared(user_cfg, gpu_type: str) -> None:
    """List jobs from the Platform API (shared cluster_mode)."""
    from ..platform_client import PlatformClient, PlatformError

    if not user_cfg.submission_server or not user_cfg.token:
        raise click.ClickException(
            "shared mode requires submission_server + token in "
            "~/.raytrain/config.yaml"
        )
    try:
        with PlatformClient(user_cfg.submission_server, user_cfg.token) as pc:
            items = pc.list_jobs(gpu_type)
    except PlatformError as exc:
        raise click.ClickException(str(exc)) from exc

    console = Console()
    table = Table(show_header=True, header_style="bold")
    table.add_column("submission_id"); table.add_column("owner")
    table.add_column("status")
    for it in items:
        meta = it.get("metadata", {}) or {}
        table.add_row(
            escape("[shared] " + it.get("submission_id", "?")),
            meta.get("raytrain.user", "-"),
            it.get("status", "-"),
        )
    console.print(table)


def _age(iso_ts: str) -> str:
    if not iso_ts:
        return "-"
    try:
        t = datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return iso_ts
    delta = datetime.utcnow() - t
    s = int(delta.total_seconds())
    if s < 60: return f"{s}s"
    if s < 3600: return f"{s // 60}m"
    if s < 86400: return f"{s // 3600}h"
    return f"{s // 86400}d"


@click.command(help="Stop a RayJob (also tears down its RayCluster).")
@click.argument("job_name")
@click.option("--namespace", default=None)
@click.option("--yes", is_flag=True, help="Skip confirmation prompt.")
@click.option("--cluster-mode", default=None,
              type=click.Choice(["per_job", "shared"]),
              help="Override default_cluster_mode for this command.")
@click.option("--gpu-type", default="h20",
              help="(shared mode) which cluster the job runs on.")
def stop(job_name, namespace, yes, cluster_mode, gpu_type):
    user_cfg = UserConfig.load()
    mode = cluster_mode or user_cfg.default_cluster_mode or "per_job"

    if mode == "shared":
        from ..platform_client import PlatformClient, PlatformError
        if not yes:
            click.confirm(f"stop submission {job_name}?", abort=True)
        try:
            with PlatformClient(user_cfg.submission_server, user_cfg.token) as pc:
                pc.stop_job(job_name, gpu_type)
        except PlatformError as exc:
            raise click.ClickException(str(exc)) from exc
        click.secho(f"stopped {job_name}", fg="green")
        return

    ns = namespace or user_cfg.namespace
    load_kube()
    if not yes:
        click.confirm(f"delete RayJob {job_name} in {ns}?", abort=True)
    delete_rayjob(job_name, ns)
    click.secho(f"deleted {job_name}", fg="green")


@click.command(help="Print (or open) the MLflow UI URL for a job or run.")
@click.argument("job_or_run", required=False)
@click.option("--namespace", default=None)
@click.option("--open", "open_browser", is_flag=True,
              help="Try to open in a browser via xdg-open / open.")
def mlflow_open(job_or_run, namespace, open_browser):
    user_cfg = UserConfig.load()
    base = user_cfg.mlflow.tracking_uri.rstrip("/")
    if not job_or_run:
        click.echo(base)
        return

    # heuristic: MLflow run_ids are 32-hex; otherwise treat as RayJob name
    url = base
    if len(job_or_run) == 32 and all(c in "0123456789abcdef" for c in job_or_run):
        url = f"{base}/#/experiments/0/runs/{job_or_run}"
    else:
        ns = namespace or user_cfg.namespace
        load_kube()
        try:
            obj = get_rayjob(job_or_run, ns)
            run_id = (obj.get("metadata", {}).get("labels", {}) or {}).get(
                "raytrain.run_id", "")
            if run_id:
                url = f"{base}/#/experiments/0/runs/{run_id}"
        except Exception as e:
            click.echo(f"(could not resolve job → run_id: {e})")

    click.echo(url)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
