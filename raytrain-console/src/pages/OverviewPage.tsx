import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { AlertTriangle, ArrowRight, Server } from "lucide-react";
import { PageHeader, Panel, Stat, QuotaUsageBar } from "../components/primitives";
import { StatusBadge, GpuTypeBadge } from "../components/badges";
import { useStore } from "../lib/store";
import { fetchOverview } from "../lib/consoleApi";
import type { Job, ResourcePool } from "../lib/types";
import { fmtRelative, fmtDuration, pct } from "../lib/format";

export function OverviewPage() {
  const { quota } = useStore();
  const nav = useNavigate();
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [pools, setPools] = useState<ResourcePool[]>([]);
  const [failed, setFailed] = useState<Job[]>([]);
  const [recent, setRecent] = useState<Job[]>([]);

  useEffect(() => {
    let alive = true;
    fetchOverview().then((o) => {
      if (!alive) return;
      setCounts(o.counts);
      setPools(o.pools);
      setFailed(o.recentFailed);
      setRecent(o.recent);
    });
    return () => {
      alive = false;
    };
  }, []);

  const count = (s: string) => counts[s] || 0;

  return (
    <div>
      <PageHeader
        title="Overview"
        subtitle="平台与当前项目的训练运行状况"
        actions={
          <button className="btn btn-primary" onClick={() => nav("/jobs/new")}>
            <span>Create Job</span>
          </button>
        }
      />

      <div className="grid grid-cols-4 gap-3">
        <Stat label="Running" value={count("Running")} tone="running" sub="正在运行" />
        <Stat label="Queued" value={count("Queued")} tone="queued" sub="排队等待准入" />
        <Stat label="Failed (24h)" value={failed.length} tone="failed" sub="近 24 小时失败" />
        <Stat label="Succeeded (24h)" value={count("Succeeded")} tone="succeeded" sub="近 24 小时完成" />
      </div>

      <div className="mt-3 grid grid-cols-3 gap-3">
        <Panel title="当前项目配额" className="col-span-1">
          <div className="space-y-3">
            <QuotaUsageBar label="GPU" used={quota.gpu.used} total={quota.gpu.total} />
            <QuotaUsageBar label="CPU" used={quota.cpu.used} total={quota.cpu.total} />
            <QuotaUsageBar label="Memory" used={quota.memGi.used} total={quota.memGi.total} unit="Gi" />
          </div>
          <div className="mt-4 flex items-center justify-between border-t border-border pt-3 text-xs">
            <span className="text-ink3">队列等待任务</span>
            <span className="font-medium text-queued">{count("Queued")} jobs</span>
          </div>
        </Panel>

        <Panel title="资源池" className="col-span-2" bodyClass="p-0">
          <table className="w-full text-[13px]">
            <thead>
              <tr className="border-b border-border text-left text-xs text-ink3">
                <th className="px-4 py-2 font-medium">Pool</th>
                <th className="px-4 py-2 font-medium">Nodes</th>
                <th className="px-4 py-2 font-medium">GPU Used</th>
                <th className="px-4 py-2 font-medium">Utilization</th>
                <th className="px-4 py-2 font-medium">Health</th>
              </tr>
            </thead>
            <tbody>
              {pools.map((p) => {
                const u = pct(p.usedGpu, p.totalGpu);
                return (
                  <tr key={p.name} className="border-b border-border/60 last:border-0">
                    <td className="px-4 py-2.5">
                      <GpuTypeBadge type={p.name} />
                    </td>
                    <td className="px-4 py-2.5 tabular-nums text-ink2">{p.nodes}</td>
                    <td className="px-4 py-2.5 tabular-nums text-ink2">
                      {p.name === "CPU-only" ? "—" : `${p.usedGpu} / ${p.totalGpu}`}
                    </td>
                    <td className="px-4 py-2.5">
                      {p.name === "CPU-only" ? (
                        <span className="text-ink3">—</span>
                      ) : (
                        <div className="flex items-center gap-2">
                          <div className="h-1.5 w-24 overflow-hidden rounded-full bg-panel2">
                            <div
                              className={`h-full rounded-full ${u >= 90 ? "bg-failed" : u >= 75 ? "bg-queued" : "bg-running"}`}
                              style={{ width: `${u}%` }}
                            />
                          </div>
                          <span className="tabular-nums text-ink3">{u}%</span>
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-2.5">
                      <span
                        className={`chip ${
                          p.health === "healthy"
                            ? "border-succeeded/40 bg-succeeded/10 text-succeeded"
                            : "border-queued/40 bg-queued/10 text-queued"
                        }`}
                      >
                        <Server size={12} />
                        {p.health}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Panel>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-3">
        <Panel
          title="最近失败任务"
          right={
            <span className="flex items-center gap-1 text-xs text-failed">
              <AlertTriangle size={12} />
              {failed.length}
            </span>
          }
          bodyClass="p-0"
        >
          {failed.length === 0 ? (
            <div className="p-6 text-center text-[13px] text-ink3">没有失败任务 🎉</div>
          ) : (
            <ul>
              {failed.map((j) => (
                <li
                  key={j.id}
                  onClick={() => nav(`/jobs/${j.id}`)}
                  className="flex cursor-pointer items-start gap-3 border-b border-border/60 px-4 py-3 last:border-0 hover:bg-panel2"
                >
                  <AlertTriangle size={15} className="mt-0.5 flex-shrink-0 text-failed" />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="truncate font-medium text-ink">{j.name}</span>
                      <span className="chip border-failed/40 bg-failed/10 text-failed">
                        {j.failure?.category}
                      </span>
                    </div>
                    <p className="mt-0.5 truncate text-xs text-ink3">{j.failure?.summary}</p>
                  </div>
                  <span className="flex-shrink-0 text-xs text-ink3">{fmtRelative(j.createdAt)}</span>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel
          title="最近运行任务"
          right={
            <button className="flex items-center gap-1 text-xs text-brand hover:underline" onClick={() => nav("/jobs")}>
              查看全部 <ArrowRight size={12} />
            </button>
          }
          bodyClass="p-0"
        >
          <ul>
            {recent.map((j) => (
              <li
                key={j.id}
                onClick={() => nav(`/jobs/${j.id}`)}
                className="flex cursor-pointer items-center gap-3 border-b border-border/60 px-4 py-2.5 last:border-0 hover:bg-panel2"
              >
                <StatusBadge status={j.status} dot />
                <span className="min-w-0 flex-1 truncate text-[13px] text-ink">{j.name}</span>
                <GpuTypeBadge type={j.resources.gpuType} />
                <span className="w-16 text-right text-xs tabular-nums text-ink3">
                  {j.status === "Running" || j.status === "Succeeded"
                    ? fmtDuration(j.durationSec)
                    : fmtRelative(j.createdAt)}
                </span>
              </li>
            ))}
          </ul>
        </Panel>
      </div>
    </div>
  );
}
