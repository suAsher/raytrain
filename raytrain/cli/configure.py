"""
`raytrain configure`: interactive one-time setup of `~/.raytrain/config.yaml`.
"""
from __future__ import annotations

import click

from ..user_config import (
    DEFAULT_MINIO_ENDPOINT,
    DEFAULT_MLFLOW_URI,
    DEFAULT_NAMESPACE,
    MinioConfig,
    MlflowConfig,
    UserConfig,
)


@click.command(help="Create or update ~/.raytrain/config.yaml (stores MinIO + MLflow credentials).")
@click.option("--user", "user_name", prompt="Your username (for job labels)")
@click.option("--namespace", default=DEFAULT_NAMESPACE, prompt=True,
              help="Kubernetes namespace for RayJobs")
@click.option("--minio-endpoint", default=DEFAULT_MINIO_ENDPOINT, prompt=True)
@click.option("--minio-access-key", prompt=True)
@click.option("--minio-secret-key", prompt=True, hide_input=True,
              confirmation_prompt=False)
@click.option("--mlflow-uri", default=DEFAULT_MLFLOW_URI, prompt=True)
@click.option("--mlflow-user", default="", prompt="MLflow username (empty ok)")
@click.option("--mlflow-password", default="", prompt="MLflow password (empty ok)",
              hide_input=True, confirmation_prompt=False)
@click.option("--cluster-mode", default="per_job",
              type=click.Choice(["per_job", "shared"]),
              prompt="Cluster mode (per_job = legacy K8s, shared = Platform API)")
@click.option("--submission-server", default="",
              prompt="Platform server URL (shared mode; empty ok)")
@click.option("--token", default="", prompt="Platform token (shared mode; empty ok)",
              hide_input=True, confirmation_prompt=False)
def configure(user_name, namespace, minio_endpoint, minio_access_key,
              minio_secret_key, mlflow_uri, mlflow_user, mlflow_password,
              cluster_mode, submission_server, token):
    cfg = UserConfig(
        user_name=user_name,
        namespace=namespace,
        minio=MinioConfig(
            endpoint=minio_endpoint,
            access_key=minio_access_key,
            secret_key=minio_secret_key,
        ),
        mlflow=MlflowConfig(
            tracking_uri=mlflow_uri,
            username=mlflow_user,
            password=mlflow_password,
        ),
        default_cluster_mode=cluster_mode,
        submission_server=submission_server,
        token=token,
    )
    path = cfg.save()
    click.secho(f"wrote {path}", fg="green")

    # Mode-specific reminders. These are purely informational (the config is
    # already saved) and reflect that kubeconfig is ONLY needed in per_job mode.
    if cluster_mode == "shared":
        if not submission_server or not token:
            click.secho(
                "warning: cluster_mode=shared but submission_server/token is "
                "empty. `raytrain submit` will fail until both are set. "
                "Re-run `raytrain configure` to fill them in.",
                fg="yellow",
            )
        else:
            click.echo(
                "note: shared mode talks to the raytrain Platform API; "
                "no local kubeconfig/kubectl is required."
            )
    else:  # per_job
        click.echo(
            "note: per_job mode talks to Kubernetes directly; a working "
            "kubeconfig (and kubectl) is required on this machine."
        )

    click.echo("tip: you can re-run `raytrain configure` anytime to update it")
    # shared_clusters is a gpu_type -> ray head URL mapping (awkward to prompt).
    # The platform admin usually provides these URLs; set them by editing
    # ~/.raytrain/config.yaml directly, e.g.:
    #   shared_clusters:
    #     h20: http://ray-shared-h20-head.ray-shared.svc:8265
    #     a100: http://ray-shared-a100-head.ray-shared.svc:8265
    click.echo(
        "tip: for shared mode, set `shared_clusters` (gpu_type -> ray head URL) "
        "by editing ~/.raytrain/config.yaml directly"
    )
