"""
MLflow helpers used by the CLI (pre-run creation) and by the driver (logging).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass
class RunInfo:
    run_id: str
    experiment_id: str
    run_name: str


def _client(tracking_uri: str, username: str, password: str):
    # lazy import so CLI works on systems without mlflow
    import mlflow
    mlflow.set_tracking_uri(tracking_uri)
    if username:
        os.environ["MLFLOW_TRACKING_USERNAME"] = username
    if password:
        os.environ["MLFLOW_TRACKING_PASSWORD"] = password
    return mlflow


def create_run(
    tracking_uri: str,
    username: str,
    password: str,
    experiment: str,
    run_name: str,
    tags: dict[str, str],
) -> RunInfo:
    """Create an MLflow run in the given experiment; returns run_id."""
    mlflow = _client(tracking_uri, username, password)
    exp = mlflow.set_experiment(experiment_name=experiment)
    run = mlflow.start_run(run_name=run_name, tags=tags)
    info = run.info
    mlflow.end_run(status="RUNNING")
    return RunInfo(
        run_id=info.run_id,
        experiment_id=info.experiment_id,
        run_name=run_name,
    )


def log_params(run_id: str, params: dict[str, Any], tracking_uri: str) -> None:
    import mlflow
    mlflow.set_tracking_uri(tracking_uri)
    with mlflow.start_run(run_id=run_id):
        mlflow.log_params(params)


def log_artifact(run_id: str, path: str, tracking_uri: str,
                 artifact_path: str | None = None) -> None:
    import mlflow
    mlflow.set_tracking_uri(tracking_uri)
    with mlflow.start_run(run_id=run_id):
        mlflow.log_artifact(path, artifact_path=artifact_path)


def set_status(run_id: str, status: str, tracking_uri: str) -> None:
    """status: FINISHED / FAILED / KILLED"""
    import mlflow
    from mlflow.entities import RunStatus
    mlflow.set_tracking_uri(tracking_uri)
    client = mlflow.MlflowClient()
    client.set_terminated(run_id, status=status)


def get_run_tags(
    run_id: str,
    tracking_uri: str,
    username: str = "",
    password: str = "",
) -> dict[str, str]:
    """Fetch all tags for an MLflow run by run_id.

    Used by `raytrain reproduce` to read the `raytrain.code_uri` /
    `raytrain.code_hash` tags written at submit time.

    The mlflow import is kept lazy (inside the function) so the CLI still
    imports on systems without mlflow installed.
    """
    mlflow = _client(tracking_uri, username, password)
    client = mlflow.MlflowClient()
    run = client.get_run(run_id)
    return dict(run.data.tags or {})
