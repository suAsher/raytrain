import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Eye,
  ScrollText,
  Ban,
  RotateCw,
  Copy,
  ExternalLink,
  MoreHorizontal,
  Filter,
  X,
} from "lucide-react";
import { PageHeader, Panel, Select } from "../components/primitives";
import { StatusBadge, GpuTypeBadge, QueueBadge, PriorityBadge } from "../components/badges";
import { useStore } from "../lib/store";
import { CREATORS, QUEUES } from "../lib/mockData";
import { fmtDuration, fmtRelative } from "../lib/format";
import type { Job, JobStatus } from "../lib/types";

const STATUSES: JobStatus[] = ["Running", "Queued", "Failed", "Succeeded", "Cancelled", "Starting"];

function RowActions({ job }: { job: Job }) {
  const nav = useNavigate();
  const { cancelJob, retryJob } = useStore();
  const [open, setOpen] = useState(false);
  const canCancel = job.status === "Running" || job.status === "Queued";

  return (
    <div className="flex items-center justify-end gap-0.5">
      <button className="btn-ghost rounded p-1.5" title="View" onClick={() => nav(`/jobs/${job.id}`)}>
        <Eye size={14} />
      </button>
      <button className="btn-ghost rounded p-1.5" title="Logs" onClick={() => nav(`/jobs/${job.id}?tab=logs`)}>
        <ScrollText size={14} />
      </button>
      <div className="relative">
        <button className="btn-ghost rounded p-1.5" title="More" onClick={() => setOpen((v) => !v)}>
          <MoreHorizontal size={14} />
        </button>
        {open && (
          <>
            <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
            <div className="absolute right-0 top-8 z-20 w-44 rounded-md border border-borderc bg-panel2 py-1 shadow-xl">
              <button
                disabled={!canCancel}
                onClick={() => {
                  cancelJob(job.id);
                  setOpen(false);
                }}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[13px] text-ink2 hover:bg-panel disabled:opacity-40"
              >
                <Ban size={13} /> Cancel
              </button>
              <button
                onClick={async () => {
                  const id = await retryJob(job.id);
                  setOpen(false);
                  nav(`/jobs/${id}`);
                }}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[13px] text-ink2 hover:bg-panel"
              >
                <RotateCw size={13} /> Retry
              </button>
              <button
                onClick={() => {
                  setOpen(false);
                  nav(`/jobs/new?clone=${job.id}`);
                }}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[13px] text-ink2 hover:bg-panel"
              >
                <Copy size={13} /> Clone
              </button>
              <button
                onClick={() => {
                  setOpen(false);
                  window.alert("打开 Ray Dashboard（mock）: http://ray-shared-h20-head:8265");
                }}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[13px] text-ink2 hover:bg-panel"
              >
                <ExternalLink size={13} /> Open Ray Dashboard
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export function JobsPage() {
  const { jobs, project } = useStore();
  const nav = useNavigate();

  const [status, setStatus] = useState("all");
  const [queue, setQueue] = useState("all");
  const [gpu, setGpu] = useState("all");
  const [creator, setCreator] = useState("all");
  const [onlyMine, setOnlyMine] = useState(false);
  const [failureOnly, setFailureOnly] = useState(false);
  const [q, setQ] = useState("");

  const filtered = useMemo(() => {
    return jobs.filter((j) => {
      if (project !== "All projects" && j.project !== project) return false;
      if (status !== "all" && j.status !== status) return false;
      if (queue !== "all" && j.queue !== queue) return false;
      if (gpu !== "all" && j.resources.gpuType !== gpu) return false;
      if (creator !== "all" && j.creator !== creator) return false;
      if (onlyMine && j.creator !== "asher") return false;
      if (failureOnly && j.status !== "Failed") return false;
      if (q && !j.name.toLowerCase().includes(q.toLowerCase())) return false;
      return true;
    });
  }, [jobs, project, status, queue, gpu, creator, onlyMine, failureOnly, q]);

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
        title="Training Jobs"
        subtitle={`${filtered.length} jobs${project !== "All projects" ? ` · ${project}` : ""}`}
        actions={
          <button className="btn btn-primary" onClick={() => nav("/jobs/new")}>
            Create Job
          </button>
        }
      />

      <Panel className="mb-3" bodyClass="p-3">
        <div className="flex flex-wrap items-center gap-2">
          <Filter size={14} className="text-ink3" />
          <input
            placeholder="job name…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            className="input w-40"
          />
          <Select value={status} onChange={setStatus} options={opt("All status", STATUSES)} className="w-32" />
          <Select value={queue} onChange={setQueue} options={opt("All queues", QUEUES)} className="w-36" />
          <Select value={gpu} onChange={setGpu} options={opt("All GPU", ["H20", "A100", "CPU-only"])} className="w-32" />
          <Select value={creator} onChange={setCreator} options={opt("All creators", CREATORS)} className="w-36" />
          <label className="flex cursor-pointer items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs text-ink2">
            <input type="checkbox" checked={onlyMine} onChange={(e) => setOnlyMine(e.target.checked)} />
            Only mine
          </label>
          <label className="flex cursor-pointer items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs text-ink2">
            <input type="checkbox" checked={failureOnly} onChange={(e) => setFailureOnly(e.target.checked)} />
            Failures only
          </label>
          <button className="btn-ghost flex items-center gap-1 rounded-md px-2 py-1.5 text-xs" onClick={reset}>
            <X size={12} /> Reset
          </button>
        </div>
      </Panel>

      <Panel bodyClass="p-0">
        <div className="overflow-x-auto">
          <table className="w-full text-[13px]">
            <thead>
              <tr className="border-b border-border text-left text-xs text-ink3">
                <th className="px-3 py-2 font-medium">Job Name</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">Project</th>
                <th className="px-3 py-2 font-medium">Queue</th>
                <th className="px-3 py-2 font-medium">GPU</th>
                <th className="px-3 py-2 text-right font-medium">Nodes</th>
                <th className="px-3 py-2 text-right font-medium">GPUs</th>
                <th className="px-3 py-2 font-medium">Image</th>
                <th className="px-3 py-2 font-medium">Creator</th>
                <th className="px-3 py-2 text-right font-medium">Duration</th>
                <th className="px-3 py-2 font-medium">Created</th>
                <th className="px-3 py-2 text-right font-medium">Actions</th>
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
                    没有符合条件的任务
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
