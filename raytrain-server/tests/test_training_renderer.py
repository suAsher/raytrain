"""Tests for the training RayJob renderer + validation + state aggregation."""
from __future__ import annotations

import json

import pytest

from raytrain_server.training import errors as E
from raytrain_server.training import labels as L
from raytrain_server.training.domain import (
    CheckpointConfig,
    DatasetMount,
    JobState,
    MountMode,
    ResourceSpec,
    TrainingJob,
)
from raytrain_server.training.renderer import (
    CHECKPOINT_PATH,
    DATA_PATH,
    RDMA_RESOURCE,
    SCRATCH_PATH,
    render_rayjob,
)
from raytrain_server.training.state import (
    ClusterSignals,
    aggregate_state,
    attribute_failure,
)
from raytrain_server.training.validate import (
    QuotaView,
    validate_all,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _job(**over) -> TrainingJob:
    base = dict(
        name="ptv3-smoke",
        creator="zhangsan",
        creator_id="u-1",
        project="occ",
        tenant="occ-team",
        quota_group="occ-quota",
        queue="occ-h20",
        namespace="raytrain-jobs",
        image="reg/pointcept:latest",
        command="python tools/train.py --config configs/x.py",
        job_id="job-abc",
        run_id="run-abc",
    )
    base.update(over)
    return TrainingJob(**base)


# --------------------------------------------------------------------------- #
# renderer: single GPU
# --------------------------------------------------------------------------- #


class TestRenderSingleGpu:
    def test_basic_shape(self):
        job = _job(resources=ResourceSpec(gpu_type="h20", nodes=1, gpus_per_node=1))
        rj = render_rayjob(job)
        assert rj["apiVersion"] == "ray.io/v1"
        assert rj["kind"] == "RayJob"
        assert rj["spec"]["submissionMode"] == "K8sJobMode"
        assert rj["spec"]["shutdownAfterJobFinishes"] is True
        assert rj["spec"]["entrypoint"].startswith("python tools/train.py")

    def test_worker_requests_one_gpu(self):
        job = _job(resources=ResourceSpec(nodes=1, gpus_per_node=1))
        rj = render_rayjob(job)
        wg = rj["spec"]["rayClusterSpec"]["workerGroupSpecs"][0]
        assert wg["replicas"] == 1
        c = wg["template"]["spec"]["containers"][0]
        assert c["resources"]["requests"]["nvidia.com/gpu"] == "1"

    def test_head_is_cpu_only(self):
        job = _job()
        rj = render_rayjob(job)
        head = rj["spec"]["rayClusterSpec"]["headGroupSpec"]
        assert head["rayStartParams"]["num-gpus"] == "0"
        hc = head["template"]["spec"]["containers"][0]
        assert "nvidia.com/gpu" not in hc["resources"]["requests"]

    def test_gpu_affinity_present(self):
        job = _job(resources=ResourceSpec(gpu_type="h20", nodes=1, gpus_per_node=1))
        rj = render_rayjob(job)
        wg = rj["spec"]["rayClusterSpec"]["workerGroupSpecs"][0]
        aff = wg["template"]["spec"]["affinity"]["nodeAffinity"]
        term = aff["requiredDuringSchedulingIgnoredDuringExecution"][
            "nodeSelectorTerms"
        ][0]["matchExpressions"][0]
        assert term["key"] == L.NODE_GPU_LABEL
        assert term["values"] == ["h20"]

    def test_single_node_disables_ib(self):
        job = _job(resources=ResourceSpec(nodes=1, gpus_per_node=1))
        rj = render_rayjob(job)
        wg = rj["spec"]["rayClusterSpec"]["workerGroupSpecs"][0]
        env = {e["name"]: e["value"] for e in wg["template"]["spec"]["containers"][0]["env"]}
        assert env["NCCL_IB_DISABLE"] == "1"
        assert env["TRAIN_NODES"] == "1"
        assert env["TRAIN_GPUS_PER_NODE"] == "1"
        assert env["TRAIN_DATA_PATH"] == DATA_PATH
        assert env["TRAIN_CHECKPOINT_PATH"] == CHECKPOINT_PATH
        # spilling config is valid JSON pointing at scratch
        spill = json.loads(env["RAY_object_spilling_config"])
        assert spill["params"]["directory_path"] == SCRATCH_PATH

    def test_scratch_emptydir_present(self):
        job = _job()
        rj = render_rayjob(job)
        wg = rj["spec"]["rayClusterSpec"]["workerGroupSpecs"][0]
        vols = {v["name"]: v for v in wg["template"]["spec"]["volumes"]}
        assert "scratch" in vols and "emptyDir" in vols["scratch"]


# --------------------------------------------------------------------------- #
# renderer: multi-node
# --------------------------------------------------------------------------- #


class TestRenderMultiNode:
    def test_replicas_match_nodes(self):
        job = _job(
            resources=ResourceSpec(nodes=2, gpus_per_node=8),
            checkpoint=CheckpointConfig(uri="s3://b/ck", mode=MountMode.RWX),
        )
        rj = render_rayjob(job)
        wg = rj["spec"]["rayClusterSpec"]["workerGroupSpecs"][0]
        assert wg["replicas"] == 2
        c = wg["template"]["spec"]["containers"][0]
        assert c["resources"]["requests"]["nvidia.com/gpu"] == "8"

    def test_rdma_and_ib_enabled(self):
        job = _job(
            resources=ResourceSpec(nodes=2, gpus_per_node=8),
            checkpoint=CheckpointConfig(uri="s3://b/ck"),
        )
        rj = render_rayjob(job)
        wg = rj["spec"]["rayClusterSpec"]["workerGroupSpecs"][0]
        c = wg["template"]["spec"]["containers"][0]
        assert c["resources"]["requests"][RDMA_RESOURCE] == "1"
        env = {e["name"]: e["value"] for e in c["env"]}
        assert env["NCCL_IB_DISABLE"] == "0"


# --------------------------------------------------------------------------- #
# renderer: CPU-only
# --------------------------------------------------------------------------- #


def test_render_cpu_only():
    job = _job(resources=ResourceSpec(nodes=1, gpus_per_node=0))
    rj = render_rayjob(job)
    wg = rj["spec"]["rayClusterSpec"]["workerGroupSpecs"][0]
    c = wg["template"]["spec"]["containers"][0]
    assert "nvidia.com/gpu" not in c["resources"]["requests"]
    # no GPU → no affinity block
    assert "affinity" not in wg["template"]["spec"]


# --------------------------------------------------------------------------- #
# renderer: labels / annotations
# --------------------------------------------------------------------------- #


class TestRenderLabels:
    def test_reserved_labels_present(self):
        job = _job(priority=5)
        rj = render_rayjob(job)
        lbl = rj["metadata"]["labels"]
        assert lbl[L.LABEL_CREATOR] == "zhangsan"
        assert lbl[L.LABEL_PROJECT] == "occ"
        assert lbl[L.LABEL_QUOTA_GROUP] == "occ-quota"
        assert lbl[L.LABEL_GPU_TYPE] == "h20"
        assert lbl[L.LABEL_QUEUE] == "occ-h20"
        assert lbl[L.LABEL_PRIORITY] == "5"
        assert lbl[L.KUEUE_QUEUE_NAME] == "occ-h20"

    def test_user_labels_cannot_override_reserved(self):
        # user tries to spoof creator; platform value must win
        job = _job(labels={"team": "ml", L.LABEL_CREATOR: "fake"})
        rj = render_rayjob(job)
        assert rj["metadata"]["labels"][L.LABEL_CREATOR] == "zhangsan"
        assert rj["metadata"]["labels"]["team"] == "ml"

    def test_annotations_have_resource_summary(self):
        job = _job(resources=ResourceSpec(nodes=2, gpus_per_node=8))
        rj = render_rayjob(job)
        ann = rj["metadata"]["annotations"]
        assert ann[L.ANNO_NODES] == "2"
        assert ann[L.ANNO_GPUS_PER_NODE] == "8"
        assert ann[L.ANNO_NUM_WORKERS] == "2"


# --------------------------------------------------------------------------- #
# renderer: checkpoint / dataset mounts
# --------------------------------------------------------------------------- #


class TestRenderMounts:
    def test_pvc_checkpoint_mounted(self):
        job = _job(
            checkpoint=CheckpointConfig(
                uri="pvc://ckpt-claim", mount_path="/checkpoints", mode=MountMode.RWX
            )
        )
        rj = render_rayjob(job)
        wg = rj["spec"]["rayClusterSpec"]["workerGroupSpecs"][0]
        vols = {v["name"]: v for v in wg["template"]["spec"]["volumes"]}
        assert vols["checkpoints"]["persistentVolumeClaim"]["claimName"] == "ckpt-claim"

    def test_dataset_pvc_readonly(self):
        job = _job(
            datasets=[
                DatasetMount(name="scannet", uri="pvc://scannet", mount_path="/data", mode=MountMode.RO)
            ]
        )
        rj = render_rayjob(job)
        wg = rj["spec"]["rayClusterSpec"]["workerGroupSpecs"][0]
        mounts = wg["template"]["spec"]["containers"][0]["volumeMounts"]
        data = [m for m in mounts if m.get("mountPath") == "/data"][0]
        assert data["readOnly"] is True


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #


class TestValidation:
    def test_ok_single_node(self):
        validate_all(_job())  # no raise

    def test_reserved_label_rejected(self):
        job = _job(labels={L.LABEL_PROJECT: "spoof"})
        with pytest.raises(E.PlatformError) as ex:
            validate_all(job)
        assert ex.value.code == E.ERR_RESERVED_LABEL

    def test_missing_image(self):
        job = _job(image="")
        with pytest.raises(E.PlatformError) as ex:
            validate_all(job)
        assert ex.value.code == E.ERR_MISSING_FIELD

    def test_multinode_requires_checkpoint(self):
        job = _job(resources=ResourceSpec(nodes=2, gpus_per_node=8))
        with pytest.raises(E.PlatformError) as ex:
            validate_all(job)
        assert ex.value.code == E.ERR_CHECKPOINT_REQUIRED_MULTINODE

    def test_multinode_rwo_pvc_blocked(self):
        job = _job(
            resources=ResourceSpec(nodes=2, gpus_per_node=8),
            checkpoint=CheckpointConfig(uri="pvc://ck", mode=MountMode.RWO),
        )
        with pytest.raises(E.PlatformError) as ex:
            validate_all(job)
        assert ex.value.code == E.ERR_PVC_RWO_MULTINODE

    def test_multinode_rwx_pvc_ok(self):
        job = _job(
            resources=ResourceSpec(nodes=2, gpus_per_node=8),
            checkpoint=CheckpointConfig(uri="pvc://ck", mode=MountMode.RWX),
        )
        validate_all(job)  # no raise

    def test_quota_exceeded(self):
        job = _job(resources=ResourceSpec(nodes=2, gpus_per_node=8))  # 16 gpus
        job.checkpoint = CheckpointConfig(uri="s3://b/ck")
        with pytest.raises(E.PlatformError) as ex:
            validate_all(job, quota=QuotaView(gpu_limit=8, gpu_used=0))
        assert ex.value.code == E.ERR_QUOTA_EXCEEDED

    def test_quota_ok(self):
        job = _job(resources=ResourceSpec(nodes=1, gpus_per_node=4))
        validate_all(job, quota=QuotaView(gpu_limit=8, gpu_used=0))

    def test_image_allowlist(self):
        job = _job(image="evil/x:latest")
        with pytest.raises(E.PlatformError) as ex:
            validate_all(job, allowed_images=["reg/"])
        assert ex.value.code == E.ERR_IMAGE_NOT_ALLOWED

    def test_invalid_gpus_per_node(self):
        job = _job(resources=ResourceSpec(nodes=1, gpus_per_node=16))
        with pytest.raises(E.PlatformError) as ex:
            validate_all(job)
        assert ex.value.code == E.ERR_INVALID_RESOURCE


# --------------------------------------------------------------------------- #
# state aggregation
# --------------------------------------------------------------------------- #


class TestStateAggregation:
    def test_queued_when_not_admitted(self):
        s = ClusterSignals(kueue_admitted=False)
        assert aggregate_state(s) == JobState.QUEUED

    def test_admitted_no_pods(self):
        s = ClusterSignals(kueue_admitted=True, pod_phases=[])
        assert aggregate_state(s) == JobState.ADMITTED

    def test_starting_partial_pods(self):
        s = ClusterSignals(
            kueue_admitted=True, pod_phases=["Pending", "Running"], expected_workers=2
        )
        assert aggregate_state(s) == JobState.STARTING

    def test_running_all_pods(self):
        s = ClusterSignals(
            kueue_admitted=True,
            pod_phases=["Running", "Running"],
            expected_workers=2,
            rayjob_status="RUNNING",
        )
        assert aggregate_state(s) == JobState.RUNNING

    def test_succeeded(self):
        s = ClusterSignals(rayjob_status="SUCCEEDED")
        assert aggregate_state(s) == JobState.SUCCEEDED

    def test_failed(self):
        s = ClusterSignals(rayjob_status="FAILED")
        assert aggregate_state(s) == JobState.FAILED

    def test_cancelling(self):
        s = ClusterSignals(cancel_requested=True, pod_phases=["Running"])
        assert aggregate_state(s) == JobState.CANCELLING

    def test_cancelled_when_pods_gone(self):
        s = ClusterSignals(cancel_requested=True, pod_phases=[])
        assert aggregate_state(s) == JobState.CANCELLED

    def test_cleaning(self):
        s = ClusterSignals(deleting=True)
        assert aggregate_state(s) == JobState.CLEANING

    def test_unknown(self):
        s = ClusterSignals()
        assert aggregate_state(s) == JobState.UNKNOWN


class TestFailureAttribution:
    def test_image_pull(self):
        r = attribute_failure(ClusterSignals(), ["Back-off pulling image ImagePullBackOff"])
        assert r and r.code == "IMAGE_PULL_FAILED"

    def test_insufficient_gpu(self):
        r = attribute_failure(ClusterSignals(), ["0/5 nodes: Insufficient nvidia.com/gpu"])
        assert r and r.code == "INSUFFICIENT_GPU"

    def test_oom(self):
        r = attribute_failure(ClusterSignals(), ["Container OOMKilled"])
        assert r and r.code == "OOM"

    def test_unschedulable(self):
        r = attribute_failure(ClusterSignals(), ["FailedScheduling: unschedulable"])
        assert r and r.code == "UNSCHEDULABLE"

    def test_no_attribution(self):
        assert attribute_failure(ClusterSignals(), ["random log line"]) is None
