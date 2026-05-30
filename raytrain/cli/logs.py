"""
`raytrain logs`: tail driver (head pod) logs. The driver streams each node's
stdout/stderr prefixed with `[node<i>]`, so this single stream is all a user
usually needs.
"""
from __future__ import annotations

import sys
import time

import click
from kubernetes import client

from ..kube import list_pods_for_rayjob, load_kube, stream_pod_logs
from ..user_config import UserConfig


@click.command(help="Tail logs of a job (defaults to head pod driver output).")
@click.argument("job_name")
@click.option("-f", "--follow", is_flag=True, help="Follow log output (like tail -f).")
@click.option("--worker", type=int, default=None,
              help="Stream a specific worker pod's ray log (0..N-1) instead of head.")
@click.option("--namespace", default=None,
              help="Override namespace; defaults to the one in user config.")
@click.option("--cluster-mode", default=None,
              type=click.Choice(["per_job", "shared"]),
              help="Override default_cluster_mode for this command.")
@click.option("--gpu-type", default="h20",
              help="(shared mode) which cluster the job runs on.")
def logs(job_name, follow, worker, namespace, cluster_mode, gpu_type):
    user_cfg = UserConfig.load()
    mode = cluster_mode or user_cfg.default_cluster_mode or "per_job"

    if mode == "shared":
        from ..platform_client import PlatformClient, PlatformError
        click.secho(f"streaming logs from {job_name} (shared)", fg="cyan")
        try:
            with PlatformClient(user_cfg.submission_server, user_cfg.token) as pc:
                for chunk in pc.stream_logs(job_name, gpu_type):
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
        except PlatformError as exc:
            raise click.ClickException(str(exc)) from exc
        except KeyboardInterrupt:
            click.echo("\n(interrupted)")
        return

    ns = namespace or user_cfg.namespace

    load_kube()
    pod = _find_pod(job_name, ns, worker)
    click.secho(f"streaming logs from {pod.metadata.name} "
                f"(phase={pod.status.phase})", fg="cyan")
    try:
        for chunk in stream_pod_logs(ns, pod.metadata.name, follow=follow):
            sys.stdout.write(chunk)
            sys.stdout.flush()
    except KeyboardInterrupt:
        click.echo("\n(interrupted)")


def _find_pod(job_name: str, namespace: str, worker: int | None):
    # retry briefly in case pod hasn't been created yet
    for _ in range(10):
        pods = list_pods_for_rayjob(job_name, namespace)
        if pods:
            break
        time.sleep(3)
    else:
        raise click.ClickException(f"no pods found for job {job_name}")

    if worker is not None:
        workers = [p for p in pods if "worker" in (p.metadata.name or "")]
        if worker >= len(workers):
            raise click.ClickException(
                f"worker index {worker} out of range (found {len(workers)})")
        return workers[worker]

    # Interactive: list all pods and let user choose
    if len(pods) == 1:
        return pods[0]

    click.echo("Available pods:")
    for i, p in enumerate(pods):
        name = p.metadata.name or ""
        if "head" in name:
            role = "head"
        elif "worker" in name:
            role = "worker"
        else:
            role = "submitter (training logs)"
        phase = p.status.phase if p.status else "?"
        click.echo(f"  [{i}] {name}  ({role}, {phase})")
    click.echo("")
    choice = click.prompt("Enter pod number", type=int, default=0)
    if choice < 0 or choice >= len(pods):
        raise click.ClickException(f"invalid choice: {choice}")
    return pods[choice]
