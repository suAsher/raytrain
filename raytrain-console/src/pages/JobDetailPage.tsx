import { useState, useEffect } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  ArrowLeft,
  Ban,
  RotateCw,
  Copy,
  AlertTriangle,
  ScrollText,
  Activity,
  ChevronRight,
  Loader,
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
import { useI18n } from "../i18n";

export function JobDetailPage() {
  const { jobId } = useParams();
  const nav = useNavigate();
  const { t } = useI18n();
  const { getJob, cancelJob, retryJob } = useStore();
  const [sp, setSp] = useSearchParams();
  const [tab, setTab] = useState(sp.get("tab") || "overview");
  // Prefer the rich detail payload from the backend; fall back to the list
  // record already in the store while it loads.
  const [detail, setDetail] = useState<Job | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    if (jobId)
      fetchJob(jobId)
        .then((j) => alive && setDetail(j))
        .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [jobId]);
  const job = detail || getJob(jobId || "");

  if (!job) {
    return (
      <div className="py-20 text-center text-ink3">
        {loading ? (
          <Loader size={18} className="mx-auto animate-spin" />
        ) : (
          <>
            <p>{t("jd.notFound", { id: jobId || "" })}</p>
            <button className="btn mt-4" onClick={() => nav("/jobs")}>
              <ArrowLeft size={14} /> {t("jd.backList")}
            </button>
          </>
        )}
      </div>
    );
  }

  const changeTab = (tk: string) => {
    setTab(tk);
    setSp(tk === "overview" ? {} : { tab: tk });
  };

  const canCancel = job.status === "Running" || job.status === "Queued";
  const warnCount = (job.events || []).filter((e) => e.type === "Warning").length;

  const tabs = [
    { key: "overview", label: t("jd.tabOverview") },
    { key: "logs", label: t("jd.tabLogs") },
    { key: "events", label: t("jd.tabEvents"), count: warnCount || undefined },
    { key: "pods", label: t("jd.tabPods"), count: job.pods?.length },
    { key: "metrics", label: t("jd.tabMetrics") },
    { key: "config", label: t("jd.tabConfig") },
    { key: "artifacts", label: t("jd.tabArtifacts"), count: job.artifacts?.length || undefined },
  ];

  const totalGpu = job.resources.nodes * job.resources.gpusPerNode;

  return (
    <div>
      <button className="mb-3 flex items-center gap-1 text-xs text-ink3 hover:text-ink2" onClick={() => nav("/jobs")}>
        <ArrowLeft size={13} /> {t("jd.back")}
      </button>

      {/* summary header */}
      <div className="mb-4 flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-lg font-semibold text-ink">{job.name}</h1>
            <StatusBadge status={job.status} dot />
            {job.live && (
              <span className="chip border-succeeded/40 bg-succeeded/10 text-succeeded" title={`Ray submission: ${job.submissionId}`}>
                {t("jd.live")}
              </span>
            )}
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-ink3">
            <span className="flex items-center gap-1">
              <QueueBadge name={job.queue} />
            </span>
            <GpuTypeBadge type={job.resources.gpuType} />
            <span>{t("jd.nodesGpu", { nodes: job.resources.nodes, gpu: totalGpu || 0 })}</span>
            <span>{t("jd.duration")} {fmtDuration(job.durationSec)}</span>
            <span>{t("jd.by")} {job.creator}</span>
            <span>{t("jd.createdAt")} {fmtRelative(job.createdAt)}</span>
          </div>
        </div>
        <div className="flex flex-shrink-0 items-center gap-2">
          <button className="btn" disabled={!canCancel} onClick={() => cancelJob(job.id)}>
            <Ban size={14} /> {t("jd.cancel")}
          </button>
          <button
            className="btn"
            onClick={async () => {
              const id = await retryJob(job.id);
              nav(`/jobs/${id}`);
            }}
          >
            <RotateCw size={14} /> {t("jd.retry")}
          </button>
          <button className="btn" onClick={() => nav(`/jobs/new?clone=${job.id}`)}>
            <Copy size={14} /> {t("jd.clone")}
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
                <ScrollText size={12} /> {t("jd.viewErrLog")}
              </button>
              <button className="btn btn-sm" onClick={() => changeTab("events")}>
                <Activity size={12} /> {t("jd.viewEvents")}
              </button>
              <button
                className="btn btn-sm btn-primary"
                onClick={async () => {
                  const id = await retryJob(job.id);
                  nav(`/jobs/${id}`);
                }}
              >
                <RotateCw size={12} /> {t("jd.retryKeep")}
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
          <Panel title={t("jd.events")} bodyClass="px-4 py-1">
            <EventTimeline job={job} />
          </Panel>
        )}
        {tab === "pods" && (
          <Panel title={t("jd.rayPods")} bodyClass="p-4">
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

function OverviewTab({ job }: { job: Job }) {
  const { t } = useI18n();
  const totalGpu = job.resources.nodes * job.resources.gpusPerNode;
  return (
    <div className="space-y-3">
      <Panel title={t("jd.timeline")}>
        <JobTimeline job={job} />
      </Panel>

      <div className="grid grid-cols-3 gap-3">
        <Panel title={t("jd.resSummary")} className="col-span-1">
          <dl className="space-y-2 text-[13px]">
            <Row k="GPU Type" v={<GpuTypeBadge type={job.resources.gpuType} />} />
            <Row k="Nodes × GPU" v={`${job.resources.nodes} × ${job.resources.gpusPerNode} = ${totalGpu}`} />
            <Row k="CPU / GPU" v={`${job.resources.cpuPerGpu} cores`} />
            <Row k="Mem / GPU" v={`${job.resources.memPerGpuGi} GiB`} />
            <Row k="Head" v={`${job.resources.headCpu} cpu · ${job.resources.headMemGi} GiB`} />
            <Row k="RDMA" v={job.resources.rdma ? "enabled" : "disabled"} />
          </dl>
        </Panel>

        <Panel title={t("jd.dataCkpt")} className="col-span-2">
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
        <Panel title={t("jd.failSummary")}>
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
