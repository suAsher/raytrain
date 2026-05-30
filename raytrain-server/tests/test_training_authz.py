"""Tests for training workbench authorization."""
from __future__ import annotations

import pytest

from raytrain_server.training import errors as E
from raytrain_server.training.authz import (
    Principal,
    Role,
    authorize_create,
    can_view_job,
)
from raytrain_server.training.domain import DatasetMount, MountMode, TrainingJob


def _job(**over) -> TrainingJob:
    base = dict(
        name="j", creator="zhangsan", creator_id="u-1", project="occ",
        tenant="occ-team", quota_group="occ-quota", queue="occ-h20",
        namespace="ns", image="reg/pointcept:latest",
        command="python train.py",
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


# --------------------------------------------------------------------------- #
# visibility
# --------------------------------------------------------------------------- #


class TestCanView:
    def test_owner_can_view(self):
        p = _principal()
        assert can_view_job(p, job_creator_id="u-1", job_project="x", job_quota_group="y")

    def test_non_owner_cannot(self):
        p = _principal(user_id="u-2")
        assert not can_view_job(p, job_creator_id="u-1", job_project="x", job_quota_group="y")

    def test_platform_admin_sees_all(self):
        p = _principal(role=Role.PLATFORM_ADMIN, user_id="u-9")
        assert can_view_job(p, job_creator_id="u-1", job_project="x", job_quota_group="y")

    def test_project_admin_sees_project(self):
        p = _principal(user_id="u-9", role=Role.PROJECT_ADMIN, admin_projects={"occ"})
        assert can_view_job(p, job_creator_id="u-1", job_project="occ", job_quota_group="y")
        assert not can_view_job(p, job_creator_id="u-1", job_project="nlp", job_quota_group="y")

    def test_quota_admin_sees_group(self):
        p = _principal(user_id="u-9", role=Role.QUOTA_ADMIN, admin_quota_groups={"occ-quota"})
        assert can_view_job(p, job_creator_id="u-1", job_project="x", job_quota_group="occ-quota")


# --------------------------------------------------------------------------- #
# create authorization
# --------------------------------------------------------------------------- #


class TestAuthorizeCreate:
    def test_ok(self):
        authorize_create(_principal(), _job())  # no raise

    def test_project_forbidden(self):
        p = _principal(projects={"other"})
        with pytest.raises(E.PlatformError) as ex:
            authorize_create(p, _job())
        assert ex.value.code == E.ERR_PROJECT_FORBIDDEN

    def test_queue_forbidden(self):
        p = _principal(queues={"other-q"})
        with pytest.raises(E.PlatformError) as ex:
            authorize_create(p, _job())
        assert ex.value.code == E.ERR_QUEUE_FORBIDDEN

    def test_quota_group_forbidden(self):
        p = _principal(quota_groups={"other"})
        with pytest.raises(E.PlatformError) as ex:
            authorize_create(p, _job())
        assert ex.value.code == E.ERR_QUOTA_EXCEEDED

    def test_dataset_forbidden(self):
        p = _principal(datasets={"allowed-ds"})
        job = _job(datasets=[DatasetMount(name="secret", uri="pvc://x", mount_path="/data")])
        with pytest.raises(E.PlatformError) as ex:
            authorize_create(p, job)
        assert ex.value.code == E.ERR_DATASET_FORBIDDEN

    def test_image_forbidden(self):
        p = _principal(image_prefixes=["reg/allowed"])
        with pytest.raises(E.PlatformError) as ex:
            authorize_create(p, _job(image="reg/evil:latest"))
        assert ex.value.code == E.ERR_IMAGE_NOT_ALLOWED

    def test_platform_admin_bypasses(self):
        p = _principal(role=Role.PLATFORM_ADMIN, projects=set(), queues=set(), quota_groups=set())
        authorize_create(p, _job())  # no raise
