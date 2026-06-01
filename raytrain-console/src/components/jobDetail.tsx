import { useEffect, useMemo, useState } from "react";
import {
  Check,
  X,
  Loader,
  Download,
  Search,
  Play,
  Pause,
  AlertTriangle,
  Activity,
  ChevronRight,
  FileBox,
  FileText,
  Brain,
  BarChart3,
} from "lucide-react";
import {
  LineChart,
  Line,
  ResponsiveContainer,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
} from "recharts";
import type { Job, MetricSeries } from "../lib/types";
import { fmtAge, fmtClock } from "../lib/format";
import { Panel } from "./primitives";
import {
  fetchJobLogs,
  fetchJobMetrics,
  type LogLineResp,
  type MetricSeriesResp,
} from "../lib/consoleApi";
import { errMsg } from "../lib/api";
import { useI18n } from "../i18n";

// ---------------- JobTimeline ----------------

export function JobTimeline({ job }: { job: Job }) {
  const phases = job.timeline || [];
  return (
    <div className="flex items-center">
      {phases.map((p, i) => {
        const icon =
          p.state === "done" ? <Check size={13} /> : p.state === "error" ? <X size={13} /> : p.state === "current" ? <Loader size={13} className="animate-spin" /> : <span className="h-1.5 w-1.5 rounded-full bg-current" />;
        const tone =
          p.state === "done"
            ? "border-succeeded bg-succeeded/15 text-succeeded"
            : p.state === "error"
            ? "border-failed bg-failed/15 text-failed"
            : p.state === "current"
            ? "border-brand bg-brand/15 text-brand"
            : "border-borderc bg-panel text-ink3";
        return (
          <div key={p.key} className="flex flex-1 items-center last:flex-none">
            <div className="flex flex-col items-center gap-1">
              <span className={`flex h-7 w-7 items-center justify-center rounded-full border ${tone}`}>{icon}</span>
              <span className="whitespace-nowrap text-[11px] text-ink2">{p.label}</span>
              <span className="whitespace-nowrap text-[10px] text-ink3">
                {p.at ? fmtClock(p.at) : "—"}
              </span>
            </div>
            {i < phases.length - 1 && (
              <div className={`mx-2 mb-7 h-px flex-1 ${p.state === "done" ? "bg-succeeded/40" : "bg-border"}`} />
            )}
          </div>
        );
      })}
    </div>
  );
}

// ---------------- LogViewer ----------------

const LEVEL_COLOR: Record<string, string> = {
  INFO: "text-ink2",
  WARN: "text-queued",
  ERROR: "text-failed",
  DEBUG: "text-ink3",
};

const LOG_CONTAINERS = ["all", "ray-head", "worker-0", "worker-1", "submitter"];

export function LogViewer({ job }: { job: Job }) {
  const { t } = useI18n();
  const [container, setContainer] = useState("all");
  const [follow, setFollow] = useState(false);
  const [q, setQ] = useState("");
  const [lines, setLines] = useState<LogLineResp[]>([]);
  const [source, setSource] = useState<"loki" | "unavailable" | "">("");
  const [reason, setReason] = useState("");
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");

  const load = useMemo(
    () => () => {
      setLoading(true);
      setErr("");
      fetchJobLogs(job.id, container)
        .then((r) => {
          setLines(r.lines || []);
          setSource(r.source);
          setReason(r.reason || "");
        })
        .catch((e) => setErr(errMsg(e)))
        .finally(() => setLoading(false));
    },
    [job.id, container]
  );

  useEffect(() => {
    load();
  }, [load]);

  // follow: poll every 3s while enabled
  useEffect(() => {
    if (!follow) return;
    const id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, [follow, load]);

  const shown = lines.filter(
    (l) => !q || (l.text || "").toLowerCase().includes(q.toLowerCase())
  );

  const download = () => {
    const text = lines.map((l) => `${l.ts || ""} ${l.container || ""} ${l.level || ""} ${l.text}`).join("\n");
    const blob = new Blob([text], { type: "text/plain" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${job.name || job.id}.log`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <div className="flex items-center gap-1 rounded-md border border-border bg-panel p-0.5">
          {LOG_CONTAINERS.map((c) => (
            <button
              key={c}
              onClick={() => setContainer(c)}
              className={`rounded px-2 py-1 text-xs transition-colors ${
                container === c ? "bg-panel2 text-ink" : "text-ink3 hover:text-ink2"
              }`}
            >
              {c}
            </button>
          ))}
        </div>
        <div className="relative w-48">
          <Search size={13} className="absolute left-2 top-1/2 -translate-y-1/2 text-ink3" />
          <input className="input py-1 pl-7 text-xs" placeholder={t("log.search")} value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        <button className={`btn btn-sm ${follow ? "btn-primary" : ""}`} onClick={() => setFollow((v) => !v)}>
          {follow ? <Pause size={12} /> : <Play size={12} />}
          {follow ? t("log.following") : t("log.follow")}
        </button>
        <button className="btn btn-sm" onClick={download} disabled={lines.length === 0}>
          <Download size={12} /> {t("log.download")}
        </button>
        {source === "loki" && (
          <span className="ml-auto flex items-center gap-1 text-xs text-ink3">
            <Activity size={12} /> {t("log.source")}: {t("log.fromLoki")}
          </span>
        )}
      </div>

      {err && (
        <div className="mb-2 flex items-center gap-2 rounded-md border border-failed/30 bg-failed/10 px-3 py-2 text-xs text-failed">
          <AlertTriangle size={13} /> {err}
        </div>
      )}

      {loading ? (
        <div className="py-10 text-center text-ink3"><Loader size={16} className="mx-auto animate-spin" /></div>
      ) : source === "unavailable" ? (
        <div className="rounded-md border border-border bg-panel2 px-4 py-8 text-center text-[13px] text-ink3">
          <AlertTriangle size={18} className="mx-auto mb-2 text-queued" />
          {t("log.unavailable")}
          {reason && <div className="mt-1 text-xs">{reason}</div>}
        </div>
      ) : (
        <div className="max-h-[440px] overflow-auto rounded-md border border-border bg-[#0b0f14] p-3 font-mono text-[12px] leading-relaxed">
          {shown.map((l, i) => {
            const level = (l.level || "INFO").toUpperCase();
            return (
              <div key={i} className="flex gap-3 whitespace-pre-wrap px-1">
                {l.ts && <span className="flex-shrink-0 text-ink3">{fmtClock(l.ts)}</span>}
                {l.container && <span className="flex-shrink-0 text-violet-300/70">{l.container}</span>}
                <span className={`flex-shrink-0 ${LEVEL_COLOR[level] || "text-ink2"}`}>{level}</span>
                <span className={LEVEL_COLOR[level] || "text-ink2"}>{l.text}</span>
              </div>
            );
          })}
          {shown.length === 0 && <div className="text-ink3">{t("log.none")}</div>}
        </div>
      )}
    </div>
  );
}

// ---------------- EventTimeline ----------------

export function EventTimeline({ job }: { job: Job }) {
  const events = job.events || [];
  if (events.length === 0) {
    return (
      <div className="py-8 text-center text-[13px] text-ink3">
        {job.pods_source === "unavailable" || !job.live
          ? "任务未真实提交到集群，暂无 K8s 事件"
          : "暂无事件"}
      </div>
    );
  }
  return (
    <ul className="space-y-0">
      {events.map((e, i) => (
        <li key={i} className="flex gap-3 border-b border-border/50 py-2.5 last:border-0">
          <span
            className={`mt-1 flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-full ${
              e.type === "Warning" ? "bg-failed/15 text-failed" : "bg-succeeded/15 text-succeeded"
            }`}
          >
            {e.type === "Warning" ? <AlertTriangle size={11} /> : <Check size={11} />}
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className={`chip ${e.type === "Warning" ? "border-failed/40 bg-failed/10 text-failed" : "border-borderc bg-panel2 text-ink2"}`}>
                {e.reason}
              </span>
              <span className="font-mono text-xs text-ink3">{e.object}</span>
              {e.raw && e.raw !== e.reason && (
                <span className="font-mono text-[10px] text-ink3">(k8s: {e.raw})</span>
              )}
            </div>
            <p className="mt-1 text-[13px] text-ink2">{e.message}</p>
          </div>
          <span className="flex-shrink-0 text-xs text-ink3">{fmtClock(e.ts)}</span>
        </li>
      ))}
    </ul>
  );
}

// ---------------- PodTable ----------------

export function PodTable({ job }: { job: Job }) {
  const { t } = useI18n();
  const [sel, setSel] = useState<string | null>(null);
  const phaseColor: Record<string, string> = {
    Running: "text-running",
    Pending: "text-queued",
    Succeeded: "text-succeeded",
    Failed: "text-failed",
    Terminating: "text-ink3",
  };
  const pods = job.pods || [];
  if (job.pods_source !== "k8s" || pods.length === 0) {
    return (
      <div className="rounded-md border border-border bg-panel2 px-4 py-10 text-center text-[13px] text-ink3">
        <AlertTriangle size={18} className="mx-auto mb-2 text-queued" />
        {job.pods_source === "unavailable" || !job.live
          ? "任务未真实提交到集群，暂无 Pod 信息（Pod 在任务运行于 Ray 集群后显示）"
          : t("common.empty")}
      </div>
    );
  }
  return (
    <div>
      <table className="w-full text-[13px]">
        <thead>
          <tr className="border-b border-border text-left text-xs text-ink3">
            <th className="py-2 font-medium">Pod</th>
            <th className="py-2 font-medium">Role</th>
            <th className="py-2 font-medium">Phase</th>
            <th className="py-2 font-medium">Node</th>
            <th className="py-2 text-right font-medium">Restarts</th>
            <th className="py-2 text-right font-medium">GPU</th>
            <th className="py-2 text-right font-medium">Age</th>
            <th className="py-2 font-medium">IP</th>
          </tr>
        </thead>
        <tbody>
          {pods.map((p) => (
            <>
              <tr
                key={p.name}
                onClick={() => setSel(sel === p.name ? null : p.name)}
                className="cursor-pointer border-b border-border/50 hover:bg-panel2"
              >
                <td className="py-2.5 font-mono text-xs text-ink">
                  <span className="inline-flex items-center gap-1">
                    <ChevronRight size={12} className={`text-ink3 transition-transform ${sel === p.name ? "rotate-90" : ""}`} />
                    {p.name}
                  </span>
                </td>
                <td className="py-2.5 text-ink2">{p.role}</td>
                <td className={`py-2.5 ${phaseColor[p.phase]}`}>{p.phase}</td>
                <td className="py-2.5 font-mono text-xs text-ink3">{p.node}</td>
                <td className={`py-2.5 text-right tabular-nums ${p.restarts > 0 ? "text-queued" : "text-ink3"}`}>{p.restarts}</td>
                <td className="py-2.5 text-right tabular-nums text-ink2">{p.gpu || "—"}</td>
                <td className="py-2.5 text-right tabular-nums text-ink3">{fmtAge(p.ageSec)}</td>
                <td className="py-2.5 font-mono text-xs text-ink3">{p.ip}</td>
              </tr>
              {sel === p.name && (
                <tr key={p.name + "-d"} className="bg-panel2/50">
                  <td colSpan={8} className="px-6 py-3 text-xs text-ink2">
                    <div className="grid grid-cols-2 gap-x-8 gap-y-1">
                      <span className="text-ink3">last event:</span>
                      <span>{p.lastEvent}</span>
                      <span className="text-ink3">container:</span>
                      <span className="font-mono">{p.role === "head" ? "ray-head" : p.role === "submitter" ? "submitter" : "ray-worker"}</span>
                      <span className="text-ink3">image:</span>
                      <span className="font-mono">{job.image}</span>
                    </div>
                  </td>
                </tr>
              )}
            </>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------- MetricsPanel ----------------

function Chart({
  title,
  data,
  unit,
  color,
}: {
  title: string;
  data: MetricSeries[];
  unit: string;
  color: string;
}) {
  return (
    <Panel title={`${title} ${unit ? `(${unit})` : ""}`} bodyClass="p-2">
      <div className="h-40">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 8, right: 12, left: -16, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#283039" />
            <XAxis dataKey="t" tick={{ fontSize: 10, fill: "#697585" }} interval="preserveStartEnd" />
            <YAxis tick={{ fontSize: 10, fill: "#697585" }} />
            <Tooltip
              contentStyle={{ background: "#161b22", border: "1px solid #323b46", borderRadius: 6, fontSize: 12 }}
              labelStyle={{ color: "#9aa7b4" }}
            />
            <Line type="monotone" dataKey="value" stroke={color} strokeWidth={1.5} dot={false} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </Panel>
  );
}

const METRIC_META: Record<string, { titleKey: string; color: string }> = {
  gpu_util: { titleKey: "metrics.gpuUtil", color: "#3b82f6" },
  gpu_mem: { titleKey: "metrics.gpuMem", color: "#22c55e" },
  throughput: { titleKey: "metrics.throughput", color: "#06b6d4" },
};

export function MetricsPanel({ job }: { job: Job }) {
  const { t } = useI18n();
  const [series, setSeries] = useState<MetricSeriesResp[]>([]);
  const [source, setSource] = useState<"prometheus" | "unavailable" | "">("");
  const [reason, setReason] = useState("");
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setErr("");
    fetchJobMetrics(job.id)
      .then((r) => {
        if (!alive) return;
        setSeries(r.series || []);
        setSource(r.source);
        setReason(r.reason || "");
      })
      .catch((e) => alive && setErr(errMsg(e)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [job.id]);

  if (loading) {
    return <div className="py-10 text-center text-ink3"><Loader size={16} className="mx-auto animate-spin" /></div>;
  }
  if (err) {
    return (
      <div className="flex items-center justify-center gap-2 py-10 text-[13px] text-failed">
        <AlertTriangle size={15} /> {err}
      </div>
    );
  }

  const populated = series.filter((s) => s.points.length > 0);
  if (source !== "prometheus" || populated.length === 0) {
    return (
      <div className="rounded-md border border-border bg-panel2 px-4 py-10 text-center text-[13px] text-ink3">
        <AlertTriangle size={18} className="mx-auto mb-2 text-queued" />
        {source === "unavailable" ? t("metrics.unavailable") : t("metrics.none")}
        {reason && <div className="mt-1 text-xs">{reason}</div>}
      </div>
    );
  }

  return (
    <div>
      <div className="mb-2 flex items-center gap-1 text-xs text-ink3">
        <Activity size={12} /> {t("log.source")}: {t("metrics.fromProm")}
      </div>
      <div className="grid grid-cols-2 gap-3">
        {series.map((s) => {
          const meta = METRIC_META[s.metric] || { titleKey: s.metric, color: "#8b5cf6" };
          if (s.points.length === 0) return null;
          return (
            <Chart
              key={s.metric}
              title={t(meta.titleKey)}
              unit={s.unit}
              color={meta.color}
              data={s.points as unknown as MetricSeries[]}
            />
          );
        })}
      </div>
    </div>
  );
}

// ---------------- ConfigPreview ----------------

export function ConfigPreview({ job }: { job: Job }) {
  const { t } = useI18n();
  const [showYaml, setShowYaml] = useState(false);
  return (
    <div className="space-y-4">
      <Panel title={t("jd.userConfig")}>
        <div className="overflow-hidden rounded-md border border-border">
          <table className="w-full text-[13px]">
            <tbody>
              {[
                ["Entrypoint", job.entrypoint],
                ["Working dir", job.workingDir],
                ["Image", job.image],
                ["Git ref", job.gitRef || "—"],
                ["Dataset", `${job.mounts.dataset.uri} → ${job.mounts.dataset.path} (${job.mounts.dataset.mode})`],
                ["Checkpoint", `${job.mounts.checkpoint.uri} → ${job.mounts.checkpoint.path} (${job.mounts.checkpoint.mode})`],
                ["Scratch", `${job.mounts.scratch.path} · ${job.mounts.scratch.sizeGi} GiB`],
              ].map(([k, v]) => (
                <tr key={k} className="border-b border-border/60 last:border-0">
                  <td className="w-40 bg-panel2 px-3 py-2 text-xs text-ink3">{k}</td>
                  <td className="px-3 py-2 font-mono text-xs text-ink2">{v}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel title={t("jd.envVars")}>
        <div className="flex flex-wrap gap-1.5">
          {job.env.map((e) => (
            <span key={e.key} className="chip border-borderc bg-panel2 font-mono text-xs text-ink2">
              {e.key}={e.value}
            </span>
          ))}
        </div>
      </Panel>

      <div>
        <button className="flex items-center gap-2 text-[13px] text-brand hover:underline" onClick={() => setShowYaml((v) => !v)}>
          <ChevronRight size={14} className={showYaml ? "rotate-90 transition-transform" : "transition-transform"} />
          {t("jd.rendered")}
        </button>
        {showYaml && (
          <pre className="mt-2 max-h-96 overflow-auto rounded-md border border-border bg-[#0b0f14] p-3 font-mono text-[11px] leading-relaxed text-ink2">
            {job.rayJobYaml}
          </pre>
        )}
      </div>
    </div>
  );
}

// ---------------- Artifacts tab ----------------

export function ArtifactsTab({ job }: { job: Job }) {
  const icon = { checkpoint: FileBox, model: Brain, log: FileText, eval: BarChart3 };
  const groups = ["checkpoint", "model", "log", "eval"] as const;
  if ((job.artifacts || []).length === 0) {
    return (
      <div className="rounded-md border border-border bg-panel2 px-4 py-10 text-center text-[13px] text-ink3">
        <AlertTriangle size={18} className="mx-auto mb-2 text-queued" />
        {job.artifacts_source === "unavailable" || !job.live
          ? "暂无产物：任务未真实运行，或 checkpoint 路径不是对象存储（s3://）"
          : "任务尚未产生 artifacts"}
      </div>
    );
  }
  return (
    <div className="space-y-4">
      {groups.map((g) => {
        const items = (job.artifacts || []).filter((a) => a.kind === g);
        if (items.length === 0) return null;
        const Icon = icon[g];
        return (
          <div key={g}>
            <div className="mb-2 flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-ink3">
              <Icon size={13} /> {g}
            </div>
            <div className="overflow-hidden rounded-md border border-border">
              <table className="w-full text-[13px]">
                <tbody>
                  {items.map((a) => (
                    <tr key={a.path} className="border-b border-border/60 last:border-0 hover:bg-panel2">
                      <td className="px-3 py-2 font-medium text-ink">{a.name}</td>
                      <td className="px-3 py-2 font-mono text-xs text-ink3">{a.path}</td>
                      <td className="px-3 py-2 text-right tabular-nums text-ink2">{a.size}</td>
                      <td className="px-3 py-2 text-right">
                        <button className="btn btn-sm">
                          <Download size={12} /> Download
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        );
      })}
    </div>
  );
}
