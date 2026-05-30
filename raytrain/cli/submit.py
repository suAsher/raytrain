"""
`raytrain submit`: render a RayJob from manifest + flags, pre-create an MLflow
run, apply everything to the cluster.

Typical usage from a repo root that contains `.raytrain.yaml`:

    raytrain submit \
        --config configs/scannet/semseg-pt-v3m1-0-base.py \
        --gpus 8 --nodes 2 --gpu-type h20 \
        --name ptv3-scannet-1

Behavior:
  1. Parse manifest + user config.
  2. (M0) Package current workdir into a zip and upload to MinIO so Ray's
     runtime_env.working_dir can fetch it. Skipped if --no-code-sync or
     manifest.code_sync.enabled = false.
  3. Create MLflow run in experiment `<repo_name>` with tags.
  4. Build a `Plan` (numbers + rendered paths + code_uri).
  5. Emit three k8s objects: ConfigMap (payload), Secret (creds), RayJob.
  6. Apply them, then print the run id and tail command.
"""
from __future__ import annotations

import os
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import click
import yaml

from ..code_sync import (
    CodeSyncError,
    CodeSyncTooLargeError,
    build_code_zip,
    upload_code_zip,
)
from ..kube import apply_yaml_docs, load_kube, parse_docs
from ..manifest import Manifest
from ..minio_util import ensure_bucket, make_client as make_minio_client
from ..mlflow_util import create_run, log_artifact
from ..rayjob import (
    Plan,
    RenderInputs,
    creds_secret_body,
    payload_configmap_body,
    render_rayjob,
)
from ..user_config import UserConfig


DEFAULT_SA = "default"   # override with --service-account if your ns needs it
JOB_NAME_MAX = 52        # k8s label/DNS-1123 safe budget


def _sanitize(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9-]+", "-", s).strip("-")
    return s[:JOB_NAME_MAX] or "run"


def _config_name(config_path: str) -> str:
    return Path(config_path).stem


def _upload_manifest_plan_artifacts(run_id, manifest_yaml, plan_yaml,
                                    tracking_uri):
    """Best-effort upload of manifest.yaml + plan.yaml as MLflow artifacts.

    Right after a per_job ``raytrain submit`` finishes applying the K8s
    objects, we attach the exact serialized manifest/plan to the pre-created
    MLflow run (under artifact_path ``raytrain/``) so the run is fully
    self-auditable: ``raytrain/manifest.yaml`` + ``raytrain/plan.yaml`` mirror
    what lives in the cluster ConfigMap (the driver reads those same files at
    ``/raytrain/manifest.yaml`` / ``/raytrain/plan.yaml``).

    This is **best-effort**: any failure (MLflow unreachable, mlflow not
    installed, network blip) is swallowed with a yellow warning. The job has
    already been applied successfully by the time we get here, so a failed
    artifact upload must never fail the submit.

    Scope: this helper is only wired into the **per_job** submit path (spec
    task 11.2, dep 2.3), where the CLI itself pre-creates the MLflow run and
    holds ``run_info.run_id``. Shared mode (``_submit_shared``) does not create
    a local MLflow run in the CLI, so it is intentionally out of scope here.

    The two YAML strings are written to a short-lived temp dir (cleaned up on
    return) because ``mlflow.log_artifact`` takes a filesystem path. We use the
    fixed basenames ``manifest.yaml`` / ``plan.yaml`` so the artifact tree
    matches the in-cluster layout.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="raytrain-artifacts-") as tmp:
            tmp_dir = Path(tmp)
            manifest_file = tmp_dir / "manifest.yaml"
            plan_file = tmp_dir / "plan.yaml"
            manifest_file.write_text(manifest_yaml)
            plan_file.write_text(plan_yaml)
            log_artifact(run_id, str(manifest_file), tracking_uri,
                         artifact_path="raytrain")
            log_artifact(run_id, str(plan_file), tracking_uri,
                         artifact_path="raytrain")
        click.secho(
            f"      uploaded manifest/plan to MLflow run {run_id} "
            f"(artifact_path=raytrain/)",
            fg="green",
        )
    except Exception as exc:  # best-effort: never break a successful submit
        click.secho(
            f"      warning: failed to upload manifest/plan artifacts to "
            f"MLflow run {run_id}: {exc}",
            fg="yellow",
        )


VALID_CLUSTER_MODES = ("per_job", "shared")

# Env flag that ops flips ON to begin the per_job deprecation window (task 10.6).
PERJOB_DEPRECATED_ENV = "RAYTRAIN_PERJOB_DEPRECATED"
# One-line deprecation notice (stderr, yellow). Kept here so docs/tests can
# reference the exact text.
PERJOB_DEPRECATED_WARNING = (
    "warning: --cluster-mode per_job is deprecated and will be removed; "
    "migrate to shared mode (see docs/migration-shared-cluster.md)."
)


def _is_truthy(value) -> bool:
    """Parse a truthy env value: "1"/"true"/"yes"/"on" (case-insensitive) are
    on; everything else (including None / unset) is off."""
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _maybe_warn_per_job_deprecated(resolved_mode: str) -> None:
    """Emit a one-line, OPT-IN deprecation warning for per_job mode.

    This is the CLI half of spec task ``long-term-evolution / 10.6``
    (deferred / time-gated). The warning is **off by default** and only fires
    when BOTH conditions hold:

      1. ``resolved_mode == "per_job"`` (shared submits are never warned), and
      2. the env flag ``RAYTRAIN_PERJOB_DEPRECATED`` is truthy
         (``"1"``/``"true"``/``"yes"``/``"on"``, case-insensitive).

    When the flag is unset/false (the DEFAULT) this is a no-op, so current
    behavior and every existing per_job test are unchanged — per_job remains
    fully functional as the sanctioned emergency fallback through tasks
    10.4 / 10.5.

    Operations runbook
    ------------------
    Flipping ``RAYTRAIN_PERJOB_DEPRECATED=1`` (in users' env or the shared CLI
    image) is the **mechanical action ops takes when the +6-month deprecation
    window begins** — it makes per_job submits print the migration notice
    without removing any functionality. The actual *removal* of the per_job
    code path + K8s submission code is the **later +6-month milestone** and is
    deliberately NOT done here. See ``docs/phase2-per-job-deprecation.md`` for
    the full timeline and removal checklist.

    Rollback: simply unset ``RAYTRAIN_PERJOB_DEPRECATED`` to silence the
    warning; per_job keeps working until the dedicated removal milestone.
    """
    if resolved_mode != "per_job":
        return
    if not _is_truthy(os.environ.get(PERJOB_DEPRECATED_ENV)):
        return
    click.secho(PERJOB_DEPRECATED_WARNING, fg="yellow", err=True)


def _configmap_cluster_mode(namespace: str) -> Optional[str]:
    """Best-effort read of the `default_cluster_mode` key from the
    `raytrain-defaults` ConfigMap in ``namespace``.

    This is the middle tier of cluster-mode resolution. It MUST never raise:
    if the kubernetes client can't be imported, kubeconfig isn't available,
    the ConfigMap doesn't exist (404), or any other error occurs, we return
    ``None`` so the caller falls through to the next precedence tier.

    Kept as a standalone function so tests can monkeypatch it.
    """
    try:
        from ..kube import load_kube
        from kubernetes import client

        load_kube()
        cm = client.CoreV1Api().read_namespaced_config_map(
            "raytrain-defaults", namespace
        )
        data = cm.data or {}
        value = data.get("default_cluster_mode")
        return value or None
    except Exception:
        # Any failure (import error, no kubeconfig, 404, connection refused...)
        # is treated as "no value at this tier".
        return None


def _resolve_cluster_mode(cli_value, user_cfg, namespace) -> str:
    """Resolve the effective cluster mode using precedence (highest first):

      1. CLI flag ``--cluster-mode`` (when not None)
      2. namespace ConfigMap ``raytrain-defaults`` (``default_cluster_mode``)
      3. UserConfig ``default_cluster_mode``
      4. final fallback ``"per_job"``

    The ConfigMap (tier 2) is only consulted when the CLI value is None, so a
    ``--cluster-mode shared`` invocation never forces a kube connection. The
    final result is validated against the allowed modes; anything unexpected
    defaults back to ``per_job`` defensively.
    """
    resolved = None

    # tier 1: explicit CLI flag short-circuits everything (no kube access).
    if cli_value:
        resolved = cli_value
    else:
        # tier 2: namespace ConfigMap (best-effort, never raises).
        resolved = _configmap_cluster_mode(namespace)

        # tier 3: user config default.
        if not resolved:
            resolved = getattr(user_cfg, "default_cluster_mode", None)

    # tier 4 + validation: fall back to per_job for empty/invalid values.
    if resolved not in VALID_CLUSTER_MODES:
        resolved = "per_job"
    return resolved


@click.command(help="Submit a training job to KubeRay.")
@click.option("--config", "config_path", required=True, type=click.Path(),
              help="Training config file, relative to the repo root, e.g. "
                   "configs/scannet/semseg-pt-v3m1-0-base.py")
@click.option("--name", "exp_name", default=None,
              help="Experiment/job name. Defaults to the config file stem.")
@click.option("--gpus", "gpus_per_node", type=int, default=None,
              help="GPUs per node. Defaults to manifest.resources.gpus_per_node.")
@click.option("--nodes", "num_nodes", type=int, default=1,
              help="Number of machines (1 = single-node DDP).")
@click.option("--gpu-type", default="h20", type=click.Choice(["a100", "h20"]),
              help="Which GPU pool to schedule on (nodeSelector: gpu=<type>).")
@click.option("--image", default=None, help="Override manifest.image.")
@click.option("--manifest-path", default=".raytrain.yaml", show_default=True,
              type=click.Path(),
              help="Path to the repo's .raytrain.yaml manifest.")
@click.option("--experiment", default=None,
              help="MLflow experiment name. Defaults to manifest.repo_name.")
@click.option("--service-account", default=DEFAULT_SA, show_default=True)
@click.option("--ttl", "ttl_seconds", type=int, default=3600, show_default=True,
              help="Seconds to keep the RayCluster alive after the job finishes.")
@click.option("--dry-run", is_flag=True, help="Print rendered YAML only; do not apply.")
@click.option("--config-override", multiple=True,
              help="Extra `key=value` options forwarded to the launcher's "
                   "`--options` list. Repeat for multiple.")
@click.option("--save-path", default=None,
              help="Where to write checkpoints and logs. Defaults to a local path "
                   "'/mnt/ray-cache/exp/{user}/{run_id}'. For multi-node training, "
                   "you can specify a shared filesystem path or S3 URI (e.g. 's3://bucket/{user}/{run_id}').")
@click.option("--no-code-sync", is_flag=True,
              help="Skip packaging current dir into MinIO; use legacy "
                   "code-in-image submission. Useful for debugging or "
                   "rolling back if working_dir misbehaves.")
@click.option("--workdir-zip", default=None, type=click.Path(),
              help="Use a pre-built code zip instead of packaging on the fly. "
                   "The zip is still uploaded and SHA256-hashed.")
@click.option("--code-bucket", default=None,
              help="Override manifest.code_sync.bucket (where the zip is stored).")
@click.option("--cluster-mode", default=None,
              type=click.Choice(["per_job", "shared"]),
              help="per_job (legacy, talks K8s directly) or shared (talks the "
                   "raytrain Platform API; no kubeconfig needed). Resolution "
                   "order: this flag > namespace ConfigMap 'raytrain-defaults' "
                   "> user config's default_cluster_mode > per_job.")
def submit(config_path, exp_name, gpus_per_node, num_nodes, gpu_type, image,
           manifest_path, experiment, service_account, ttl_seconds, dry_run,
           config_override, save_path,
           no_code_sync, workdir_zip, code_bucket, cluster_mode):
    # 1. Load manifest + user config
    manifest = Manifest.load(manifest_path)
    user_cfg = UserConfig.load()

    if gpus_per_node is None:
        gpus_per_node = manifest.resources.gpus_per_node
    image = image or user_cfg.default_image or manifest.image

    config_name = _config_name(config_path)
    exp_name = exp_name or config_name
    stamp = datetime.now().strftime("%y%m%d-%H%M%S")
    job_name = _sanitize(
        f"{user_cfg.user_name}-{manifest.repo_name}-{exp_name}-{stamp}"
    )
    run_name = f"{exp_name}-{stamp}"
    experiment = experiment or manifest.repo_name or "default"

    # Resolve cluster mode: CLI flag > namespace ConfigMap > user config default.
    resolved_mode = _resolve_cluster_mode(
        cluster_mode, user_cfg, user_cfg.namespace
    )

    # --- shared mode: delegate to the raytrain Platform API ---------------
    if resolved_mode == "shared":
        return _submit_shared(
            manifest=manifest,
            user_cfg=user_cfg,
            config_path=config_path,
            config_name=config_name,
            exp_name=exp_name,
            job_name=job_name,
            gpus_per_node=gpus_per_node,
            num_nodes=num_nodes,
            gpu_type=gpu_type,
            no_code_sync=no_code_sync,
            workdir_zip=workdir_zip,
            code_bucket=code_bucket,
            config_override=config_override,
            save_path=save_path,
            dry_run=dry_run,
        )

    # --- per_job mode (legacy K8s-direct path) ----------------------------
    # Gated deprecation warning (task 10.6, deferred): no-op by default; only
    # fires when ops have flipped RAYTRAIN_PERJOB_DEPRECATED on. per_job stays
    # fully functional regardless — nothing is removed here.
    _maybe_warn_per_job_deprecated(resolved_mode)

    # Decide whether to use code-as-submission (M0). Precedence:
    #   --no-code-sync flag             (explicit off)
    #   manifest.code_sync.enabled       (default true)
    code_sync_enabled = manifest.code_sync.enabled and not no_code_sync
    total_steps = 5 if code_sync_enabled else 4

    # 2. (M0) Package + upload code zip if enabled
    code_uri: str | None = None
    code_hash: str | None = None
    code_size_bytes: int = 0

    if code_sync_enabled:
        bucket = code_bucket or manifest.code_sync.bucket
        max_bytes = manifest.code_sync.max_size_mib * 1024 * 1024

        click.echo(
            f"[1/{total_steps}] packaging code (workdir={Path.cwd()})"
        )
        try:
            if workdir_zip:
                # Reuse existing zip but re-hash so MLflow has correct fingerprint.
                from ..code_sync import CodeBundle
                import hashlib
                zp = Path(workdir_zip).expanduser().resolve()
                if not zp.is_file():
                    raise click.ClickException(
                        f"--workdir-zip not found: {zp}"
                    )
                if zp.stat().st_size > max_bytes:
                    raise click.ClickException(
                        f"--workdir-zip exceeds limit "
                        f"({zp.stat().st_size / 1024 / 1024:.1f} MiB > "
                        f"{manifest.code_sync.max_size_mib} MiB)"
                    )
                hasher = hashlib.sha256()
                with zp.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                        hasher.update(chunk)
                bundle = CodeBundle(
                    zip_path=zp,
                    sha256=hasher.hexdigest(),
                    size_bytes=zp.stat().st_size,
                    file_count=-1,
                )
            else:
                bundle = build_code_zip(
                    workdir=Path.cwd(),
                    job_name=job_name,
                    user=user_cfg.user_name,
                    extra_excludes=manifest.code_sync.extra_excludes,
                    max_size_bytes=max_bytes,
                )
            click.secho(
                f"      zip size: {bundle.size_mib:.1f} MiB, "
                f"sha256: {bundle.sha256[:12]}..., "
                f"files: {bundle.file_count}",
                fg="green",
            )
        except CodeSyncTooLargeError as exc:
            raise click.ClickException(str(exc)) from exc
        except CodeSyncError as exc:
            raise click.ClickException(f"code packaging failed: {exc}") from exc

        # Upload (skip on dry-run; we still need the bundle for hash injection)
        if dry_run:
            # In dry-run, fabricate a stable fake URI so the rendered YAML
            # is realistic. Actual upload only happens on a real submit.
            code_uri = f"s3://{bucket}/{user_cfg.user_name}/{job_name}.zip"
            click.echo(f"[2/{total_steps}] uploading code (skipped: --dry-run)")
            click.secho(f"      would upload to: {code_uri}", fg="yellow")
        else:
            click.echo(f"[2/{total_steps}] uploading code -> "
                       f"s3://{bucket}/{user_cfg.user_name}/{job_name}.zip")
            try:
                minio_client = make_minio_client(
                    endpoint=user_cfg.minio.endpoint,
                    access_key=user_cfg.minio.access_key,
                    secret_key=user_cfg.minio.secret_key,
                    secure=user_cfg.minio.secure,
                    region=user_cfg.minio.region,
                )
                # Best-effort ensure bucket. Admins should pre-create bucket
                # with 7d lifecycle (deploy/setup-code-bucket.sh); this is a
                # safety net in case it hasn't run yet.
                ensure_bucket(minio_client, bucket)
                code_uri = upload_code_zip(
                    bundle,
                    minio_client,
                    bucket=bucket,
                    user=user_cfg.user_name,
                    job_name=job_name,
                    dedup=manifest.code_sync.dedup,
                )
            except CodeSyncError as exc:
                raise click.ClickException(f"code upload failed: {exc}") from exc
            click.secho(f"      uploaded: {code_uri}", fg="green")

        code_hash = bundle.sha256
        code_size_bytes = bundle.size_bytes

    # 3. Create MLflow run (get run_id)
    step_no = 3 if code_sync_enabled else 1
    click.echo(f"[{step_no}/{total_steps}] creating MLflow run "
               f"in experiment={experiment!r}")
    run_info = create_run(
        tracking_uri=user_cfg.mlflow.tracking_uri,
        username=user_cfg.mlflow.username,
        password=user_cfg.mlflow.password,
        experiment=experiment,
        run_name=run_name,
        tags={
            "user": user_cfg.user_name,
            "repo": manifest.repo_name or "",
            "gpu_type": gpu_type,
            "num_nodes": str(num_nodes),
            "gpus_per_node": str(gpus_per_node),
            "world_size": str(num_nodes * gpus_per_node),
            "config": config_path,
            "job_name": job_name,
            # M0 audit: tag the MLflow run with code metadata for later
            # `raytrain reproduce` (will be added in a follow-up PR).
            "raytrain.code_uri": code_uri or "",
            "raytrain.code_hash": code_hash or "",
            "raytrain.code_size_bytes": str(code_size_bytes),
        },
    )
    click.secho(f"      MLflow run_id = {run_info.run_id}", fg="green")

    # 4. Build Plan
    # Resolve save_path: CLI override > manifest override > default local NVMe path.
    resolved_save_path = save_path or manifest.save_path
    if resolved_save_path:
        subs = {
            "user": user_cfg.user_name,
            "run_id": run_info.run_id,
            "config_name": config_name,
        }
        try:
            resolved_save_path = resolved_save_path.format(**subs)
        except Exception as e:
            click.secho(f"Warning: failed to format save_path template: {e}", fg="yellow")
    else:
        resolved_save_path = f"/mnt/ray-cache/exp/{user_cfg.user_name}/{run_info.run_id}"

    # config_path given as a repo-relative path; training code runs with cwd=workdir
    plan = Plan(
        job_name=job_name,
        run_id=run_info.run_id,
        user=user_cfg.user_name,
        repo_name=manifest.repo_name or "repo",
        num_nodes=num_nodes,
        gpus_per_node=gpus_per_node,
        cpus_per_node=manifest.resources.cpus_per_node,
        gpu_type=gpu_type,
        config_name=config_name,
        config_path=config_path,
        save_path=resolved_save_path,
        datasets=[vars(d) for d in manifest.datasets],
        artifacts=list(manifest.artifacts),
        launcher_type=manifest.launcher.type,
        launcher_entrypoint=manifest.launcher.entrypoint,
        launcher_args=list(manifest.launcher.args),
        launcher_env=dict(manifest.launcher.env),
        workdir=manifest.workdir,
        extra_options={"overrides": list(config_override)},
        data_source=(vars(manifest.data_source) if manifest.data_source else None),
        cpu_workers=manifest.cpu_workers,
        # M0: code-as-submission
        code_uri=code_uri,
        code_hash=code_hash,
        code_size_bytes=code_size_bytes,
    )

    # 5. Render k8s objects
    step_no = 4 if code_sync_enabled else 2
    data_mode = (f"Ray Data ({manifest.data_source.type} @ {manifest.data_source.uri})"
                 if manifest.data_source else f"{len(manifest.datasets)} dataset mounts")
    code_mode = "code-sync (working_dir)" if code_sync_enabled else "image-baked code"
    click.echo(f"[{step_no}/{total_steps}] rendering RayJob "
               f"({num_nodes}×{gpus_per_node} GPUs on {gpu_type}, "
               f"data: {data_mode}, code: {code_mode})")
    cm = payload_configmap_body(manifest, plan)
    secret = creds_secret_body(user_cfg, plan.job_name)
    rj_yaml = render_rayjob(RenderInputs(
        manifest=manifest,
        user_cfg=user_cfg,
        plan=plan,
        image=image,
        service_account=service_account,
        payload_configmap=cm["metadata"]["name"],
        ttl_seconds_after_finished=ttl_seconds,
        object_store_memory_bytes=RenderInputs.compute_object_store_bytes(manifest),
    ))
    rj_docs = parse_docs(rj_yaml)

    if dry_run:
        click.echo("# --- configmap ---")
        click.echo(yaml.safe_dump(cm, sort_keys=False))
        click.echo("# --- secret (data redacted) ---")
        redacted = {**secret, "data": {k: "***" for k in secret["data"]}}
        click.echo(yaml.safe_dump(redacted, sort_keys=False))
        click.echo("# --- rayjob ---")
        click.echo(rj_yaml)
        return
    # 6. Apply
    step_no = 5 if code_sync_enabled else 3
    click.echo(f"[{step_no}/{total_steps}] applying to namespace={user_cfg.namespace}")
    load_kube()
    apply_yaml_docs([cm, secret, *rj_docs], namespace=user_cfg.namespace)

    # Best-effort: attach the exact serialized manifest/plan (the same strings
    # baked into the ConfigMap) to the pre-created MLflow run for audit. Reuse
    # cm["data"] so we never re-serialize. Never fails the submit (task 11.2).
    _upload_manifest_plan_artifacts(
        run_info.run_id,
        cm["data"]["manifest.yaml"],
        cm["data"]["plan.yaml"],
        user_cfg.mlflow.tracking_uri,
    )

    # 获取 NodePort 端口以方便直接查看 Ray Dashboard
    node_port = None
    try:
        from kubernetes import client
        core = client.CoreV1Api()
        svc_name = f"{plan.job_name}-dashboard"
        time.sleep(0.5)
        svc = core.read_namespaced_service(svc_name, user_cfg.namespace)
        if svc.spec.ports:
            for p in svc.spec.ports:
                if p.name == "dashboard":
                    node_port = p.node_port
                    break
            if node_port is None:
                node_port = svc.spec.ports[0].node_port
    except Exception:
        pass

    final_step = total_steps
    click.secho(f"[{final_step}/{total_steps}] submitted", fg="green", bold=True)
    click.echo("")
    click.echo(f"  job_name     : {plan.job_name}")
    click.echo(f"  mlflow_run   : {run_info.run_id}")
    click.echo(f"  experiment   : {experiment}")
    click.echo(f"  namespace    : {user_cfg.namespace}")
    if node_port:
        click.echo(f"  ray_dashboard: http://<Kubernetes-Node-IP>:{node_port}")
    click.echo("")
    click.echo(f"follow logs :  raytrain logs {plan.job_name} -f")
    click.echo(f"list yours  :  raytrain list")
    click.echo(f"stop        :  raytrain stop {plan.job_name}")
    click.echo(f"mlflow ui   :  {user_cfg.mlflow.tracking_uri}/#/experiments/"
               f"{run_info.experiment_id}/runs/{run_info.run_id}")


# ---------------------------------------------------------------------------- #
# M1: shared-cluster submission (talks the Platform API, no K8s)
# ---------------------------------------------------------------------------- #


def _substitute_args(args, vals):
    """Replace {placeholder} tokens in launcher args with vals.

    Unknown placeholders are left intact so the server / driver can fill them.
    """
    out = []
    for a in args:
        try:
            out.append(a.format(**vals))
        except (KeyError, IndexError, ValueError):
            out.append(a)
    return out


def _build_entrypoint(manifest, config_path, save_path, num_nodes,
                      gpus_per_node, config_override):
    """Build the shell entrypoint string for shared-mode submission.

    In shared mode, Ray runs ``entrypoint`` directly on the head node after
    unpacking working_dir. We mirror the legacy launcher arg substitution so
    behavior matches per_job mode for the common launchers.
    """
    world_size = num_nodes * gpus_per_node
    vals = {
        "config": config_path,
        "save_path": save_path,
        "num_nodes": str(num_nodes),
        "num_gpus_per_node": str(gpus_per_node),
        "world_size": str(world_size),
        "run_name": "",
        # master_addr/port/node_rank are resolved by the in-cluster driver;
        # leave them as placeholders for it to fill.
    }
    launcher = manifest.launcher
    entry = launcher.entrypoint
    parts = entry.split() if " " in entry else ["python", entry]
    if len(parts) == 1:
        parts = ["python", parts[0]]
    args = _substitute_args(list(launcher.args), vals)
    extra = []
    for ov in (config_override or []):
        extra.append(str(ov))
    return " ".join(parts + args + extra)


def _submit_shared(
    *,
    manifest,
    user_cfg,
    config_path,
    config_name,
    exp_name,
    job_name,
    gpus_per_node,
    num_nodes,
    gpu_type,
    no_code_sync,
    workdir_zip,
    code_bucket,
    config_override,
    save_path,
    dry_run,
):
    """Submit through the raytrain Platform API (cluster_mode=shared)."""
    from ..platform_client import PlatformClient, PlatformError

    if not user_cfg.submission_server:
        raise click.ClickException(
            "cluster_mode=shared requires 'submission_server' in "
            "~/.raytrain/config.yaml. Run `raytrain configure` and set the "
            "platform URL + token."
        )
    if not user_cfg.token:
        raise click.ClickException(
            "cluster_mode=shared requires a 'token' in ~/.raytrain/config.yaml."
        )

    code_sync_enabled = manifest.code_sync.enabled and not no_code_sync
    total = 4 if code_sync_enabled else 3

    # 1. package code
    code_uri = None
    code_hash = None
    if code_sync_enabled:
        max_bytes = manifest.code_sync.max_size_mib * 1024 * 1024
        click.echo(f"[1/{total}] packaging code (workdir={Path.cwd()})")
        try:
            if workdir_zip:
                import hashlib
                from ..code_sync import CodeBundle
                zp = Path(workdir_zip).expanduser().resolve()
                if not zp.is_file():
                    raise click.ClickException(f"--workdir-zip not found: {zp}")
                hasher = hashlib.sha256()
                with zp.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                        hasher.update(chunk)
                bundle = CodeBundle(
                    zip_path=zp, sha256=hasher.hexdigest(),
                    size_bytes=zp.stat().st_size, file_count=-1,
                )
            else:
                bundle = build_code_zip(
                    workdir=Path.cwd(),
                    job_name=job_name,
                    user=user_cfg.user_name,
                    extra_excludes=manifest.code_sync.extra_excludes,
                    max_size_bytes=max_bytes,
                )
            click.secho(
                f"      zip size: {bundle.size_mib:.1f} MiB, "
                f"sha256: {bundle.sha256[:12]}...", fg="green"
            )
        except CodeSyncTooLargeError as exc:
            raise click.ClickException(str(exc)) from exc
        except CodeSyncError as exc:
            raise click.ClickException(f"code packaging failed: {exc}") from exc

        if dry_run:
            code_uri = f"s3://{code_bucket or manifest.code_sync.bucket}/" \
                       f"{user_cfg.user_name}/{job_name}.zip"
            code_hash = bundle.sha256
            click.echo(f"[2/{total}] uploading code (skipped: --dry-run)")
            click.secho(f"      would upload to: {code_uri}", fg="yellow")
        else:
            click.echo(f"[2/{total}] uploading code via Platform API")
            try:
                with PlatformClient(
                    user_cfg.submission_server, user_cfg.token
                ) as pc:
                    res = pc.upload_code(
                        zip_path=str(bundle.zip_path),
                        sha256=bundle.sha256,
                        job_name=job_name,
                    )
            except PlatformError as exc:
                raise click.ClickException(f"code upload failed: {exc}") from exc
            code_uri = res["code_uri"]
            code_hash = res["sha256"]
            click.secho(f"      uploaded: {code_uri}", fg="green")

    # 2. build save_path + entrypoint
    resolved_save_path = save_path or manifest.save_path or \
        f"/mnt/ray-cache/exp/{user_cfg.user_name}/{config_name}"
    try:
        resolved_save_path = resolved_save_path.format(
            user=user_cfg.user_name, run_id=job_name, config_name=config_name
        )
    except Exception:
        pass
    entrypoint = _build_entrypoint(
        manifest, config_path, resolved_save_path,
        num_nodes, gpus_per_node, config_override,
    )

    # 3. submit
    step = 3 if code_sync_enabled else 1
    click.echo(f"[{step}/{total}] submitting to Platform "
               f"({num_nodes}×{gpus_per_node} GPUs on {gpu_type})")
    if dry_run:
        click.echo("# --- shared submission (dry-run) ---")
        click.echo(f"  server     : {user_cfg.submission_server}")
        click.echo(f"  gpu_type   : {gpu_type}")
        click.echo(f"  nodes      : {num_nodes}")
        click.echo(f"  gpus/node  : {gpus_per_node}")
        click.echo(f"  code_uri   : {code_uri}")
        click.echo(f"  entrypoint : {entrypoint}")
        return

    try:
        with PlatformClient(user_cfg.submission_server, user_cfg.token) as pc:
            resp = pc.submit_job(
                repo=manifest.repo_name or "repo",
                exp_name=exp_name,
                gpu_type=gpu_type,
                num_nodes=num_nodes,
                gpus_per_node=gpus_per_node,
                entrypoint=entrypoint,
                code_uri=code_uri,
                code_hash=code_hash,
                extra_env=dict(manifest.launcher.env),
                metadata={
                    "config": config_path,
                    "data_source_uri": (
                        manifest.data_source.uri if manifest.data_source else ""
                    ),
                },
            )
    except PlatformError as exc:
        raise click.ClickException(f"submission failed: {exc}") from exc

    submission_id = resp["submission_id"]
    click.secho(f"[{total}/{total}] submitted", fg="green", bold=True)
    click.echo("")
    click.echo(f"  submission_id : {submission_id}")
    click.echo(f"  cluster       : {resp.get('cluster_address', '?')}")
    click.echo(f"  gpu_type      : {gpu_type}")
    click.echo("")
    click.echo(f"follow logs :  raytrain logs {submission_id} -f")
    click.echo(f"stop        :  raytrain stop {submission_id}")
