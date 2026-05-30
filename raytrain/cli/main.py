"""
`raytrain` top-level CLI. Subcommands are grouped here for discoverability.
"""
from __future__ import annotations

import click

from . import configure as configure_cmd
from . import submit as submit_cmd
from . import logs as logs_cmd
from . import status as status_cmd
from . import data as data_cmd
from . import exec_ as exec_cmd
from . import reproduce as reproduce_cmd


@click.group(help="Submit PyTorch DDP training jobs to KubeRay. See `raytrain <cmd> --help` for each subcommand.")
@click.version_option(package_name="raytrain")
def cli():
    pass


cli.add_command(configure_cmd.configure)
cli.add_command(submit_cmd.submit)
cli.add_command(logs_cmd.logs)
cli.add_command(status_cmd.list_jobs, name="list")
cli.add_command(status_cmd.stop)
cli.add_command(status_cmd.mlflow_open, name="mlflow")
cli.add_command(data_cmd.data)
cli.add_command(exec_cmd.exec_cmd)
cli.add_command(reproduce_cmd.reproduce)


if __name__ == "__main__":
    cli()
