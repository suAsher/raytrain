"""Tests for TrainingService orchestration."""
from __future__ import annotations

import pytest

from raytrain_server.training import errors as E
from raytrain_server.training.authz import Principal, Role
from raytrain_server.training.domain import (
    CheckpointConfig,
    ResourceSpec,
    TrainingJob,
)
from raytrain_server.training.service import TrainingService
from raytrain_server.training.validate import QuotaView


def _job(**over) -> TrainingJob:
    base = dict(
        name="smoke", creator="x", creator_id="x", project="occ",
        tenant="occ-team", quota_group="occ-quota", queue="occ-h20",
        namespace="ns", image="reg/pointcept:latest", command="python train.py",
    )
    base.update(over)
    return TrainingJob(**base)


def _principal(**over) -> Principal:
    base = dict(
        user_id="u-1", user="zhangsan", role=Role.USER,
        projects={"occ"}, quota_groups={"occ-quota"}, queues={"occ-h20"},
    )
    base.update(over)
    return Principal(**base)


def test_dry_run_returns_rayjob_no_apply():
    applied = []
    svc = TrainingService(applier=lambda rj, ns: applied.append((rj, ns)))
    res = svc.create(_principal(), _job(), dry_run=True)
    assert res.dry_run is True
    assert res.rayjob["kind"] == "RayJob"
    assert applied == []  # nothing submitted
    assert res.job_id  # id assigned


def test_real_submit_calls_applier():
    applied = []
    svc = TrainingService(applier=lambda rj, ns: applied.append((rj, ns)))
    res = svc.create(_principal(), _job(), dry_run=False)
    assert res.dry_run is False
    assert len(applied) == 1
    rj, ns = applied[0]
    assert rj["kind"] == "RayJob"
    assert ns == "ns"


def test_creator_stamped_from_principal():
    svc = TrainingService(applier=lambda rj, ns: None)
    # client lies about creator; service overwrites from principal
    res = svc.create(_principal(), _job(creator="fake", creator_id="fake"), dry_run=True)
    assert res.rayjob["metadata"]["labels"]["raytrain.io/creator"] == "zhangsan"
    assert res.rayjob["metadata"]["labels"]["raytrain.io/creator-id"] == "u-1"


def test_authz_failure_blocks_before_render():
    svc = TrainingService(applier=lambda rj, ns: None)
    p = _principal(projects={"other"})
    with pytest.raises(E.PlatformError) as ex:
        svc.create(p, _job(), dry_run=True)
    assert ex.value.code == E.ERR_PROJECT_FORBIDDEN


def test_quota_failure():
    svc = TrainingService(applier=lambda rj, ns: None)
    job = _job(
        resources=ResourceSpec(nodes=2, gpus_per_node=8),
        checkpoint=CheckpointConfig(uri="s3://b/ck"),
    )
    with pytest.raises(E.PlatformError) as ex:
        svc.create(_principal(), job, quota=QuotaView(gpu_limit=8, gpu_used=0), dry_run=True)
    assert ex.value.code == E.ERR_QUOTA_EXCEEDED


def test_audit_called_on_dry_run():
    events = []
    svc = TrainingService(
        applier=lambda rj, ns: None,
        audit=lambda **kw: events.append(kw),
    )
    svc.create(_principal(), _job(), dry_run=True)
    assert any(e["action"] == "dry_run_job" for e in events)


def test_submit_failure_audited_and_raised():
    def boom(rj, ns):
        raise RuntimeError("api down")

    events = []
    svc = TrainingService(applier=boom, audit=lambda **kw: events.append(kw))
    with pytest.raises(E.PlatformError) as ex:
        svc.create(_principal(), _job(), dry_run=False)
    assert ex.value.code == "SUBMIT_FAILED"
    assert any(e["result"] == "error" for e in events)


def test_no_applier_real_submit_errors():
    svc = TrainingService(applier=None)
    with pytest.raises(E.PlatformError) as ex:
        svc.create(_principal(), _job(), dry_run=False)
    assert ex.value.code == "NO_APPLIER"
