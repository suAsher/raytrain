"""
Ray Job driver. Runs as the `entrypoint` of a RayJob in the head pod.

Responsibilities:
  1. Attach to Ray ("auto") that the RayJob already started.
  2. Create a placement group of shape [{GPU: N, CPU: C}] * num_nodes,
     strategy STRICT_SPREAD so each bundle lands on a distinct worker pod.
  3. Start one `NodeLauncher` actor per bundle; ask each for its node IP.
  4. Pick rank-0 IP as MASTER_ADDR, pick a free port for MASTER_PORT.
  5. In parallel, each actor:
       a. Syncs required MinIO datasets to /mnt/ray-cache and symlinks them
          into the workdir (idempotent, cache-hit fast path).
       b. Builds the subprocess command via launcher adapter.
       c. Runs the training subprocess in `workdir`, streaming stdout/stderr.
  6. On success: upload artifacts to MinIO + MLflow, mark MLflow RUN as FINISHED.
     On failure: mark RUN as FAILED, re-raise, so RayJob reports failure.

Reads its inputs either from base64-encoded YAML in the environment
(``RAYTRAIN_MANIFEST_B64`` / ``RAYTRAIN_PLAN_B64``, used by the shared/Platform
path where the head pod has no ConfigMap-mounted ``/raytrain/*.yaml``) or from
``/raytrain/manifest.yaml`` and ``/raytrain/plan.yaml`` file paths, mounted as a
ConfigMap on both head and worker pods (the legacy per-job path).
"""
from __future__ import annotations

import argparse
import base64
import binascii
import os
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from .dataset_sync import DatasetSpec, sync_datasets
from .launchers import build_command


# ---------------------------------------------------------------------------- #
# Ray imports are local to main() so the module is importable on the CLI side.
# ---------------------------------------------------------------------------- #


@dataclass
class DriverConfig:
    manifest: dict[str, Any]
    plan: dict[str, Any]

    # Environment variables that carry base64-encoded YAML in the shared /
    # Platform path (the head pod has no ConfigMap-mounted /raytrain/*.yaml).
    MANIFEST_ENV = "RAYTRAIN_MANIFEST_B64"
    PLAN_ENV = "RAYTRAIN_PLAN_B64"

    @staticmethod
    def load(manifest_path: str, plan_path: str) -> "DriverConfig":
        """Load manifest/plan from two YAML files (legacy per-job path)."""
        with open(manifest_path) as f:
            m = yaml.safe_load(f)
        with open(plan_path) as f:
            p = yaml.safe_load(f)
        return DriverConfig(manifest=m, plan=p)

    @staticmethod
    def from_env(environ: Optional[Mapping[str, str]] = None) -> "DriverConfig":
        """
        Reconstruct manifest/plan from base64-encoded YAML in the environment.

        Used by the shared/Platform path (design 2.2): the server submits the
        RayJob with ``entrypoint="python -m raytrain.entrypoint.driver
        --from-env"`` and passes the manifest/plan as base64-encoded YAML in
        ``RAYTRAIN_MANIFEST_B64`` / ``RAYTRAIN_PLAN_B64`` env vars, because the
        head pod has no ConfigMap-mounted ``/raytrain/*.yaml`` in shared mode.

        Raises ``ValueError`` with a clear message if either env var is
        missing/empty, if base64 decoding fails, or if the decoded payload is
        not a valid YAML mapping.
        """
        env = os.environ if environ is None else environ
        manifest = DriverConfig._decode_b64_yaml(env, DriverConfig.MANIFEST_ENV)
        plan = DriverConfig._decode_b64_yaml(env, DriverConfig.PLAN_ENV)
        return DriverConfig(manifest=manifest, plan=plan)

    @staticmethod
    def _decode_b64_yaml(
        env: Mapping[str, str], var_name: str
    ) -> dict[str, Any]:
        """Base64-decode + YAML-parse one env var into a dict."""
        raw = (env.get(var_name) or "").strip()
        if not raw:
            raise ValueError(
                f"env var {var_name} is missing or empty; cannot load "
                f"manifest/plan from environment. (env-mode requires both "
                f"{DriverConfig.MANIFEST_ENV} and {DriverConfig.PLAN_ENV}.)"
            )
        try:
            decoded = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as e:
            raise ValueError(
                f"env var {var_name} is not valid base64: {e}"
            ) from e
        try:
            doc = yaml.safe_load(decoded)
        except yaml.YAMLError as e:
            raise ValueError(
                f"env var {var_name} did not decode to valid YAML: {e}"
            ) from e
        if not isinstance(doc, dict):
            raise ValueError(
                f"env var {var_name} must decode to a YAML mapping, got "
                f"{type(doc).__name__}"
            )
        return doc


def _choose_driver_config(
    args: argparse.Namespace, environ: Optional[Mapping[str, str]] = None
) -> DriverConfig:
    """
    Decide where to load the manifest/plan from, and load it.

    Precedence (kept small and pure so it is unit-testable without Ray):

    1. If ``--from-env`` was passed, OR both ``RAYTRAIN_MANIFEST_B64`` and
       ``RAYTRAIN_PLAN_B64`` are present while no ``--manifest``/``--plan`` file
       paths were given, load from the environment (shared/Platform path).
    2. Otherwise, if both file paths were given, load from the files (legacy
       per-job path) — this keeps the Phase 1 RayJob template working exactly
       as before.
    3. If neither source is available, raise ``ValueError`` with a clear
       message.
    """
    env = os.environ if environ is None else environ
    have_paths = bool(getattr(args, "manifest", None)) and bool(
        getattr(args, "plan", None)
    )
    env_present = bool((env.get(DriverConfig.MANIFEST_ENV) or "").strip()) and bool(
        (env.get(DriverConfig.PLAN_ENV) or "").strip()
    )

    if getattr(args, "from_env", False) or (env_present and not have_paths):
        return DriverConfig.from_env(env)
    if have_paths:
        return DriverConfig.load(args.manifest, args.plan)
    raise ValueError(
        "no manifest/plan source available: pass --manifest/--plan file paths, "
        "or --from-env with RAYTRAIN_MANIFEST_B64 / RAYTRAIN_PLAN_B64 set in "
        "the environment."
    )


def _pick_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------- #
# M0: code-as-submission helpers
# ---------------------------------------------------------------------------- #


def _resolve_workdir(plan: dict[str, Any], manifest: dict[str, Any]) -> str:
    """
    Decide which directory the training subprocess should ``cd`` into.

    Precedence:

    1. If Ray's runtime_env injected a working_dir for us
       (``RAY_RUNTIME_ENV_WORKING_DIR``), use that. This is the M0
       code-as-submission path: Ray fetched the user's code zip from MinIO,
       unzipped it into a session-local temp dir, and exposed the path in
       this env var. Both head pod (driver) and worker pods (NodeLauncher
       actors) get the same env var on the same node.
    2. Otherwise fall back to ``plan.workdir`` or ``manifest.workdir``.
       This preserves the legacy "code is baked into the image" behavior.
    3. If neither is set, raise — this is a configuration bug, not a runtime
       issue, and a clear early failure beats a confusing chdir later.
    """
    rt_dir = os.environ.get("RAY_RUNTIME_ENV_WORKING_DIR", "").strip()
    if rt_dir:
        return rt_dir
    fallback = (plan.get("workdir") or manifest.get("workdir") or "").strip()
    if fallback:
        return fallback
    raise RuntimeError(
        "no workdir resolved: neither RAY_RUNTIME_ENV_WORKING_DIR nor "
        "plan.workdir / manifest.workdir is set. Either enable code_sync "
        "in .raytrain.yaml or provide a workdir explicitly."
    )


def _format_code_banner(code_hash: str | None = None) -> str:
    """
    Build the one-line startup banner that records which code revision this
    driver is running, e.g. ``[driver] code_hash=a3f8c1d2e4b5``.

    The value is read from the ``RAYTRAIN_CODE_HASH`` environment variable
    (injected into the head/worker pods by the RayJob template's
    ``runtimeEnvYAML.env_vars``) unless an explicit ``code_hash`` is passed in.
    Only the first 12 characters are shown — enough to eyeball-match against
    the hash printed by ``raytrain submit`` and the ``raytrain.code_hash``
    MLflow tag, without cluttering the log with the full 64-char SHA256.

    When no code hash is available (legacy code-in-image mode), the banner
    reads ``[driver] code_hash=<none>`` so the line is always present and
    greppable in the head pod logs.
    """
    if code_hash is None:
        code_hash = os.environ.get("RAYTRAIN_CODE_HASH", "")
    code_hash = (code_hash or "").strip()
    if not code_hash:
        return "[driver] code_hash=<none>"
    return f"[driver] code_hash={code_hash[:12]}"


# ---------------------------------------------------------------------------- #
# NodeLauncher actor: wraps one worker pod, runs the subprocess there.
# ---------------------------------------------------------------------------- #

def _make_node_launcher(gpus_per_node: int, cpus_per_node: int):
    import ray

    @ray.remote(num_gpus=gpus_per_node, num_cpus=cpus_per_node)
    class NodeLauncher:
        def get_ip(self) -> str:
            return ray.util.get_node_ip_address()

        def sync(self, dataset_specs: list[dict], workdir: str,
                 cache_root: str) -> str:
            specs = [DatasetSpec(**s) for s in dataset_specs]
            sync_datasets(specs, workdir=workdir, cache_root=cache_root)
            return f"ok on {ray.util.get_node_ip_address()}"

        def run(self, cmd: list[str], workdir: str,
                env_overrides: dict[str, str],
                stream_prefix: str) -> int:
            # M0: working_dir comes from Ray's runtime_env (it set
            # RAY_RUNTIME_ENV_WORKING_DIR on every worker before this actor
            # started). If present, prefer it over whatever the driver passed
            # in — the driver value is from a different pod and may not exist
            # on this worker. Fall back to the passed-in `workdir` only when
            # legacy "code-in-image" mode is being used. Reuse _resolve_workdir
            # so the precedence rule lives in exactly one place: the actor reads
            # its own RAY_RUNTIME_ENV_WORKING_DIR, else the passed-in workdir.
            wd = Path(_resolve_workdir({"workdir": workdir}, {}))
            wd.mkdir(parents=True, exist_ok=True)

            env = os.environ.copy()
            env.update(env_overrides)
            env["RAYTRAIN_RESOLVED_WORKDIR"] = str(wd)

            # GPU visibility: Ray already sets CUDA_VISIBLE_DEVICES for us
            # via the actor's num_gpus assignment; do not override it.
            print(f"[{stream_prefix}] launching in {wd}: {' '.join(cmd)}",
                  flush=True)
            print(f"[{stream_prefix}] MASTER_ADDR={env.get('MASTER_ADDR')} "
                  f"MASTER_PORT={env.get('MASTER_PORT')} "
                  f"NODE_RANK={env.get('NODE_RANK')} "
                  f"WORLD_SIZE={env.get('WORLD_SIZE')} "
                  f"CUDA_VISIBLE_DEVICES={env.get('CUDA_VISIBLE_DEVICES')}",
                  flush=True)

            proc = subprocess.Popen(
                cmd, cwd=str(wd), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                sys.stdout.write(f"[{stream_prefix}] {line}")
                sys.stdout.flush()
            rc = proc.wait()
            return rc

        def upload_artifacts(self, manifest: dict, plan: dict, save_path: str,
                             tracking_uri: str) -> None:
            # Run the upload logic on the worker pod itself!
            is_remote = "://" in save_path or save_path.startswith("s3://") or save_path.startswith("minio://")
            run_dir = None if is_remote else Path(save_path)
            _upload_artifacts(manifest, plan, run_dir, tracking_uri)

    return NodeLauncher


def _slot_values(
    plan: dict[str, Any],
    run_id: str,
    num_nodes: int,
    gpus_per_node: int,
    workdir: str,
    save_path: str,
    master_addr: str,
    master_port: int,
    node_rank: int,
    manifest: dict[str, Any],
) -> dict[str, str]:
    return {
        "master_addr": master_addr,
        "master_port": str(master_port),
        "node_rank": str(node_rank),
        "num_nodes": str(num_nodes),
        "num_gpus_per_node": str(gpus_per_node),
        "world_size": str(num_nodes * gpus_per_node),
        "config": plan["config_path"],
        "save_path": save_path,
        "run_id": run_id,
        "workdir": workdir,
        "run_name": f"{plan.get('config_name', 'run')}-{run_id[:8]}",
        "cpus_per_worker": str(
            max(1, int(plan.get("cpus_per_node", 1)) // max(1, gpus_per_node))
        ),
        "dataset": (
            manifest["datasets"][0]["name"] if manifest.get("datasets") else "dataset"
        ),
    }


def _env_overrides(
    manifest: dict[str, Any],
    data_source: dict | None,
    master_addr: str,
    master_port: int,
    node_rank: int,
    num_nodes: int,
    gpus_per_node: int,
    cpus_per_node: int,
    run_id: str,
) -> dict[str, str]:
    env_overrides = {
        "MASTER_ADDR": master_addr,
        "MASTER_PORT": str(master_port),
        "NODE_RANK": str(node_rank),
        "WORLD_SIZE": str(num_nodes * gpus_per_node),
        "RAYTRAIN_RUN_ID": run_id,
        "RAYTRAIN_NODE_RANK": str(node_rank),
        "RAYTRAIN_NUM_NODES": str(num_nodes),
        "RAYTRAIN_GPUS_PER_NODE": str(gpus_per_node),
        "RAYTRAIN_CPUS_PER_NODE": str(cpus_per_node),
        "OMP_NUM_THREADS": str(max(1, cpus_per_node // max(1, gpus_per_node))),
    }
    if data_source:
        env_overrides["RAYTRAIN_DATA_SOURCE_TYPE"] = data_source["type"]
        env_overrides["RAYTRAIN_DATA_SOURCE_URI"] = data_source["uri"]
        env_overrides["RAYTRAIN_DATA_SOURCE_VERSION"] = str(
            data_source.get("version", "latest")
        )
        env_overrides["RAYTRAIN_DATA_SOURCE_FILTER"] = data_source.get("filter", "")
        if data_source.get("columns"):
            env_overrides["RAYTRAIN_DATA_SOURCE_COLUMNS"] = ",".join(
                data_source["columns"]
            )
    env_overrides.update(manifest["launcher"].get("env") or {})
    return env_overrides


def _run_subprocess(
    cmd: list[str],
    workdir: str,
    env_overrides: dict[str, str],
    stream_prefix: str,
) -> int:
    # Same logic as NodeLauncher.run: prefer Ray-injected working_dir, falling
    # back to the workdir the driver resolved. Reuse _resolve_workdir so the
    # precedence rule lives in exactly one place.
    wd = Path(_resolve_workdir({"workdir": workdir}, {}))
    wd.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(env_overrides)
    env["RAYTRAIN_RESOLVED_WORKDIR"] = str(wd)
    print(f"[{stream_prefix}] launching in {wd}: {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(
        cmd,
        cwd=str(wd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(f"[{stream_prefix}] {line}")
        sys.stdout.flush()
    return proc.wait()


def _run_driver_side_launcher(
    manifest: dict[str, Any],
    plan: dict[str, Any],
    workdir: str,
    save_path: str,
    run_dir: Path | None,
    tracking_uri: str,
    run_id: str,
    num_nodes: int,
    gpus_per_node: int,
    cpus_per_node: int,
    data_source: dict | None,
) -> int:
    import ray

    master_addr = ray.util.get_node_ip_address()
    master_port = _pick_free_port()
    vals = _slot_values(
        plan, run_id, num_nodes, gpus_per_node, workdir, save_path,
        master_addr, master_port, 0, manifest,
    )
    cmd = build_command(manifest["launcher"], vals)
    env_overrides = _env_overrides(
        manifest, data_source, master_addr, master_port, 0,
        num_nodes, gpus_per_node, cpus_per_node, run_id,
    )
    env_overrides.setdefault("RAY_ADDRESS", os.environ.get("RAY_ADDRESS", "auto"))
    if data_source:
        print(
            f"[driver] data_source mode: {data_source['type']} @ "
            f"{data_source['uri']} (driver-side ray_train)",
            flush=True,
        )
    print(
        "[driver] launcher type ray_train: running entrypoint once in head; "
        "Ray Train/TorchTrainer will schedule GPU workers",
        flush=True,
    )

    rc = 0
    try:
        rc = _run_subprocess(cmd, workdir, env_overrides, "ray-train")
        print(f"[driver] ray_train exit code: {rc}", flush=True)
        if rc != 0:
            raise RuntimeError(f"training failed; exit code: {rc}")
        _upload_artifacts(manifest, plan, run_dir, tracking_uri)
        _mlflow_finish(tracking_uri, run_id, "FINISHED")
    except Exception:
        traceback.print_exc()
        try:
            _mlflow_finish(tracking_uri, run_id, "FAILED")
        except Exception:
            pass
        rc = rc or 1
    finally:
        time.sleep(1)
    return rc


# ---------------------------------------------------------------------------- #
# Main
# ---------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=None)
    ap.add_argument("--plan", default=None)
    ap.add_argument(
        "--from-env",
        action="store_true",
        help="Load manifest/plan from RAYTRAIN_MANIFEST_B64 / "
        "RAYTRAIN_PLAN_B64 env vars (shared/Platform path) instead of files.",
    )
    args = ap.parse_args()

    try:
        dc = _choose_driver_config(args)
    except ValueError as e:
        print(f"[driver] error: {e}", file=sys.stderr, flush=True)
        return 2
    manifest = dc.manifest
    plan = dc.plan

    run_id = plan["run_id"]
    num_nodes = int(plan["num_nodes"])
    gpus_per_node = int(plan["gpus_per_node"])
    cpus_per_node = int(plan["cpus_per_node"])
    workdir = _resolve_workdir(plan, manifest)
    save_path = plan["save_path"]
    is_remote = "://" in save_path or save_path.startswith("s3://") or save_path.startswith("minio://")
    run_dir = None if is_remote else Path(save_path)

    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI", "")
    data_source = plan.get("data_source")

    # Log code-as-submission info if applicable
    code_uri = plan.get("code_uri") or os.environ.get("RAYTRAIN_CODE_URI", "")
    code_hash = plan.get("code_hash") or os.environ.get("RAYTRAIN_CODE_HASH", "")
    # Always emit a single greppable line recording the code revision, so the
    # head pod logs can be matched against `raytrain submit`'s output and the
    # `raytrain.code_hash` MLflow tag (see Requirement 3.5 / task 2.5).
    print(_format_code_banner(code_hash), flush=True)
    if code_uri:
        print(
            f"[driver] code-as-submission active:\n"
            f"  code_uri  = {code_uri}\n"
            f"  code_hash = {code_hash[:12] if code_hash else '?'}...\n"
            f"  workdir   = {workdir}",
            flush=True,
        )
    else:
        print(f"[driver] legacy code-in-image mode, workdir = {workdir}",
              flush=True)

    # Fail early with a helpful message if Ray not reachable.
    import ray
    # Don't pass runtime_env here — the RayJob already set env vars via
    # runtimeEnvYAML, and Ray 2.54+ refuses to merge overlapping env_var keys.
    ray.init(address="auto", ignore_reinit_error=True,
             logging_level="WARNING")

    if manifest.get("launcher", {}).get("type", "native_ddp") == "ray_train":
        return _run_driver_side_launcher(
            manifest=manifest,
            plan=plan,
            workdir=workdir,
            save_path=save_path,
            run_dir=run_dir,
            tracking_uri=tracking_uri,
            run_id=run_id,
            num_nodes=num_nodes,
            gpus_per_node=gpus_per_node,
            cpus_per_node=cpus_per_node,
            data_source=data_source,
        )

    NodeLauncher = _make_node_launcher(gpus_per_node, cpus_per_node)

    from ray.util.placement_group import placement_group
    from ray.util.scheduling_strategies import PlacementGroupSchedulingStrategy

    print(f"[driver] requesting placement group: "
          f"{num_nodes} bundles of "
          f"{{GPU: {gpus_per_node}, CPU: {cpus_per_node}}} (STRICT_SPREAD)",
          flush=True)
    bundles = [{"GPU": gpus_per_node, "CPU": cpus_per_node}] * num_nodes
    pg = placement_group(bundles, strategy="STRICT_SPREAD", name=f"pg-{run_id}")
    ready = ray.wait([pg.ready()], timeout=900)
    if not ready[0]:
        raise TimeoutError(
            "placement group not ready within 15min; likely insufficient GPUs. "
            "Check Ray dashboard: ray status")

    actors = [
        NodeLauncher.options(
            scheduling_strategy=PlacementGroupSchedulingStrategy(
                placement_group=pg,
                placement_group_bundle_index=i,
            )
        ).remote()
        for i in range(num_nodes)
    ]

    ips = ray.get([a.get_ip.remote() for a in actors])
    print(f"[driver] node IPs: {ips}", flush=True)
    master_addr = ips[0]
    master_port = _pick_free_port()

    # --- dataset sync (parallel, per-node, idempotent) ---
    if data_source:
        # Ray Data mode: training subprocess reads from Lance/MinIO directly.
        # No local download needed — skip dataset_sync entirely.
        print(f"[driver] data_source mode: {data_source['type']} @ "
              f"{data_source['uri']} (skipping dataset_sync)", flush=True)
    elif manifest.get("datasets"):
        print(f"[driver] syncing {len(manifest['datasets'])} datasets on "
              f"{num_nodes} nodes in parallel", flush=True)
        sync_futs = [
            a.sync.remote(manifest["datasets"], workdir,
                          "/mnt/ray-cache/datasets")
            for a in actors
        ]
        for i, res in enumerate(ray.get(sync_futs)):
            print(f"[driver] sync[{i}]: {res}", flush=True)

    # --- build per-node command & env, then run ---
    rc = 0
    mlflow_imported = False
    try:
        futs = []
        for i in range(num_nodes):
            vals = _slot_values(
                plan, run_id, num_nodes, gpus_per_node, workdir, save_path,
                master_addr, master_port, i, manifest,
            )
            cmd = build_command(manifest["launcher"], vals)
            env_overrides = _env_overrides(
                manifest, data_source, master_addr, master_port, i,
                num_nodes, gpus_per_node, cpus_per_node, run_id,
            )

            futs.append(
                actors[i].run.remote(cmd, workdir, env_overrides, f"node{i}")
            )

        # Wait for all nodes; any non-zero rc is a failure.
        rcs = ray.get(futs)
        for i, r in enumerate(rcs):
            print(f"[driver] node{i} exit code: {r}", flush=True)
        if any(r != 0 for r in rcs):
            rc = max(rcs)
            raise RuntimeError(f"training failed; exit codes: {rcs}")

        # --- artifact upload on rank-0 node: run remotely on the Rank-0 actor! ---
        print("[driver] triggering artifact upload remotely on Node 0 (Rank-0)...", flush=True)
        ray.get(actors[0].upload_artifacts.remote(manifest, plan, save_path, tracking_uri))

        _mlflow_finish(tracking_uri, run_id, "FINISHED")
        mlflow_imported = True

    except Exception:
        traceback.print_exc()
        try:
            _mlflow_finish(tracking_uri, run_id, "FAILED")
            mlflow_imported = True
        except Exception:
            pass
        rc = rc or 1
    finally:
        if not mlflow_imported:
            # best-effort mark as killed if we die before hitting the status call
            try:
                _mlflow_finish(tracking_uri, run_id, "KILLED")
            except Exception:
                pass
        time.sleep(1)

    return rc


def _upload_artifacts(manifest: dict, plan: dict, run_dir: Path | None,
                      tracking_uri: str) -> None:
    """
    Upload artifacts to MLflow under the run. The save_path directory (where the
    repo writes checkpoints, logs, tensorboard, config.py) is ALWAYS uploaded —
    users don't have to remember to list it. Additional paths from
    manifest.artifacts are also uploaded; they may be absolute or relative to
    workdir, and may contain {run_id}/{run_name} placeholders.
    """
    try:
        import mlflow
    except ImportError:
        print("[driver] mlflow not installed; skipping artifact upload",
              flush=True)
        return

    mlflow.set_tracking_uri(tracking_uri)
    run_id = plan["run_id"]

    to_upload: list[Path] = []

    # Always: the run's save_path (checkpoints + config + logs)
    if run_dir and run_dir.exists():
        to_upload.append(run_dir)
    elif run_dir:
        print(f"[driver] note: save_path {run_dir} missing, nothing to upload",
              flush=True)
    else:
        print("[driver] save_path is remote/S3; skipping local upload",
              flush=True)

    # Plus whatever the manifest asks for (skip duplicates of save_path)
    subs = {"run_id": run_id, "run_name": plan.get("config_name", "run")}
    for p in manifest.get("artifacts") or []:
        try:
            rendered = p.format(**subs)
        except KeyError:
            rendered = p
        ap = Path(rendered)
        if not ap.is_absolute():
            ap = Path(manifest["workdir"]) / ap
        if run_dir and ap.resolve() == run_dir.resolve():
            continue
        if not ap.exists():
            print(f"[driver] manifest artifact missing, skip: {ap}", flush=True)
            continue
        to_upload.append(ap)

    if not to_upload:
        print("[driver] no artifacts to upload", flush=True)
        return

    with mlflow.start_run(run_id=run_id):
        for ap in to_upload:
            if ap.is_dir():
                mlflow.log_artifacts(str(ap), artifact_path=ap.name)
            else:
                mlflow.log_artifact(str(ap))
            print(f"[driver] uploaded artifact: {ap}", flush=True)


def _mlflow_finish(tracking_uri: str, run_id: str, status: str) -> None:
    if not tracking_uri or not run_id:
        return
    try:
        import mlflow
        from mlflow.tracking import MlflowClient
    except ImportError:
        return
    mlflow.set_tracking_uri(tracking_uri)
    MlflowClient().set_terminated(run_id, status=status)


if __name__ == "__main__":
    sys.exit(main())
