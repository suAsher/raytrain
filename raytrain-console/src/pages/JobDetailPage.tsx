import { useState, useEffect } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  ArrowLeft,
  Ban,
  RotateCw,
  Copy,
  ExternalLink,
  AlertTriangle,
  ScrollText,
  Activity,
  ChevronRight,
} from "lucide-react";
import { Panel, Tabs } from "../components/primitives";
import { StatusBadge, GpuTypeBadge, QueueBadge } from "../components/badges";
import {
  JobTimeline,
  LogViewer,
  EventTimeline,
  PodTable,
  MetricsPanel,
  ConfigPreview,
  ArtifactsTab,
} from "../components/jobDetail";
import { useStore } from "../lib/store";
import { fetchJob } from "../lib/consoleApi";
import type { Job } from "../lib/types";
import { fmtDuration, fmtRelative } from "../lib/format";

export function JobDetailPage() {
  const { jobId } = useParams();
  const nav = useNavigate();
  const { getJob, cancelJob, retryJob } = useStore();
  const [sp, setSp] = useSearchParams();
  const [tab, setTab] = useState(sp.get("tab") || "overview");
  // Prefer the rich detail payload from the backend; fall back to the list
  // record already in the store while it loads (or if backend is unavailable).
  const [detail, setDetail] = useState<Job | undefined>(undefined);
  useEffect(() => {
    let alive = true;
    if (jobId) fetchJob(jobId).then((j) => alive && setDetail(j));
    return () => {
      alive = false;
    };
  }, [jobId]);
  const job = detail || getJob(jobId || "");

  if (!job) {
    return (
      <div className="py-20 text-center text-ink3">
        <p>找不到任务 {jobId}</p>
        <button className="btn mt-4" onClick={() => nav("/jobs")}>
          <ArrowLeft size={14} /> 返回任务列表
        </button>
      </div>
    );
  }

  const changeTab = (t: string) => {
    setTab(t);
    setSp(t === "overview" ? {} : { tab: t });
  };

  const canCancel = job.status === "Running" || job.status === "Queued";
  const warnCount = (job.events || []).filter((e) => e.type === "Warning").length;

  const tabs = [
    { key: "overview", label: "Overview" },
    { key: "logs", label: "Logs" },
    { key: "events", label: "Events", count: warnCount || undefined },
    { key: "pods", label: "Pods", count: job.pods?.length },
    { key: "metrics", label: "Metrics" },
    { key: "config", label: "Config" },
    { key: "artifacts", label: "Artifacts", count: job.artifacts?.length || undefined },
  ];

  const totalGpu = job.resources.nodes * job.resources.gpusPerNode;

  return (
    <div>
      <button className="mb-3 flex items-center gap-1 text-xs text-ink3 hover:text-ink2" onClick={() => nav("/jobs")}>
        <ArrowLeft size={13} /> Training Jobs
      </button>

      {/* summary header */}
      <div className="mb-4 flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-lg font-semibold text-ink">{job.name}</h1>
            <StatusBadge status={job.status} dot />
            {job.live && (
              <span className="chip border-succeeded/40 bg-succeeded/10 text-succeeded" title={`Ray submission: ${job.submissionId}`}>
                ● LIVE on cluster
              </span>
            )}
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-ink3">
            <span className="flex items-center gap-1">
              <QueueBadge name={job.queue} />
            </span>
            <GpuTypeBadge type={job.resources.gpuType} />
            <span>{job.resources.nodes} nodes · {totalGpu || 0} GPU</span>
            <span>duration {fmtDuration(job.durationSec)}</span>
            <span>by {job.creator}</span>
            <span>created {fmtRelative(job.createdAt)}</span>
          </div>
        </div>
        <div className="flex flex-shrink-0 items-center gap-2">
          <button className="btn" disabled={!canCancel} onClick={() => cancelJob(job.id)}>
            <Ban size={14} /> Cancel
          </button>
          <button
            className="btn"
            onClick={async () => {
              const id = await retryJob(job.id);
              nav(`/jobs/${id}`);
            }}
          >
            <RotateCw size={14} /> Retry
          </button>
          <button className="btn" onClick={() => nav(`/jobs/new?clone=${job.id}`)}>
            <Copy size={14} /> Clone
          </button>
          <button className="btn" onClick={() => window.alert("打开 Ray Dashboard（mock）")}>
            <ExternalLink size={14} /> Dashboard
          </button>
        </div>
      </div>

      {/* failure banner */}
      {job.failure && (
        <div className="mb-4 flex items-start gap-3 rounded-md border border-failed/40 bg-failed/10 p-3">
          <AlertTriangle size={18} className="mt-0.5 flex-shrink-0 text-failed" />
          <div className="flex-1">
            <div className="flex items-center gap-2">
              <span className="chip border-failed/40 bg-failed/20 text-failed">{job.failure.category}</span>
              <span className="text-[13px] font-medium text-ink">{job.failure.summary}</span>
            </div>
            <p className="mt-1 text-[13px] text-ink2">{job.failure.detail}</p>
            <div className="mt-2 flex gap-2">
              <button className="btn btn-sm" onClick={() => changeTab("logs")}>
                <ScrollText size={12} /> 查看错误日志
              </button>
              <button className="btn btn-sm" onClick={() => changeTab("events")}>
                <Activity size={12} /> 查看事件
              </button>
              <button
                className="btn btn-sm btn-primary"
                onClick={async () => {
                  const id = await retryJob(job.id);
                  nav(`/jobs/${id}`);
                }}
              >
                <RotateCw size={12} /> Retry（保留原配置）
              </button>
            </div>
          </div>
        </div>
      )}

      <Tabs tabs={tabs} active={tab} onChange={changeTab} />

      <div className="mt-4">
        {tab === "overview" && <OverviewTab job={job} />}
        {tab === "logs" && (
          <Panel bodyClass="p-3">
            <LogViewer job={job} />
          </Panel>
        )}
        {tab === "events" && (
          <Panel title="Kubernetes Events（已翻译为可读原因）" bodyClass="px-4 py-1">
            <EventTimeline job={job} />
          </Panel>
        )}
        {tab === "pods" && (
          <Panel title="Ray Pods" bodyClass="p-4">
            <PodTable job={job} />
          </Panel>
        )}
        {tab === "metrics" && <MetricsPanel job={job} />}
        {tab === "config" && <ConfigPreview job={job} />}
        {tab === "artifacts" && (
          <Panel bodyClass="p-4">
            <ArtifactsTab job={job} />
          </Panel>
        )}
      </div>
    </div>
  );
}

function OverviewTab({ job }: { job: ReturnType<typeof useStore>["jobs"][number] }) {
  const totalGpu = job.resources.nodes * job.resources.gpusPerNode;
  return (
    <div className="space-y-3">
      <Panel title="状态时间线">
        <JobTimeline job={job} />
      </Panel>

      <div className="grid grid-cols-3 gap-3">
        <Panel title="资源摘要" className="col-span-1">
          <dl className="space-y-2 text-[13px]">
            <Row k="GPU Type" v={<GpuTypeBadge type={job.resources.gpuType} />} />
            <Row k="Nodes × GPU" v={`${job.resources.nodes} × ${job.resources.gpusPerNode} = ${totalGpu}`} />
            <Row k="CPU / GPU" v={`${job.resources.cpuPerGpu} cores`} />
            <Row k="Mem / GPU" v={`${job.resources.memPerGpuGi} GiB`} />
            <Row k="Head" v={`${job.resources.headCpu} cpu · ${job.resources.headMemGi} GiB`} />
            <Row k="RDMA" v={job.resources.rdma ? "enabled" : "disabled"} />
          </dl>
        </Panel>

        <Panel title="数据与 checkpoint" className="col-span-2">
          <dl className="space-y-2 text-[13px]">
            <Row k="Dataset" v={<code className="text-xs text-ink2">{job.mounts.dataset.uri} → {job.mounts.dataset.path} ({job.mounts.dataset.mode})</code>} />
            <Row
              k="Checkpoint"
              v={
                <code className="text-xs text-ink2">
                  {job.mounts.checkpoint.uri} → {job.mounts.checkpoint.path} ({job.mounts.checkpoint.mode}
                  {job.mounts.checkpoint.shared ? ", shared" : ""})
                </code>
              }
            />
            <Row k="Scratch" v={<code className="text-xs text-ink2">{job.mounts.scratch.path} · {job.mounts.scratch.sizeGi} GiB</code>} />
            <Row k="Entrypoint" v={<code className="text-xs text-ink2">{job.entrypoint}</code>} />
            <Row k="Image" v={<code className="text-xs text-ink2">{job.image}</code>} />
          </dl>
          {job.description && <p className="mt-3 border-t border-border pt-3 text-[13px] text-ink3">{job.description}</p>}
        </Panel>
      </div>

      {job.failure && (
        <Panel title="失败原因摘要">
          <div className="flex items-start gap-2 text-[13px]">
            <ChevronRight size={14} className="mt-0.5 text-failed" />
            <div>
              <span className="font-medium text-failed">{job.failure.category}</span>
              <span className="text-ink2"> — {job.failure.detail}</span>
            </div>
          </div>
        </Panel>
      )}
    </div>
  );
}

function Row({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4">
      <dt className="text-ink3">{k}</dt>
      <dd className="text-right text-ink">{v}</dd>
    </div>
  );
}
