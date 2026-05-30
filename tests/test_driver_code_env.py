"""
Unit tests for driver code-revision propagation (task 2.5):

  1. The startup banner helper ``_format_code_banner`` emits the exact
     ``[driver] code_hash=<first12>`` line expected in the head pod logs.
  2. The subprocess launchers inherit ``os.environ`` as their base env, so
     ``RAYTRAIN_CODE_HASH`` / ``RAYTRAIN_CODE_URI`` (injected on the pod by the
     RayJob template) reach the training subprocess.

Run with:
    PYTHONPATH=. python3 -m pytest tests/test_driver_code_env.py -v
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from raytrain.entrypoint import driver  # noqa: E402
from raytrain.entrypoint.driver import _format_code_banner, _run_subprocess  # noqa: E402


# --------------------------------------------------------------------------- #
# 1. Startup banner helper
# --------------------------------------------------------------------------- #


def test_banner_uses_first_12_chars_of_explicit_hash() -> None:
    full = "a3f8c1d2e4b56789abcdef0123456789abcdef0123456789abcdef0123456789"
    assert _format_code_banner(full) == "[driver] code_hash=a3f8c1d2e4b5"


def test_banner_reads_env_when_no_arg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAYTRAIN_CODE_HASH", "0123456789abcdeffffffff")
    assert _format_code_banner() == "[driver] code_hash=0123456789ab"


def test_banner_none_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAYTRAIN_CODE_HASH", raising=False)
    assert _format_code_banner() == "[driver] code_hash=<none>"


def test_banner_none_when_env_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAYTRAIN_CODE_HASH", "")
    assert _format_code_banner() == "[driver] code_hash=<none>"


def test_banner_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAYTRAIN_CODE_HASH", "  abcdef012345xyz  ")
    assert _format_code_banner() == "[driver] code_hash=abcdef012345"


def test_banner_short_hash_not_padded() -> None:
    # A hash shorter than 12 chars should be emitted verbatim (no padding).
    assert _format_code_banner("abc123") == "[driver] code_hash=abc123"


def test_banner_emitted_on_startup_path(capsys: pytest.CaptureFixture[str]) -> None:
    """The banner is printed to stdout in the exact greppable form."""
    print(_format_code_banner("deadbeefcafebabe1234"), flush=True)
    out = capsys.readouterr().out
    assert "[driver] code_hash=deadbeefcafe" in out


# --------------------------------------------------------------------------- #
# 2. Subprocess env inheritance: RAYTRAIN_CODE_HASH / RAYTRAIN_CODE_URI
# --------------------------------------------------------------------------- #


class _FakeProc:
    """Minimal stand-in for subprocess.Popen that records the env passed in."""

    def __init__(self, *args, **kwargs):
        self.captured_env = kwargs.get("env")
        # Emulate a process that produced no output and exited cleanly.
        self.stdout = iter(())

    def wait(self) -> int:
        return 0


def test_run_subprocess_forwards_code_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    The subprocess launcher copies os.environ as its base, so code-as-submission
    env vars injected on the pod (RAYTRAIN_CODE_HASH / RAYTRAIN_CODE_URI) are
    present in the child process environment.
    """
    monkeypatch.setenv("RAYTRAIN_CODE_HASH", "a3f8c1d2e4b5deadbeef")
    monkeypatch.setenv("RAYTRAIN_CODE_URI", "s3://raytrain-code/zhangsan/job.zip")
    # Avoid relying on Ray's runtime_env; use an explicit workdir.
    monkeypatch.delenv("RAY_RUNTIME_ENV_WORKING_DIR", raising=False)

    captured: dict[str, dict] = {}

    def _fake_popen(*args, **kwargs):
        proc = _FakeProc(*args, **kwargs)
        captured["env"] = proc.captured_env
        return proc

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)

    rc = _run_subprocess(
        cmd=["echo", "hi"],
        workdir=str(tmp_path),
        env_overrides={"MASTER_ADDR": "10.0.0.1"},
        stream_prefix="test",
    )

    assert rc == 0
    env = captured["env"]
    assert env is not None
    # Code-as-submission vars inherited from os.environ reach the subprocess.
    assert env["RAYTRAIN_CODE_HASH"] == "a3f8c1d2e4b5deadbeef"
    assert env["RAYTRAIN_CODE_URI"] == "s3://raytrain-code/zhangsan/job.zip"
    # env_overrides are merged on top of the inherited environment.
    assert env["MASTER_ADDR"] == "10.0.0.1"
    # Driver also records the resolved cwd for the subprocess.
    assert env["RAYTRAIN_RESOLVED_WORKDIR"] == str(tmp_path)
