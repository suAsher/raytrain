"""
Acceptance tests for the gated per_job deprecation warning (task 10.6).

The warning is implemented as an OPT-IN / gated mechanism in
`raytrain/cli/submit.py`:

  `_maybe_warn_per_job_deprecated(resolved_mode)` prints a one-line warning to
  stderr **only when** BOTH:
    1. resolved_mode == "per_job", AND
    2. env `RAYTRAIN_PERJOB_DEPRECATED` is truthy ("1"/"true"/"yes"/"on").

  Default (flag unset/false) → no-op, so existing per_job behavior/tests are
  unchanged and per_job stays fully functional as the emergency fallback.

These cover:
  * flag set + mode==per_job  → warning emitted,
  * flag unset + mode==per_job → NO warning (default, unchanged),
  * mode==shared (flag set)    → no warning,
  * truthy parsing of the env value.

Both the tiny helper (direct) and the full `submit` per_job dry-run command
(via CliRunner, which folds stderr into result.output) are exercised.

Validates: task long-term-evolution / 10.6
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import raytrain.cli.submit as submit_mod  # noqa: E402
from raytrain.cli.submit import (  # noqa: E402
    PERJOB_DEPRECATED_ENV,
    PERJOB_DEPRECATED_WARNING,
    _is_truthy,
    _maybe_warn_per_job_deprecated,
    submit,
)
from raytrain.code_sync import CodeBundle  # noqa: E402

from test_render import build_dummy  # noqa: E402


# --------------------------------------------------------------------------- #
# helper-level unit tests (call the helper directly inside a click context)
# --------------------------------------------------------------------------- #


def _run_helper(resolved_mode):
    """Invoke the helper inside a throwaway click command and capture stderr.

    Returns the stderr text the helper wrote (empty string if nothing).
    CliRunner(mix_stderr=False) keeps stderr separate so we can assert on it
    precisely.
    """
    @click.command()
    def _probe():
        _maybe_warn_per_job_deprecated(resolved_mode)

    runner = CliRunner(mix_stderr=False)
    result = runner.invoke(_probe, [])
    assert result.exit_code == 0, result.output
    return result.stderr


def test_truthy_parsing():
    """"1"/"true"/"yes"/"on" (any case) are on; everything else is off."""
    for on in ("1", "true", "TRUE", "Yes", "on", "ON", " true "):
        assert _is_truthy(on) is True, on
    for off in (None, "", "0", "false", "no", "off", "2", "maybe", "y"):
        assert _is_truthy(off) is False, off


def test_helper_warns_when_flag_on_and_per_job(monkeypatch):
    """flag truthy + mode==per_job → warning on stderr."""
    monkeypatch.setenv(PERJOB_DEPRECATED_ENV, "1")
    stderr = _run_helper("per_job")
    assert PERJOB_DEPRECATED_WARNING in stderr, repr(stderr)


def test_helper_silent_when_flag_off_and_per_job(monkeypatch):
    """flag unset + mode==per_job → NO warning (default behavior, unchanged)."""
    monkeypatch.delenv(PERJOB_DEPRECATED_ENV, raising=False)
    stderr = _run_helper("per_job")
    assert stderr == "", repr(stderr)


def test_helper_silent_when_flag_explicitly_false(monkeypatch):
    """flag set to a non-truthy value + per_job → still no warning."""
    monkeypatch.setenv(PERJOB_DEPRECATED_ENV, "false")
    stderr = _run_helper("per_job")
    assert stderr == "", repr(stderr)


def test_helper_silent_for_shared_even_when_flag_on(monkeypatch):
    """mode==shared never warns, even with the flag on."""
    monkeypatch.setenv(PERJOB_DEPRECATED_ENV, "1")
    stderr = _run_helper("shared")
    assert stderr == "", repr(stderr)


# --------------------------------------------------------------------------- #
# command-level test: full `submit` per_job dry-run via CliRunner.
#
# Reuses the hermetic dry-run harness from tests/test_cli_submit.py
# (monkeypatch Manifest.load / UserConfig.load / create_run / build_code_zip)
# so no real cluster / MinIO / MLflow is touched.
# --------------------------------------------------------------------------- #


@pytest.fixture
def patched_submit(monkeypatch):
    manifest, user_cfg, _plan = build_dummy()
    assert manifest.code_sync.enabled is True
    # force the per_job branch
    user_cfg.default_cluster_mode = "per_job"

    monkeypatch.setattr(submit_mod.Manifest, "load",
                        staticmethod(lambda *a, **k: manifest))
    monkeypatch.setattr(submit_mod.UserConfig, "load",
                        staticmethod(lambda *a, **k: user_cfg))
    monkeypatch.setattr(
        submit_mod, "create_run",
        lambda *a, **k: SimpleNamespace(
            run_id="deadbeefcafebabe1234567890abcdef",
            experiment_id="42",
        ),
    )
    fake_bundle = CodeBundle(
        zip_path=Path("/tmp/raytrain-code-fake.zip"),
        sha256="a" * 64,
        size_bytes=12_345_678,
        file_count=7,
    )
    monkeypatch.setattr(submit_mod, "build_code_zip",
                        lambda *a, **k: fake_bundle)
    return manifest, user_cfg


def test_submit_per_job_warns_when_flag_on(patched_submit, monkeypatch):
    """Full per_job dry-run with the flag ON prints the warning.

    CliRunner defaults to mix_stderr=True, so the stderr warning shows up in
    result.output alongside the normal stdout staging.
    """
    monkeypatch.setenv(PERJOB_DEPRECATED_ENV, "1")
    runner = CliRunner()  # mix_stderr=True by default
    result = runner.invoke(
        submit,
        ["--dry-run", "--config", "configs/foo.py", "--cluster-mode", "per_job"],
    )
    assert result.exit_code == 0, result.output
    assert PERJOB_DEPRECATED_WARNING in result.output, result.output
    # still a real per_job dry-run (5-stage code-sync path) → proves we did
    # NOT remove or short-circuit per_job functionality.
    assert "/5]" in result.output, result.output


def test_submit_per_job_silent_by_default(patched_submit, monkeypatch):
    """Default (flag unset) per_job dry-run prints NO warning — unchanged."""
    monkeypatch.delenv(PERJOB_DEPRECATED_ENV, raising=False)
    runner = CliRunner()
    result = runner.invoke(
        submit,
        ["--dry-run", "--config", "configs/foo.py", "--cluster-mode", "per_job"],
    )
    assert result.exit_code == 0, result.output
    assert PERJOB_DEPRECATED_WARNING not in result.output, result.output
    # per_job still works fully.
    assert "/5]" in result.output, result.output


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
