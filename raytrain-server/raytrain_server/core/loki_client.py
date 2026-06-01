"""
LokiClient — query training logs from the cluster's Loki (Req 8).

Ray worker stdout is scraped by the cluster's Loki agent (Promtail/Alloy) and
labeled per pod / job. We query by the job's submission_id label so logs are
available both while running AND after the job ends (within Loki's retention) —
unlike tailing the Ray dashboard, whose logs vanish when the cluster reaps them.

Injectable (Protocol + HTTP impl + Fake) so tests need no real Loki. Read
failures raise LokiUnavailable → the API turns it into a FriendlyError; we never
fabricate logs.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

log = logging.getLogger(__name__)


class LokiUnavailable(Exception):
    """Raised when Loki cannot be queried (unconfigured / network / HTTP error)."""


@dataclass
class LogLine:
    ts: str          # ISO-ish timestamp
    container: str
    level: str       # INFO/WARN/ERROR/DEBUG (best-effort parse)
    text: str


@dataclass
class LogPage:
    lines: list[LogLine] = field(default_factory=list)
    next_cursor: str | None = None
    source: str = "loki"

    def to_dict(self) -> dict:
        return {
            "lines": [
                {"ts": l.ts, "container": l.container, "level": l.level, "text": l.text}
                for l in self.lines
            ],
            "next_cursor": self.next_cursor,
            "source": self.source,
        }


def _level_of(text: str) -> str:
    t = text.upper()
    if "ERROR" in t or "TRACEBACK" in t:
        return "ERROR"
    if "WARN" in t:
        return "WARN"
    if "DEBUG" in t:
        return "DEBUG"
    return "INFO"


class LokiClient(Protocol):
    def query_range(
        self, submission_id: str, start_ns: int, end_ns: int,
        limit: int = 1000, container: str | None = None,
    ) -> LogPage: ...


class HttpLokiClient:
    """Queries Loki's /loki/api/v1/query_range by submission_id label."""

    def __init__(self, base_url: str, namespace: str = "raytrain-shared", timeout_s: int = 10):
        self._base = base_url.rstrip("/")
        self._ns = namespace
        self._timeout = timeout_s

    def _logql(self, submission_id: str, container: str | None) -> str:
        # Match common Ray label spellings for the submission id; the cluster's
        # promtail/alloy relabels these from pod labels.
        sel = (
            '{namespace="%s"} '
            '| ray_io_job_submission_id="%s" or job_submission_id="%s"'
            % (self._ns, submission_id, submission_id)
        )
        if container:
            sel += ' | container="%s"' % container
        return sel

    def query_range(
        self, submission_id: str, start_ns: int, end_ns: int,
        limit: int = 1000, container: str | None = None,
    ) -> LogPage:
        params = {
            "query": self._logql(submission_id, container),
            "start": str(start_ns),
            "end": str(end_ns),
            "limit": str(limit),
            "direction": "forward",
        }
        url = f"{self._base}/loki/api/v1/query_range?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise LokiUnavailable(f"Loki query failed: {exc!r}") from exc

        lines: list[LogLine] = []
        last_ts_ns = None
        for stream in data.get("data", {}).get("result", []):
            labels = stream.get("stream", {}) or {}
            container_name = labels.get("container") or labels.get("pod") or "ray"
            for ts_ns, text in stream.get("values", []):
                last_ts_ns = ts_ns
                iso = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ts_ns) / 1e9)
                )
                lines.append(LogLine(iso, container_name, _level_of(text), text))
        lines.sort(key=lambda l: l.ts)
        cursor = str(int(last_ts_ns) + 1) if (last_ts_ns and len(lines) >= limit) else None
        return LogPage(lines=lines, next_cursor=cursor, source="loki")


class FakeLokiClient:
    """Test double: serves preset LogLines, or raises if fail=True."""

    def __init__(self, lines: list[LogLine] | None = None, fail: bool = False):
        self._lines = lines or []
        self._fail = fail

    def query_range(self, submission_id, start_ns, end_ns, limit=1000, container=None):
        if self._fail:
            raise LokiUnavailable("fake loki failure")
        lines = self._lines
        if container:
            lines = [l for l in lines if l.container == container]
        return LogPage(lines=list(lines[:limit]), next_cursor=None, source="loki")


_loki_client: LokiClient | None = None


def get_loki_client() -> LokiClient | None:
    """Return the configured Loki client, or None when loki_url is unset."""
    global _loki_client
    if _loki_client is None:
        from .settings import get_settings

        s = get_settings()
        if not s.loki_url:
            return None
        _loki_client = HttpLokiClient(s.loki_url)
    return _loki_client


def set_loki_client(c: LokiClient | None) -> None:
    global _loki_client
    _loki_client = c
