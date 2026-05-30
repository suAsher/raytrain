"""
`raytrain exec`: open an interactive shell inside a job's pod.
If --worker is not specified, lists all pods and prompts the user to choose.
"""
from __future__ import annotations

import os
import sys

import click

from ..kube import list_pods_for_rayjob, load_kube
from ..user_config import UserConfig


@click.command(name="exec", help="Exec into a job's pod for interactive debug (like ssh).")
@click.argument("job_name")
@click.option("--worker", type=int, default=None,
              help="Enter the Nth worker (0..N-1). If omitted, shows a list to choose from.")
@click.option("--namespace", default=None)
@click.option("--shell", default="bash", show_default=True,
              help="Shell to invoke (bash / sh / zsh).")
def exec_cmd(job_name, worker, namespace, shell):
    user_cfg = UserConfig.load()
    ns = namespace or user_cfg.namespace

    load_kube()
    pods = list_pods_for_rayjob(job_name, ns)
    if not pods:
        raise click.ClickException(f"no pods found for job {job_name}")

    if worker is not None:
        # Direct selection by index
        workers = [p for p in pods if "worker" in p.metadata.name]
        if worker >= len(workers):
            raise click.ClickException(
                f"worker index {worker} out of range (found {len(workers)} workers)")
        target = workers[worker]
    else:
        # Interactive: list all pods and let user choose
        running_pods = [p for p in pods
                        if p.status and p.status.phase == "Running"]
        if not running_pods:
            raise click.ClickException("no Running pods found for this job")

        if len(running_pods) == 1:
            target = running_pods[0]
        else:
            click.echo("Available pods:")
            for i, p in enumerate(running_pods):
                role = "head" if "head" in p.metadata.name else \
                       "worker" if "worker" in p.metadata.name else "submitter"
                node = p.spec.node_name or "?"
                click.echo(f"  [{i}] {p.metadata.name}  ({role}, node={node})")
            click.echo("")
            choice = click.prompt("Enter pod number", type=int, default=0)
            if choice < 0 or choice >= len(running_pods):
                raise click.ClickException(f"invalid choice: {choice}")
            target = running_pods[choice]

    pod_name = target.metadata.name
    click.secho(f"entering {pod_name} ({shell})...", fg="cyan")
    argv = ["kubectl", "-n", ns, "exec", "-it", pod_name, "--", shell]
    os.execvp(argv[0], argv)
