import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Eye,
  ScrollText,
  Ban,
  RotateCw,
  Copy,
  MoreHorizontal,
  Filter,
  X,
  Loader,
  AlertTriangle,
} from "lucide-react";
import { PageHeader, Panel, Select } from "../components/primitives";
import { StatusBadge, GpuTypeBadge, QueueBadge, PriorityBadge } from "../components/badges";
import { useStore } from "../lib/store";
import { fmtDuration, fmtRelative } from "../lib/format";
import type { Job, JobStatus } from "../lib/types";
import { useI18n } from "../i18n";

const STATUSES: JobStatus[] = ["Running", "Queued", "Failed", "Succeeded", "Cancelled", "Starting"];

function RowActions({ job }: { job: Job }) {
  const nav = useNavigate();
  const { cancelJob, retryJob } = useStore();
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const canCancel = job.status === "Running" || job.status === "Queued";

  return (
    <div className="flex items-center justify-end gap-0.5">
      <button className="btn-ghost rounded p-1.5" title={t("jobs.view")} onClick={() => nav(`/jobs/${job.id}`)}>
        <Eye size={14} />
      </button>
      <button className="btn-ghost rounded p-1.5" title={t("jobs.logs")} onClick={() => nav(`/jobs/${job.id}?tab=logs`)}>
        <ScrollText size={14} />
      </button>
      <div className="relative">
        <button className="btn-ghost rounded p-1.5" title="More" onClick={() => setOpen((v) => !v)}>
          {busy ? <Loader size={14} className="animate-spin" /> : <MoreHorizontal size={14} />}
        </button>
        {open && (
          <>
            <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
            <div className="absolute right-0 top-8 z-20 w-44 rounded-md border border-borderc bg-panel2 py-1 shadow-xl">
              <button
                disabled={!canCancel || busy}
                onClick={async () => {
                  setBusy(true);
                  try {
                    await cancelJob(job.id);
                  } finally {
                    setBusy(false);
                    setOpen(false);
                  }
                }}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[13px] text-ink2 hover:bg-panel disabled:opacity-40"
              >
                <Ban size={13} /> {t("jobs.cancel")}
              </button>
              <button
                onClick={async () => {
                  setBusy(true);
                  try {
                    const id = await retryJob(job.id);
                    setOpen(false);
                    nav(`/jobs/${id}`);
                  } finally {
                    setBusy(false);
                  }
                }}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[13px] text-ink2 hover:bg-panel"
              >
                <RotateCw size={13} /> {t("jobs.retry")}
              </button>
              <button
                onClick={() => {
                  setOpen(false);
                  nav(`/jobs/new?clone=${job.id}`);
                }}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[13px] text-ink2 hover:bg-panel"
              >
                <Copy size={13} /> {t("jobs.clone")}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export function JobsPage() {
  const { jobs, project, me, loading, error } = useStore();
  const { t } = useI18n();
  const nav = useNavigate();

  const [status, setStatus] = useState("all");
  const [queue, setQueue] = useState("all");
  const [gpu, setGpu] = useState("all");
  const [creator, setCreator] = useState("all");
  const [onlyMine, setOnlyMine] = useState(false);
  const [failureOnly, setFailureOnly] = useState(false);
  const [q, setQ] = useState("");

  // Derive filter option lists from the real jobs.
  const queueOpts = useMemo(() => Array.from(new Set(jobs.map((j) => j.queue).filter(Boolean))), [jobs]);
  const creatorOpts = useMemo(() => Array.from(new Set(jobs.map((j) => j.creator).filter(Boolean))), [jobs]);

  const filtered = useMemo(() => {
    return jobs.filter((j) => {
      if (project !== "All projects" && j.project !== project) return false;
      if (status !== "all" && j.status !== status) return false;
      if (queue !== "all" && j.queue !== queue) return false;
      if (gpu !== "all" && j.resources.gpuType !== gpu) return false;
      if (creator !== "all" && j.creator !== creator) return false;
      if (onlyMine && me && j.creator !== me.user) return false;
      if (failureOnly && j.status !== "Failed") return false;
      if (q && !j.name.toLowerCase().includes(q.toLowerCase())) return false;
      return true;
    });
  }, [jobs, project, status, queue, gpu, creator, onlyMine, failureOnly, q, me]);

  const reset = () => {
    setStatus("all");
    setQueue("all");
    setGpu("all");
    setCreator("all");
    setOnlyMine(false);
    setFailureOnly(false);
    setQ("");
  };

  const opt = (label: string, vals: string[]) => [
    { value: "all", label },
    ...vals.map((v) => ({ value: v, label: v })),
  ];

  return (
    <div>
      <PageHeader
        title={t("jobs.title")}
        subtitle={`${t("jobs.count", { n: filtered.length })}${project !== "All projects" ? ` · ${project}` : ""}`}
        actions={
          <button className="btn btn-primary" onClick={() => nav("/jobs/new")}>
            {t("jobs.create")}
          </button>
        }
      />

      {error && (
        <div className="mb-3 flex items-center gap-2 rounded-md border border-failed/30 bg-failed/10 px-3 py-2 text-xs text-failed">
          <AlertTriangle size={13} /> {error}
        </div>
      )}

      <Panel className="mb-3" bodyClass="p-3">
        <div className="flex flex-wrap items-center gap-2">
          <Filter size={14} className="text-ink3" />
          <input placeholder={t("jobs.jobName")} value={q} onChange={(e) => setQ(e.target.value)} className="input w-40" />
          <Select value={status} onChange={setStatus} options={opt(t("jobs.allStatus"), STATUSES)} className="w-32" />
          <Select value={queue} onChange={setQueue} options={opt(t("jobs.allQueues"), queueOpts)} className="w-36" />
          <Select value={gpu} onChange={setGpu} options={opt(t("jobs.allGpu"), ["H20", "A100", "CPU-only"])} className="w-32" />
          <Select value={creator} onChange={setCreator} options={opt(t("jobs.allCreators"), creatorOpts)} className="w-36" />
          <label className="flex cursor-pointer items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs text-ink2">
            <input type="checkbox" checked={onlyMine} onChange={(e) => setOnlyMine(e.target.checked)} />
            {t("jobs.onlyMine")}
          </label>
          <label className="flex cursor-pointer items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs text-ink2">
            <input type="checkbox" checked={failureOnly} onChange={(e) => setFailureOnly(e.target.checked)} />
            {t("jobs.failuresOnly")}
          </label>
          <button className="btn-ghost flex items-center gap-1 rounded-md px-2 py-1.5 text-xs" onClick={reset}>
            <X size={12} /> {t("jobs.reset")}
          </button>
        </div>
      </Panel>

      <Panel bodyClass="p-0">
        <div className="overflow-x-auto">
          <table className="w-full text-[13px]">
            <thead>
              <tr className="border-b border-border text-left text-xs text-ink3">
                <th className="px-3 py-2 font-medium">{t("jobs.jobName")}</th>
                <th className="px-3 py-2 font-medium">{t("common.status")}</th>
                <th className="px-3 py-2 font-medium">{t("jobs.project")}</th>
                <th className="px-3 py-2 font-medium">{t("jobs.queue")}</th>
                <th className="px-3 py-2 font-medium">{t("jobs.gpu")}</th>
                <th className="px-3 py-2 text-right font-medium">{t("jobs.nodes")}</th>
                <th className="px-3 py-2 text-right font-medium">{t("jobs.gpus")}</th>
                <th className="px-3 py-2 font-medium">{t("jobs.image")}</th>
                <th className="px-3 py-2 font-medium">{t("jobs.creator")}</th>
                <th className="px-3 py-2 text-right font-medium">{t("jobs.duration")}</th>
                <th className="px-3 py-2 font-medium">{t("jobs.created")}</th>
                <th className="px-3 py-2 text-right font-medium">{t("common.actions")}</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((j) => (
                <tr
                  key={j.id}
                  onClick={() => nav(`/jobs/${j.id}`)}
                  className="cursor-pointer border-b border-border/50 last:border-0 hover:bg-panel2"
                >
                  <td className="px-3 py-2.5">
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-ink">{j.name}</span>
                      <PriorityBadge p={j.priority} />
                    </div>
                  </td>
                  <td className="px-3 py-2.5">
                    <StatusBadge status={j.status} dot />
                  </td>
                  <td className="px-3 py-2.5 text-ink2">{j.project}</td>
                  <td className="px-3 py-2.5">
                    <QueueBadge name={j.queue} />
                  </td>
                  <td className="px-3 py-2.5">
                    <GpuTypeBadge type={j.resources.gpuType} />
                  </td>
                  <td className="px-3 py-2.5 text-right tabular-nums text-ink2">{j.resources.nodes}</td>
                  <td className="px-3 py-2.5 text-right tabular-nums text-ink2">
                    {j.resources.nodes * j.resources.gpusPerNode}
                  </td>
                  <td className="px-3 py-2.5 font-mono text-xs text-ink3">{j.image}</td>
                  <td className="px-3 py-2.5 text-ink2">{j.creator}</td>
                  <td className="px-3 py-2.5 text-right tabular-nums text-ink2">{fmtDuration(j.durationSec)}</td>
                  <td className="px-3 py-2.5 text-ink3">{fmtRelative(j.createdAt)}</td>
                  <td className="px-3 py-2.5" onClick={(e) => e.stopPropagation()}>
                    <RowActions job={j} />
                  </td>
                </tr>
              ))}
              {filtered.length === 0 && (
                <tr>
                  <td colSpan={12} className="px-4 py-10 text-center text-ink3">
                    {loading ? <Loader size={16} className="mx-auto animate-spin" /> : t("jobs.none")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
  );
}
