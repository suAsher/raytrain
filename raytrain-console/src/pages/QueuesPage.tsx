import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Layers } from "lucide-react";
import { PageHeader, Panel } from "../components/primitives";
import { StatusBadge, GpuTypeBadge } from "../components/badges";
import { fetchQueues } from "../lib/consoleApi";
import type { Queue } from "../lib/types";
import { pct } from "../lib/format";

export function QueuesPage() {
  const nav = useNavigate();
  const [queues, setQueues] = useState<Queue[]>([]);
  useEffect(() => {
    let alive = true;
    fetchQueues().then((q) => alive && setQueues(q));
    return () => {
      alive = false;
    };
  }, []);
  return (
    <div>
      <PageHeader title="Queues" subtitle="队列与资源池视角（Kueue），无需理解底层 CRD" />

      <div className="space-y-3">
        {queues.map((q) => {
          const usedPct = pct(q.used, q.nominal);
          return (
            <Panel key={q.name} bodyClass="p-0">
              <div className="flex items-center justify-between border-b border-border px-4 py-3">
                <div className="flex items-center gap-3">
                  <Layers size={16} className="text-ink3" />
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="font-semibold text-ink">{q.name}</span>
                      <GpuTypeBadge type={q.gpuType} />
                      <span
                        className={`chip ${
                          q.health === "healthy"
                            ? "border-succeeded/40 bg-succeeded/10 text-succeeded"
                            : q.health === "degraded"
                            ? "border-queued/40 bg-queued/10 text-queued"
                            : "border-failed/40 bg-failed/10 text-failed"
                        }`}
                      >
                        {q.health}
                      </span>
                    </div>
                    <div className="mt-0.5 font-mono text-xs text-ink3">ClusterQueue: {q.clusterQueue}</div>
                  </div>
                </div>
                <div className="text-right text-xs text-ink3">
                  平均等待 <span className={`font-medium ${q.avgWaitMin > 15 ? "text-queued" : "text-ink2"}`}>{q.avgWaitMin} min</span>
                </div>
              </div>

              <div className="grid grid-cols-4 divide-x divide-border">
                <Metric label="Nominal Quota" value={`${q.nominal}`} sub="GPU" />
                <Metric label="Used" value={`${q.used}`} sub={`${usedPct}%`} tone={usedPct >= 90 ? "queued" : "ink"} />
                <Metric label="Admitted" value={`${q.admitted}`} sub="jobs running" />
                <Metric label="Pending" value={`${q.pending}`} sub="waiting" tone={q.pending > 10 ? "queued" : "ink"} />
              </div>

              <div className="px-4 py-3">
                <div className="mb-2 h-2 w-full overflow-hidden rounded-full bg-panel2">
                  <div
                    className={`h-full rounded-full ${usedPct >= 90 ? "bg-failed" : usedPct >= 75 ? "bg-queued" : "bg-running"}`}
                    style={{ width: `${usedPct}%` }}
                  />
                </div>
                <div className="flex items-center gap-2 text-xs text-ink3">
                  <span>最近任务:</span>
                  {q.recentJobs.map((j) => (
                    <button
                      key={j.id}
                      onClick={() => nav(`/jobs/${j.id}`)}
                      className="flex items-center gap-1.5 rounded border border-border px-2 py-0.5 hover:bg-panel2"
                    >
                      <StatusBadge status={j.status} dot />
                      <span className="text-ink2">{j.name}</span>
                    </button>
                  ))}
                </div>
              </div>
            </Panel>
          );
        })}
      </div>
    </div>
  );
}

function Metric({ label, value, sub, tone = "ink" }: { label: string; value: string; sub: string; tone?: "ink" | "queued" }) {
  return (
    <div className="px-4 py-3">
      <div className="text-xs text-ink3">{label}</div>
      <div className={`mt-0.5 text-xl font-semibold tabular-nums ${tone === "queued" ? "text-queued" : "text-ink"}`}>{value}</div>
      <div className="text-xs text-ink3">{sub}</div>
    </div>
  );
}
