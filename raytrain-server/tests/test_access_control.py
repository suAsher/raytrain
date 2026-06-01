"""
Shared access-control decisions (core.access_control.enforce_submit):
account-enabled + grants + quota, used by BOTH /v1/jobs and /v1/console/jobs.
"""
from __future__ import annotations

import pytest

from raytrain_server.core.access_control import AccessDenied, SubmitAsk, enforce_submit
from raytrain_server.core.users import UserQuota, UserRecord


def _user(**kw) -> UserRecord:
    base = dict(user="alice", tenant="t", role="user", quota=UserQuota())
    base.update(kw)
    return UserRecord(**base)


def test_no_record_is_unrestricted():
    # bootstrap-friendly: no user table yet → don't block
    enforce_submit(None, SubmitAsk(gpus=64), current_gpus=0, current_jobs=0)


def test_disabled_blocked_even_for_admin():
    rec = _user(role="admin", enabled=False)
    with pytest.raises(AccessDenied) as ei:
        enforce_submit(rec, SubmitAsk(), current_gpus=0, current_jobs=0)
    assert ei.value.code == "ACCOUNT_DISABLED"


def test_admin_bypasses_grants_and_quota():
    rec = _user(role="admin", quota=UserQuota(max_gpus=1), projects=["only-this"])
    enforce_submit(rec, SubmitAsk(project="anything", gpus=999),
                   current_gpus=0, current_jobs=0)  # no raise


def test_project_grant_enforced():
    rec = _user(projects=["pointcept"])
    with pytest.raises(AccessDenied) as ei:
        enforce_submit(rec, SubmitAsk(project="secret"), current_gpus=0, current_jobs=0)
    assert ei.value.code == "PROJECT_FORBIDDEN"
    # granted project passes
    enforce_submit(rec, SubmitAsk(project="pointcept"), current_gpus=0, current_jobs=0)


def test_image_prefix_enforced():
    rec = _user(image_prefixes=["raytrain/"])
    with pytest.raises(AccessDenied) as ei:
        enforce_submit(rec, SubmitAsk(image="evil/x:latest"), current_gpus=0, current_jobs=0)
    assert ei.value.code == "IMAGE_FORBIDDEN"
    enforce_submit(rec, SubmitAsk(image="raytrain/pointcept:v3"),
                   current_gpus=0, current_jobs=0)


def test_quota_enforced_with_current_usage():
    rec = _user(quota=UserQuota(max_gpus=8))
    # 6 already used + ask 4 = 10 > 8 → denied
    with pytest.raises(AccessDenied) as ei:
        enforce_submit(rec, SubmitAsk(gpus=4), current_gpus=6, current_jobs=0)
    assert ei.value.code == "QUOTA_EXCEEDED"
    # 6 + 2 = 8 == cap → ok
    enforce_submit(rec, SubmitAsk(gpus=2), current_gpus=6, current_jobs=0)


def test_empty_grants_unrestricted():
    rec = _user()  # no projects/queues/image_prefixes, default quota (unlimited)
    enforce_submit(rec, SubmitAsk(project="x", queue="q", image="any/i", gpus=100),
                   current_gpus=0, current_jobs=0)
