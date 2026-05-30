"""
Smoke test: ensure the RayJob template renders into valid YAML with dummy inputs.
Run with `python -m raytrain.tests.test_render` or pytest.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

# Allow running directly: `python raytrain/tests/test_render.py`
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from raytrain.manifest import (
    DatasetMount, DataSource, Launcher, Manifest, Resources,
)
from raytrain.rayjob import (
    Plan, RenderInputs, creds_secret_body, payload_configmap_body,
    render_rayjob,
)
from raytrain.user_config import MinioConfig, MlflowConfig, UserConfig


def build_dummy():
    manifest = Manifest(
        api_version="raytrain/v1",
        image="172.31.9.104:5050/pointcept:latest",
        workdir="/workspace/pointcept",
        launcher=Launcher(
            type="native_ddp",
            entrypoint="tools/train.py",
            args=[
                "--config-file={config}",
                "--num-gpus={num_gpus_per_node}",
                "--num-machines={num_nodes}",
                "--machine-rank={node_rank}",
                "--dist-url=tcp://{master_addr}:{master_port}",
                "--options",
                "save_path={save_path}",
            ],
        ),
        resources=Resources(
            gpus_per_node=8, cpus_per_node=32,
            memory_per_node="256Gi", shm_size="32Gi",
        ),
        datasets=[
            DatasetMount(name="scannet",
                         s3="s3://pointcept-data/scannet",
                         mount="data/scannet"),
        ],
        artifacts=[],
        repo_name="pointcept",
    )
    user_cfg = UserConfig(
        user_name="zhangsan",
        namespace="ray-cluster-3",
        minio=MinioConfig(endpoint="http://172.31.16.3:30950",
                          access_key="AK", secret_key="SK"),
        mlflow=MlflowConfig(
            tracking_uri="http://mlflow.mlflow.svc.cluster.local:5000",
            username="zhangsan", password="pw"),
    )
    plan = Plan(
        job_name="zhangsan-pointcept-semseg-ptv3-250512-083000",
        run_id="deadbeefcafebabe1234567890abcdef",
        user="zhangsan",
        repo_name="pointcept",
        num_nodes=2,
        gpus_per_node=8,
        cpus_per_node=32,
        gpu_type="h20",
        config_name="semseg-pt-v3m1-0-base",
        config_path="configs/scannet/semseg-pt-v3m1-0-base.py",
        save_path="/mnt/ray-cache/exp/zhangsan/deadbeefcafebabe1234567890abcdef",
        datasets=[{"name": "scannet", "s3": "s3://pointcept-data/scannet",
                   "mount": "data/scannet", "read_only": True}],
        workdir="/workspace/pointcept",
    )
    return manifest, user_cfg, plan


def test_render_produces_valid_yaml():
    manifest, user_cfg, plan = build_dummy()

    cm = payload_configmap_body(manifest, plan)
    secret = creds_secret_body(user_cfg, plan.job_name)
    rj_text = render_rayjob(RenderInputs(
        manifest=manifest,
        user_cfg=user_cfg,
        plan=plan,
        image=manifest.image,
        service_account="default",
        payload_configmap=cm["metadata"]["name"],
    ))

    # Parse all three: must be valid YAML
    rj = next(yaml.safe_load_all(rj_text))
    assert rj["kind"] == "RayJob", rj
    assert rj["metadata"]["namespace"] == "ray-cluster-3"
    assert rj["spec"]["shutdownAfterJobFinishes"] is True
    head = rj["spec"]["rayClusterSpec"]["headGroupSpec"]["template"]["spec"]
    wg = rj["spec"]["rayClusterSpec"]["workerGroupSpecs"][0]
    assert wg["replicas"] == 2
    assert wg["template"]["spec"]["nodeSelector"]["gpu"] == "h20"
    # worker must request exactly gpus_per_node GPUs
    c = wg["template"]["spec"]["containers"][0]
    assert c["resources"]["limits"]["nvidia.com/gpu"] == "8"

    # The two ConfigMaps/Secrets must also be renderable & valid
    assert cm["kind"] == "ConfigMap"
    assert "manifest.yaml" in cm["data"] and "plan.yaml" in cm["data"]
    assert secret["kind"] == "Secret"
    for k in ("aws_access_key_id", "aws_secret_access_key",
              "mlflow_username", "mlflow_password"):
        assert k in secret["data"]

    # NCCL hint env present
    env = {e.get("name"): e.get("value") for e in c["env"] if "value" in e}
    assert env.get("NCCL_IB_DISABLE") == "1"
    assert env.get("NCCL_SOCKET_IFNAME")

    # No cpu-workers group when cpu_workers=0 (default)
    worker_groups = rj["spec"]["rayClusterSpec"]["workerGroupSpecs"]
    cpu_groups = [w for w in worker_groups if w["groupName"] == "cpu-workers"]
    assert len(cpu_groups) == 0, "cpu-workers should NOT be present when cpu_workers=0"

    print("OK: rendered RayJob YAML parses; worker nodeSelector=h20, 8 GPU, "
          "shm 32Gi, hostPath /data4/ray-cache mounted, no cpu-workers")


def test_launcher_args_substitute():
    """The launcher arg template should substitute at driver runtime, not at
    render time. So the raw args in ConfigMap should still contain the
    placeholders (for portability)."""
    manifest, _, plan = build_dummy()
    cm = payload_configmap_body(manifest, plan)
    import yaml as _y
    m = _y.safe_load(cm["data"]["manifest.yaml"])
    args = m["launcher"]["args"]
    assert any("{master_addr}" in a for a in args)
    assert any("{node_rank}" in a for a in args)
    print("OK: launcher args contain placeholders for driver-time substitution")


def test_launcher_builders():
    from raytrain.entrypoint.launchers import build_command
    vals = {
        "master_addr": "10.0.0.1", "master_port": "29500",
        "node_rank": "1", "num_nodes": "2",
        "num_gpus_per_node": "8", "world_size": "16",
        "config": "configs/scannet/x.py",
        "save_path": "/mnt/ray-cache/exp/x",
        "run_id": "abc", "workdir": "/workspace/pointcept",
        "run_name": "x", "dataset": "scannet", "cpus_per_worker": "4",
    }
    native = build_command({
        "type": "native_ddp",
        "entrypoint": "tools/train.py",
        "args": ["--config-file={config}",
                 "--num-gpus={num_gpus_per_node}",
                 "--machine-rank={node_rank}",
                 "--dist-url=tcp://{master_addr}:{master_port}"],
    }, vals)
    assert native[0:2] == ["python", "tools/train.py"]
    assert "--config-file=configs/scannet/x.py" in native
    assert "--num-gpus=8" in native
    assert "--machine-rank=1" in native
    assert "--dist-url=tcp://10.0.0.1:29500" in native

    tr = build_command({
        "type": "torchrun", "entrypoint": "train.py",
        "args": ["--cfg={config}"],
    }, vals)
    assert tr[0] == "torchrun"
    assert "--nnodes=2" in tr and "--nproc_per_node=8" in tr
    assert "--node_rank=1" in tr and "--master_addr=10.0.0.1" in tr
    assert "--cfg=configs/scannet/x.py" in tr

    rt = build_command({
        "type": "ray_train",
        "entrypoint": "tools/train_ray.py",
        "args": ["--config", "{config}", "--num-workers", "{world_size}",
                 "--cpus-per-worker", "{cpus_per_worker}"],
    }, vals)
    assert rt[:2] == ["python", "tools/train_ray.py"]
    assert rt[-6:] == [
        "--config", "configs/scannet/x.py",
        "--num-workers", "16",
        "--cpus-per-worker", "4",
    ]

    print("OK: launchers native_ddp/torchrun/ray_train substitute placeholders")


# ====================================================================
# NEW: Ray Data + Lance integration tests
# ====================================================================

def test_manifest_accepts_ray_train_launcher():
    """Manifest accepts the driver-side Ray Train launcher."""
    import tempfile, os
    content = """\
apiVersion: raytrain/v1
image: test:latest
workdir: /workspace/pointcept
launcher:
  type: ray_train
  entrypoint: tools/train_ray.py
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(content)
        f.flush()
        path = f.name
    try:
        m = Manifest.load(path)
    finally:
        os.unlink(path)

    assert m.launcher.type == "ray_train"
    print("OK: manifest accepts ray_train launcher")

def test_manifest_with_data_source():
    """Manifest with data_source parses correctly."""
    import tempfile, os
    content = """\
apiVersion: raytrain/v1
image: test:latest
workdir: /workspace
launcher:
  type: native_ddp
  entrypoint: train.py
data_source:
  type: lance
  uri: s3://bucket/dataset.lance
  version: "3"
  filter: "split == 'train'"
  columns:
    - coord
    - segment
cpu_workers: 4
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(content)
        f.flush()
        path = f.name
    try:
        m = Manifest.load(path)
    finally:
        os.unlink(path)

    assert m.data_source is not None
    assert m.data_source.type == "lance"
    assert m.data_source.uri == "s3://bucket/dataset.lance"
    assert m.data_source.version == "3"
    assert m.data_source.filter == "split == 'train'"
    assert m.data_source.columns == ["coord", "segment"]
    assert m.cpu_workers == 4
    assert m.datasets == []  # should be empty when using data_source

    print("OK: manifest with data_source parses correctly")


def test_manifest_rejects_datasets_with_data_source():
    """A repo must choose local dataset sync OR Ray Data streaming, not both."""
    import tempfile, os
    import pytest
    content = """\
apiVersion: raytrain/v1
image: test:latest
workdir: /workspace
launcher:
  type: native_ddp
  entrypoint: train.py
datasets:
  - {name: scannet, s3: s3://bucket/scannet, mount: data/scannet}
data_source:
  type: lance
  uri: s3://bucket/dataset.lance
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(content)
        f.flush()
        path = f.name
    try:
        with pytest.raises(ValueError, match="datasets.*data_source"):
            Manifest.load(path)
    finally:
        os.unlink(path)

    print("OK: manifest rejects datasets and data_source together")


def test_render_with_cpu_workers():
    """RayJob template renders cpu-worker group when cpu_workers > 0."""
    manifest, user_cfg, plan = build_dummy()
    plan.cpu_workers = 4
    plan.data_source = {"type": "lance", "uri": "s3://test/ds.lance"}

    cm = payload_configmap_body(manifest, plan)
    rj_text = render_rayjob(RenderInputs(
        manifest=manifest,
        user_cfg=user_cfg,
        plan=plan,
        image=manifest.image,
        service_account="default",
        payload_configmap=cm["metadata"]["name"],
    ))
    rj = next(yaml.safe_load_all(rj_text))
    worker_specs = rj["spec"]["rayClusterSpec"]["workerGroupSpecs"]
    cpu_group = [w for w in worker_specs if w["groupName"] == "cpu-workers"]
    assert len(cpu_group) == 1, "cpu-workers group should be present"
    assert cpu_group[0]["replicas"] == 4
    assert cpu_group[0]["maxReplicas"] == 8
    # cpu-worker must NOT request GPUs
    cpu_params = cpu_group[0]["rayStartParams"]
    assert cpu_params["num-gpus"] == "0"
    # cpu-worker container should not have nvidia.com/gpu
    cpu_container = cpu_group[0]["template"]["spec"]["containers"][0]
    assert "nvidia.com/gpu" not in str(cpu_container.get("resources", {}))

    print("OK: cpu-worker group rendered when cpu_workers > 0")


def test_payload_configmap_includes_data_source():
    """ConfigMap includes data_source when set on manifest."""
    manifest, _, plan = build_dummy()
    manifest.data_source = DataSource(
        type="lance", uri="s3://test/ds.lance", version="5",
        filter="split == 'val'", columns=["coord"],
    )
    cm = payload_configmap_body(manifest, plan)
    import yaml as _y
    m = _y.safe_load(cm["data"]["manifest.yaml"])
    assert m["data_source"] is not None
    assert m["data_source"]["type"] == "lance"
    assert m["data_source"]["uri"] == "s3://test/ds.lance"
    assert m["data_source"]["version"] == "5"
    print("OK: payload configmap includes data_source")


def test_custom_save_path_resolution():
    """Manifest with custom save_path templates parses correctly."""
    import tempfile, os
    content = """\
apiVersion: raytrain/v1
image: test:latest
workdir: /workspace
launcher:
  type: native_ddp
  entrypoint: train.py
save_path: s3://personal-bucket/exp/{user}/{run_id}
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(content)
        f.flush()
        path = f.name
    try:
        m = Manifest.load(path)
    finally:
        os.unlink(path)

    assert m.save_path == "s3://personal-bucket/exp/{user}/{run_id}"
    print("OK: manifest with custom save_path template parses correctly")


# ====================================================================
# M0: code-as-submission (working_dir + code_uri / code_hash)
# ====================================================================


def test_manifest_default_code_sync():
    """A manifest without an explicit code_sync block defaults to enabled."""
    import tempfile, os
    content = """\
apiVersion: raytrain/v1
image: test:latest
workdir: /workspace
launcher:
  type: native_ddp
  entrypoint: train.py
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(content)
        f.flush()
        path = f.name
    try:
        m = Manifest.load(path)
    finally:
        os.unlink(path)

    assert m.code_sync.enabled is True
    assert m.code_sync.bucket == "raytrain-code"
    assert m.code_sync.max_size_mib == 200
    assert m.code_sync.dedup is False
    print("OK: manifest default code_sync = enabled")


def test_manifest_code_sync_disabled_via_bool():
    """Shorthand `code_sync: false` disables it."""
    import tempfile, os
    content = """\
apiVersion: raytrain/v1
image: test:latest
workdir: /workspace
launcher:
  type: native_ddp
  entrypoint: train.py
code_sync: false
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(content)
        f.flush()
        path = f.name
    try:
        m = Manifest.load(path)
    finally:
        os.unlink(path)

    assert m.code_sync.enabled is False
    print("OK: code_sync: false disables it")


def test_manifest_code_sync_with_overrides():
    """Mapping form supports per-field overrides."""
    import tempfile, os
    content = """\
apiVersion: raytrain/v1
image: test:latest
workdir: /workspace
launcher:
  type: native_ddp
  entrypoint: train.py
code_sync:
  enabled: true
  bucket: my-team-code
  extra_excludes:
    - "outputs/"
    - "wandb/"
  max_size_mib: 100
  dedup: true
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(content)
        f.flush()
        path = f.name
    try:
        m = Manifest.load(path)
    finally:
        os.unlink(path)

    cs = m.code_sync
    assert cs.enabled is True
    assert cs.bucket == "my-team-code"
    assert cs.extra_excludes == ["outputs/", "wandb/"]
    assert cs.max_size_mib == 100
    assert cs.dedup is True
    print("OK: code_sync mapping overrides")


def test_render_with_code_sync():
    """Plan.code_uri set → template includes runtimeEnvYAML.working_dir."""
    manifest, user_cfg, plan = build_dummy()
    plan.code_uri = "s3://raytrain-code/zhangsan/zhangsan-pointcept-foo.zip"
    plan.code_hash = "abc123def456" + "0" * 52  # 64 hex chars
    plan.code_size_bytes = 91234567

    cm = payload_configmap_body(manifest, plan)
    rj_text = render_rayjob(RenderInputs(
        manifest=manifest,
        user_cfg=user_cfg,
        plan=plan,
        image=manifest.image,
        service_account="default",
        payload_configmap=cm["metadata"]["name"],
    ))
    rj = next(yaml.safe_load_all(rj_text))

    runtime_env_yaml = rj["spec"]["runtimeEnvYAML"]
    assert "working_dir:" in runtime_env_yaml
    assert plan.code_uri in runtime_env_yaml
    assert "setup_timeout_seconds: 600" in runtime_env_yaml
    assert "RAYTRAIN_CODE_URI" in runtime_env_yaml
    assert "RAYTRAIN_CODE_HASH" in runtime_env_yaml
    assert plan.code_hash in runtime_env_yaml

    # Worker pod env_vars should also include the code metadata
    wg = rj["spec"]["rayClusterSpec"]["workerGroupSpecs"][0]
    container = wg["template"]["spec"]["containers"][0]
    env = {e.get("name"): e.get("value") for e in container["env"] if "value" in e}
    assert env.get("RAYTRAIN_CODE_URI") == plan.code_uri
    assert env.get("RAYTRAIN_CODE_HASH") == plan.code_hash
    assert env.get("RAYTRAIN_CODE_SIZE_BYTES") == str(plan.code_size_bytes)

    print("OK: render with code_sync injects working_dir + env vars")


def test_render_without_code_sync():
    """No code_uri → no working_dir block (legacy path preserved)."""
    manifest, user_cfg, plan = build_dummy()
    # plan.code_uri remains None / empty

    cm = payload_configmap_body(manifest, plan)
    rj_text = render_rayjob(RenderInputs(
        manifest=manifest,
        user_cfg=user_cfg,
        plan=plan,
        image=manifest.image,
        service_account="default",
        payload_configmap=cm["metadata"]["name"],
    ))
    rj = next(yaml.safe_load_all(rj_text))

    runtime_env_yaml = rj["spec"]["runtimeEnvYAML"]
    assert "working_dir:" not in runtime_env_yaml
    # Legacy env vars still present
    assert "RAYTRAIN_USER:" in runtime_env_yaml
    assert "RAYTRAIN_JOB_NAME:" in runtime_env_yaml
    # New env vars NOT injected when no code_uri
    assert "RAYTRAIN_CODE_URI" not in runtime_env_yaml

    print("OK: legacy render without code_sync preserved")


def test_plan_to_yaml_includes_code_fields():
    """Plan.to_yaml() must serialize the code-as-submission fields so the
    head-pod driver can read them from plan.yaml in the payload ConfigMap."""
    _, _, plan = build_dummy()
    plan.code_uri = "s3://raytrain-code/zhangsan/zhangsan-pointcept-foo.zip"
    plan.code_hash = "abc123def456" + "0" * 52  # 64 hex chars
    plan.code_size_bytes = 91234567

    parsed = yaml.safe_load(plan.to_yaml())

    assert parsed["code_uri"] == plan.code_uri
    assert parsed["code_hash"] == plan.code_hash
    assert parsed["code_size_bytes"] == plan.code_size_bytes
    print("OK: Plan.to_yaml includes code_uri / code_hash / code_size_bytes")


if __name__ == "__main__":
    test_render_produces_valid_yaml()
    test_launcher_args_substitute()
    test_launcher_builders()
    test_manifest_accepts_ray_train_launcher()
    test_manifest_with_data_source()
    test_render_with_cpu_workers()
    test_payload_configmap_includes_data_source()
    test_custom_save_path_resolution()
    test_manifest_default_code_sync()
    test_manifest_code_sync_disabled_via_bool()
    test_manifest_code_sync_with_overrides()
    test_render_with_code_sync()
    test_render_without_code_sync()
    test_plan_to_yaml_includes_code_fields()
    print("\nall checks passed")
