import { useMemo, useState } from "react";
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
  Legend,
} from "recharts";
import type { Job, LogLine, MetricSeries } from "../lib/types";
import { fmtAge, fmtClock } from "../lib/format";
import { Panel } from "./primitives";

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

const CONTAINER_LEVEL_COLOR: Record<LogLine["level"], string> = {
  INFO: "text-ink2",
  WARN: "text-queued",
  ERROR: "text-failed",
  DEBUG: "text-ink3",
};

export function LogViewer({ job }: { job: Job }) {
  const containers = useMemo(() => {
    const set = new Set<string>(["all"]);
    (job.logs || []).forEach((l) => set.add(l.container));
    return Array.from(set);
  }, [job.logs]);

  const [container, setContainer] = useState("all");
  const [follow, setFollow] = useState(job.status === "Running");
  const [q, setQ] = useState("");
  // When the job is live (real Ray submission), fetch real logs from the
  // server's streaming endpoint; otherwise show the derived demo logs.
  const [liveText, setLiveText] = useState<string | null>(null);
  const isLive = Boolean(job.live);
  const loadLive = () => {
    fetch(`/v1/console/jobs/${job.id}/logs`, {
      headers: { Authorization: `Bearer ${localStorage.getItem("raytrain.console.token") || ""}` },
    })
      .then((r) => r.text())
      .then(setLiveText)
      .catch(() => setLiveText(null));
  };

  const errorAnchor = job.failure?.logAnchor;

  const lines = (job.logs || []).filter(
    (l) => (container === "all" || l.container === container) && (!q || l.text.toLowerCase().includes(q.toLowerCase()))
  );

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <div className="flex items-center gap-1 rounded-md border border-border bg-panel p-0.5">
          {containers.map((c) => (
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
          <input className="input py-1 pl-7 text-xs" placeholder="grep logs…" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        <button className={`btn btn-sm ${follow ? "btn-primary" : ""}`} onClick={() => setFollow((v) => !v)}>
          {follow ? <Pause size={12} /> : <Play size={12} />}
          {follow ? "Following" : "Follow"}
        </button>
        <button className="btn btn-sm">
          <Download size={12} /> Download
        </button>
        {isLive && (
          <button className="btn btn-sm" onClick={loadLive} title="从 Ray 拉取真实日志">
            <Activity size={12} /> 实时日志
          </button>
        )}
        {job.failure && (
          <span className="ml-auto flex items-center gap-1 text-xs text-failed">
            <AlertTriangle size={12} /> 已跳转到错误附近
          </span>
        )}
      </div>
      {liveText !== null ? (
        <pre className="max-h-[440px] overflow-auto rounded-md border border-border bg-[#0b0f14] p-3 font-mono text-[12px] leading-relaxed text-ink2 whitespace-pre-wrap">
          {liveText || "（暂无日志输出）"}
        </pre>
      ) : (
      <div className="max-h-[440px] overflow-auto rounded-md border border-border bg-[#0b0f14] p-3 font-mono text-[12px] leading-relaxed">
        {lines.map((l, i) => {
          const isErr = errorAnchor != null && i >= errorAnchor - 1 && l.level === "ERROR";
          return (
            <div
              key={i}
              className={`flex gap-3 whitespace-pre-wrap px-1 ${isErr ? "rounded bg-failed/10" : ""}`}
            >
              <span className="flex-shrink-0 text-ink3">{fmtClock(l.ts)}</span>
              <span className="flex-shrink-0 text-violet-300/70">{l.container}</span>
              <span className={`flex-shrink-0 ${CONTAINER_LEVEL_COLOR[l.level]}`}>{l.level}</span>
              <span className={CONTAINER_LEVEL_COLOR[l.level]}>{l.text}</span>
            </div>
          );
        })}
        {lines.length === 0 && <div className="text-ink3">没有日志输出</div>}
      </div>
      )}
    </div>
  );
}

// ---------------- EventTimeline ----------------

export function EventTimeline({ job }: { job: Job }) {
  return (
    <ul className="space-y-0">
      {(job.events || []).map((e, i) => (
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
      {(job.events || []).length === 0 && <li className="py-6 text-center text-ink3">暂无事件</li>}
    </ul>
  );
}

// ---------------- PodTable ----------------

export function PodTable({ job }: { job: Job }) {
  const [sel, setSel] = useState<string | null>(null);
  const phaseColor: Record<string, string> = {
    Running: "text-running",
    Pending: "text-queued",
    Succeeded: "text-succeeded",
    Failed: "text-failed",
    Terminating: "text-ink3",
  };
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
          {(job.pods || []).map((p) => (
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
  keys,
  unit,
  colors,
}: {
  title: string;
  data: MetricSeries[];
  keys: string[];
  unit: string;
  colors: string[];
}) {
  return (
    <Panel title={`${title} ${unit ? `(${unit})` : ""}`} bodyClass="p-2">
      <div className="h-40">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 8, right: 12, left: -16, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#283039" />
            <XAxis dataKey="t" tick={{ fontSize: 10, fill: "#697585" }} interval={5} />
            <YAxis tick={{ fontSize: 10, fill: "#697585" }} />
            <Tooltip
              contentStyle={{ background: "#161b22", border: "1px solid #323b46", borderRadius: 6, fontSize: 12 }}
              labelStyle={{ color: "#9aa7b4" }}
            />
            {keys.length > 1 && <Legend wrapperStyle={{ fontSize: 11 }} />}
            {keys.map((k, i) => (
              <Line key={k} type="monotone" dataKey={k} stroke={colors[i]} strokeWidth={1.5} dot={false} isAnimationActive={false} />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </Panel>
  );
}

export function MetricsPanel({ job }: { job: Job }) {
  const m = job.metrics;
  if (!m || (m.cpu.length === 0 && m.gpuUtil.length === 0)) {
    return <div className="py-10 text-center text-ink3">任务尚未运行，暂无指标数据</div>;
  }
  return (
    <div className="grid grid-cols-2 gap-3">
      <Chart title="GPU Utilization" unit="%" data={m.gpuUtil} keys={["worker-0", "worker-1"]} colors={["#3b82f6", "#22c55e"]} />
      <Chart title="GPU Memory" unit="GiB" data={m.gpuMem} keys={["worker-0", "worker-1"]} colors={["#3b82f6", "#22c55e"]} />
      <Chart title="CPU" unit="%" data={m.cpu} keys={["value"]} colors={["#8b5cf6"]} />
      <Chart title="Memory" unit="%" data={m.mem} keys={["value"]} colors={["#f59e0b"]} />
      <Chart title="Ray Object Store" unit="GiB" data={m.objStore} keys={["value"]} colors={["#06b6d4"]} />
      <Chart title="Throughput" unit="it/s" data={m.throughput} keys={["value"]} colors={["#22c55e"]} />
    </div>
  );
}

// ---------------- ConfigPreview ----------------

export function ConfigPreview({ job }: { job: Job }) {
  const [showYaml, setShowYaml] = useState(false);
  return (
    <div className="space-y-4">
      <Panel title="用户提交配置">
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

      <Panel title="Environment variables">
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
          平台渲染后的 RayJob manifest（默认折叠）
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
    return <div className="py-10 text-center text-ink3">任务尚未产生 artifacts</div>;
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
