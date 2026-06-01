import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { FlaskConical, GitBranch, Copy, RotateCw, Loader } from "lucide-react";
import { PageHeader, Panel } from "../components/primitives";
import { fetchExperiments } from "../lib/consoleApi";
import type { Experiment } from "../lib/types";
import { useStore } from "../lib/store";
import { fmtRelative } from "../lib/format";

export function ExperimentsPage() {
  const nav = useNavigate();
  const { retryJob } = useStore();
  const [experiments, setExperiments] = useState<Experiment[]>([]);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let alive = true;
    fetchExperiments()
      .then((e) => alive && setExperiments(e))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, []);
  return (
    <div>
      <PageHeader title="Experiments" subtitle="实验分组与复现。Clone 复用配置，Retry 保留原配置生成新 run" />
      {loading && <div className="py-12 text-center text-ink3"><Loader size={18} className="mx-auto animate-spin" /></div>}
      {!loading && experiments.length === 0 && (
        <Panel bodyClass="py-12 text-center text-ink3">
          还没有实验。实验由训练任务自动聚合（按 experiment 或 project 分组），提交训练后会出现在这里。
        </Panel>
      )}
      <div className="grid grid-cols-2 gap-3">
        {experiments.map((e) => (
          <Panel key={e.id} bodyClass="p-4">
            <div className="flex items-start justify-between">
              <div className="flex items-center gap-2">
                <FlaskConical size={16} className="text-brand" />
                <div>
                  <div className="font-semibold text-ink">{e.name}</div>
                  <div className="text-xs text-ink3">{e.project}</div>
                </div>
              </div>
              <span className="chip border-succeeded/40 bg-succeeded/10 text-succeeded">{e.bestMetric}</span>
            </div>

            <div className="mt-3 grid grid-cols-3 gap-2 text-center">
              <div className="rounded-md border border-border bg-panel2 py-2">
                <div className="text-lg font-semibold tabular-nums text-ink">{e.runs}</div>
                <div className="text-xs text-ink3">runs</div>
              </div>
              <div className="rounded-md border border-border bg-panel2 py-2">
                <div className="text-sm font-medium text-ink">{fmtRelative(e.lastRunAt)}</div>
                <div className="text-xs text-ink3">last run</div>
              </div>
              <div className="rounded-md border border-border bg-panel2 py-2">
                <div className="flex items-center justify-center gap-1 text-sm font-medium text-ink2">
                  <GitBranch size={12} /> {e.baselineJobId}
                </div>
                <div className="text-xs text-ink3">baseline</div>
              </div>
            </div>

            <div className="mt-3 flex gap-2">
              <button className="btn btn-sm flex-1" onClick={() => nav(`/jobs/${e.baselineJobId}`)}>
                查看 baseline
              </button>
              <button className="btn btn-sm" onClick={() => nav(`/jobs/new?clone=${e.baselineJobId}`)}>
                <Copy size={12} /> Clone
              </button>
              <button
                className="btn btn-sm"
                onClick={async () => {
                  const id = await retryJob(e.baselineJobId);
                  nav(`/jobs/${id}`);
                }}
              >
                <RotateCw size={12} /> Retry
              </button>
            </div>
          </Panel>
        ))}
      </div>
    </div>
  );
}
