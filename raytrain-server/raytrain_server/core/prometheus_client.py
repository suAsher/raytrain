"""
PrometheusClient — query a job's real GPU/throughput metrics from the cluster's
Prometheus (Req 10).

Queries are constrained to the job's pods (label match on the submission id) so
returned series belong only to that job. No data → empty series (flagged), not
fabricated. Read failure → PromUnavailable → FriendlyError.

Injectable (Protocol + HTTP impl + Fake) for tests.
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


class PromUnavailable(Exception):
    """Raised when Prometheus cannot be queried."""


@dataclass
class MetricSeries:
    metric: str                       # gpu_util | gpu_mem | throughput
    unit: str
    points: list[dict] = field(default_factory=list)   # [{t: "HH:MM", value: float}]
    source: str = "prometheus"        # or "unavailable" when no data

    def to_dict(self) -> dict:
        return {
            "metric": self.metric, "unit": self.unit,
            "points": self.points, "source": self.source,
        }


# PromQL templates; {sid} is the submission id used as a pod label match.
_QUERIES = {
    "gpu_util": ('DCGM_FI_DEV_GPU_UTIL{{pod=~"{sid}.*"}}', "%"),
    "gpu_mem": ('DCGM_FI_DEV_FB_USED{{pod=~"{sid}.*"}}', "MiB"),
    "throughput": ('rate(raytrain_samples_total{{job_submission_id="{sid}"}}[1m])', "samples/s"),
}


class PrometheusClient(Protocol):
    def job_metrics(self, submission_id: str, start: int, end: int, step: int) -> list[MetricSeries]: ...


class HttpPrometheusClient:
    def __init__(self, base_url: str, timeout_s: int = 10):
        self._base = base_url.rstrip("/")
        self._timeout = timeout_s

    def _query_range(self, promql: str, start: int, end: int, step: int) -> list[dict]:
        params = {"query": promql, "start": str(start), "end": str(end), "step": str(step)}
        url = f"{self._base}/api/v1/query_range?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise PromUnavailable(f"Prometheus query failed: {exc!r}") from exc
        if data.get("status") != "success":
            raise PromUnavailable(f"Prometheus error: {data.get('error', 'unknown')}")
        return data.get("data", {}).get("result", [])

    def job_metrics(self, submission_id, start, end, step) -> list[MetricSeries]:
        out: list[MetricSeries] = []
        for name, (tmpl, unit) in _QUERIES.items():
            promql = tmpl.format(sid=submission_id)
            result = self._query_range(promql, start, end, step)
            points: list[dict] = []
            # average across matching series per timestamp (keep it simple)
            agg: dict[int, list[float]] = {}
            for series in result:
                for ts, val in series.get("values", []):
                    try:
                        agg.setdefault(int(ts), []).append(float(val))
                    except (TypeError, ValueError):
                        pass
            for ts in sorted(agg):
                label = time.strftime("%H:%M", time.localtime(ts))
                vals = agg[ts]
                points.append({"t": label, "value": round(sum(vals) / len(vals), 2)})
            out.append(MetricSeries(
                metric=name, unit=unit, points=points,
                source="prometheus" if points else "unavailable",
            ))
        return out


class FakePrometheusClient:
    def __init__(self, series: list[MetricSeries] | None = None, fail: bool = False):
        self._series = series
        self._fail = fail

    def job_metrics(self, submission_id, start, end, step) -> list[MetricSeries]:
        if self._fail:
            raise PromUnavailable("fake prom failure")
        if self._series is not None:
            return self._series
        # default: one populated + one empty (to exercise both branches)
        return [
            MetricSeries("gpu_util", "%", [{"t": "10:00", "value": 88.0}], "prometheus"),
            MetricSeries("gpu_mem", "MiB", [], "unavailable"),
            MetricSeries("throughput", "samples/s", [{"t": "10:00", "value": 3.2}], "prometheus"),
        ]


_prom_client: PrometheusClient | None = None


def get_prometheus_client() -> PrometheusClient | None:
    global _prom_client
    if _prom_client is None:
        from .settings import get_settings

        s = get_settings()
        if not s.prometheus_url:
            return None
        _prom_client = HttpPrometheusClient(s.prometheus_url)
    return _prom_client


def set_prometheus_client(c: PrometheusClient | None) -> None:
    global _prom_client
    _prom_client = c
