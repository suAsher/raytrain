import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  Check,
  ChevronLeft,
  ChevronRight,
  AlertTriangle,
  Cpu,
  Zap,
  MemoryStick,
  Clock,
  Code2,
  HardDrive,
  ClipboardCheck,
  Info,
  Loader,
} from "lucide-react";
import { PageHeader, Panel, Select } from "../components/primitives";
import { GpuTypeBadge } from "../components/badges";
import { useStore } from "../lib/store";
import { rayJobYaml } from "../lib/rayJobPreview";
import type { GpuType, Job, Queue } from "../lib/types";
import {
  createJob as createJobApi,
  fetchQueues,
  fetchRuntimeImages,
} from "../lib/consoleApi";
import { apiFetch, errMsg } from "../lib/api";
import { useI18n } from "../i18n";

interface Draft {
  name: string;
  project: string;
  quotaGroup: string;
  queue: string;
  priority: "low" | "normal" | "high";
  description: string;
  image: string;
  entrypoint: string;
  workingDir: string;
  gitRef: string;
  env: { key: string; value: string }[];
  gpuType: GpuType | "Auto";
  nodes: number;
  gpusPerNode: number;
  cpuPerGpu: number;
  memPerGpuGi: number;
  headCpu: number;
  headMemGi: number;
  rdma: boolean;
  datasetUri: string;
  checkpointUri: string;
  checkpointShared: boolean;
  scratchGi: number;
}

const STEP_KEYS = [
  { key: "basic", labelKey: "cj.stepBasic", icon: Info },
  { key: "code", labelKey: "cj.stepCode", icon: Code2 },
  { key: "resources", labelKey: "cj.stepRes", icon: Zap },
  { key: "data", labelKey: "cj.stepData", icon: HardDrive },
  { key: "review", labelKey: "cj.stepReview", icon: ClipboardCheck },
];

function defaultDraft(clone?: Job): Draft {
  if (clone) {
    return {
      name: clone.name + "-clone",
      project: clone.project,
      quotaGroup: clone.quotaGroup,
      queue: clone.queue,
      priority: clone.priority,
      description: clone.description || "",
      image: clone.image,
      entrypoint: clone.entrypoint,
      workingDir: clone.workingDir,
      gitRef: clone.gitRef || "",
      env: clone.env,
      gpuType: clone.resources.gpuType,
      nodes: clone.resources.nodes,
      gpusPerNode: clone.resources.gpusPerNode,
      cpuPerGpu: clone.resources.cpuPerGpu,
      memPerGpuGi: clone.resources.memPerGpuGi,
      headCpu: clone.resources.headCpu,
      headMemGi: clone.resources.headMemGi,
      rdma: clone.resources.rdma,
      datasetUri: clone.mounts.dataset.uri,
      checkpointUri: clone.mounts.checkpoint.uri,
      checkpointShared: clone.mounts.checkpoint.shared,
      scratchGi: clone.mounts.scratch.sizeGi,
    };
  }
  return {
    name: "",
    project: "",
    quotaGroup: "",
    queue: "",
    priority: "normal",
    description: "",
    image: "",
    entrypoint: "",
    workingDir: "",
    gitRef: "main",
    env: [{ key: "OMP_NUM_THREADS", value: "8" }],
    gpuType: "H20",
    nodes: 1,
    gpusPerNode: 8,
    cpuPerGpu: 8,
    memPerGpuGi: 96,
    headCpu: 4,
    headMemGi: 16,
    rdma: false,
    datasetUri: "",
    checkpointUri: "",
    checkpointShared: true,
    scratchGi: 200,
  };
}

export function CreateJobPage() {
  const nav = useNavigate();
  const { t } = useI18n();
  const { getJob, projects } = useStore();
  const [sp] = useSearchParams();
  const cloneId = sp.get("clone");
  const cloneSrc = cloneId ? getJob(cloneId) : undefined;

  const [step, setStep] = useState(0);
  const [d, setD] = useState<Draft>(() => defaultDraft(cloneSrc));
  const set = (patch: Partial<Draft>) => setD((prev) => ({ ...prev, ...patch }));

  // Real option sources.
  const [queues, setQueues] = useState<Queue[]>([]);
  const [images, setImages] = useState<string[]>([]);
  const [optErr, setOptErr] = useState("");
  const projectOptions = projects.filter((p) => p !== "All projects");

  useEffect(() => {
    fetchQueues().then(setQueues).catch((e) => setOptErr(errMsg(e)));
    fetchRuntimeImages().then(setImages).catch(() => {});
  }, []);

  // Populate sensible defaults once options arrive (skip when cloning).
  useEffect(() => {
    if (cloneSrc) return;
    setD((prev) => {
      const next = { ...prev };
      if (!next.project && projectOptions.length) {
        next.project = projectOptions[0];
        next.quotaGroup = projectOptions[0] + "-qg";
        next.workingDir = "/workspace/" + projectOptions[0];
      }
      if (!next.image && images.length) next.image = images[0];
      return next;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectOptions.length, images.length]);

  // Queues valid for the chosen gpu type (Req 9.6 — only real Kueue queues).
  const effectiveGpuType: GpuType = d.gpuType === "Auto" ? "H20" : d.gpuType;
  const isCpu = effectiveGpuType === "CPU-only";
  const queuesForGpu = useMemo(
    () => queues.filter((q) => q.gpuType === effectiveGpuType),
    [queues, effectiveGpuType]
  );
  const queueChoices = queuesForGpu.length ? queuesForGpu : queues;

  // keep the selected queue valid for the current gpu type
  useEffect(() => {
    if (queueChoices.length && !queueChoices.some((q) => q.name === d.queue)) {
      set({ queue: queueChoices[0].name });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queueChoices]);

  const totalGpu = isCpu ? 0 : d.nodes * d.gpusPerNode;
  const totalCpu = (isCpu ? d.nodes * 16 : totalGpu * d.cpuPerGpu) + d.headCpu;
  const totalMem = (isCpu ? d.nodes * 64 : totalGpu * d.memPerGpuGi) + d.headMemGi;
  const selectedQueue = queues.find((q) => q.name === d.queue);
  const estWaitMin = selectedQueue ? selectedQueue.avgWaitMin : 0;
  const noQueues = queues.length === 0;

  const [submitting, setSubmitting] = useState(false);
  const [submitErr, setSubmitErr] = useState("");

  // validation
  const errors = useMemo(() => {
    const e: string[] = [];
    if (!d.name.trim()) e.push("Job name 不能为空");
    else if (!/^[a-z0-9-]+$/.test(d.name)) e.push("Job name 只能包含小写字母、数字和连字符");
    if (!d.image) e.push("必须选择镜像");
    if (!d.entrypoint.trim()) e.push("Entrypoint 不能为空");
    if (!d.queue) e.push("必须选择队列");
    if (!isCpu && d.gpusPerNode < 1) e.push("GPU per node 至少为 1");
    if (d.nodes > 1 && !d.checkpointShared)
      e.push("多节点训练要求 checkpoint 使用共享存储，否则各 worker 无法写入同一路径");
    return e;
  }, [d, isCpu]);

  const stepValid = (s: number): boolean => {
    if (s === 0) return !!d.name.trim() && /^[a-z0-9-]+$/.test(d.name) && !!d.queue;
    if (s === 1) return !!d.image && !!d.entrypoint.trim();
    if (s === 3) return !(d.nodes > 1 && !d.checkpointShared);
    return true;
  };

  const submit = async () => {
    if (errors.length || noQueues) return;
    setSubmitting(true);
    setSubmitErr("");
    try {
      const created = await createJobApi({
        name: d.name,
        project: d.project,
        queue: d.queue,
        quotaGroup: d.quotaGroup,
        priority: d.priority,
        description: d.description,
        image: d.image,
        entrypoint: d.entrypoint,
        workingDir: d.workingDir,
        gitRef: d.gitRef,
        env: Object.fromEntries(d.env.filter((e) => e.key).map((e) => [e.key, e.value])),
        resources: {
          gpuType: effectiveGpuType,
          nodes: d.nodes,
          gpusPerNode: isCpu ? 0 : d.gpusPerNode,
          cpuPerGpu: d.cpuPerGpu,
          memPerGpuGi: d.memPerGpuGi,
          headCpu: d.headCpu,
          headMemGi: d.headMemGi,
          rdma: d.rdma,
        },
        mounts: {
          datasetUri: d.datasetUri,
          checkpointUri: d.checkpointUri,
          checkpointShared: d.checkpointShared,
          scratchGi: d.scratchGi,
        },
      });
      nav(`/jobs/${created.id}`);
    } catch (e) {
      setSubmitErr(errMsg(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="mx-auto max-w-5xl">
      <PageHeader
        title={cloneSrc ? t("cj.cloneTitle") : t("cj.createTitle")}
        subtitle={cloneSrc ? t("cj.cloneSub", { name: cloneSrc.name }) : t("cj.createSub")}
      />

      {optErr && (
        <div className="mb-3 flex items-center gap-2 rounded-md border border-failed/30 bg-failed/10 px-3 py-2 text-xs text-failed">
          <AlertTriangle size={13} /> {optErr}
        </div>
      )}
      {noQueues && !optErr && (
        <div className="mb-3 flex items-center gap-2 rounded-md border border-queued/40 bg-queued/10 px-3 py-2 text-xs text-queued">
          <AlertTriangle size={13} /> {t("cj.noQueue")}
        </div>
      )}

      {/* stepper */}
      <div className="mb-5 flex items-center">
        {STEP_KEYS.map((s, i) => {
          const Icon = s.icon;
          const done = i < step;
          const active = i === step;
          return (
            <div key={s.key} className="flex flex-1 items-center last:flex-none">
              <button onClick={() => i <= step && setStep(i)} className="flex items-center gap-2">
                <span
                  className={`flex h-7 w-7 items-center justify-center rounded-full border text-xs ${
                    active
                      ? "border-brand bg-brand text-white"
                      : done
                      ? "border-succeeded bg-succeeded/15 text-succeeded"
                      : "border-borderc bg-panel text-ink3"
                  }`}
                >
                  {done ? <Check size={14} /> : <Icon size={14} />}
                </span>
                <span className={`text-[13px] ${active ? "font-medium text-ink" : "text-ink3"}`}>
                  {t(s.labelKey)}
                </span>
              </button>
              {i < STEP_KEYS.length - 1 && (
                <div className={`mx-3 h-px flex-1 ${done ? "bg-succeeded/40" : "bg-border"}`} />
              )}
            </div>
          );
        })}
      </div>

      <div className="grid grid-cols-3 gap-4">
        <div className="col-span-2">
          <Panel>
            {step === 0 && (
              <StepBasic d={d} set={set} projects={projectOptions} queues={queueChoices} />
            )}
            {step === 1 && <StepCode d={d} set={set} images={images} />}
            {step === 2 && <StepResources d={d} set={set} isCpu={isCpu} />}
            {step === 3 && <StepData d={d} set={set} />}
            {step === 4 && <StepReview d={d} errors={errors} submitErr={submitErr} />}
          </Panel>
        </div>

        {/* live estimation rail */}
        <div className="col-span-1">
          <Panel title={t("cj.estTitle")} className="sticky top-0">
            <div className="space-y-3">
              <EstRow icon={Zap} label={t("cj.totalGpu")} value={isCpu ? "—" : `${totalGpu}`} sub={isCpu ? "CPU-only" : `${effectiveGpuType}`} />
              <EstRow icon={Cpu} label={t("cj.totalCpu")} value={`${totalCpu}`} sub="cores" />
              <EstRow icon={MemoryStick} label={t("cj.totalMem")} value={`${totalMem}`} sub="GiB" />
              <EstRow icon={Clock} label={t("cj.estWait")} value={`~${estWaitMin}`} sub="min" tone={estWaitMin > 15 ? "queued" : "ink"} />
            </div>
            <div className="mt-4 border-t border-border pt-3">
              <div className="mb-1.5 text-xs text-ink3">{t("cj.queue")}</div>
              <div className="flex items-center justify-between text-[13px]">
                <span className="text-ink">{d.queue || "—"}</span>
                <GpuTypeBadge type={effectiveGpuType} />
              </div>
            </div>
            {d.nodes > 1 && (
              <div className="mt-3 flex items-start gap-2 rounded-md border border-brand/30 bg-brand/10 px-2.5 py-2 text-xs text-ink2">
                <Info size={13} className="mt-0.5 flex-shrink-0 text-brand" />
                RDMA {d.rdma ? "✓" : "✗"} · checkpoint {d.checkpointShared ? "shared ✓" : "✗"}
              </div>
            )}
            {errors.length > 0 && step === 4 && (
              <div className="mt-3 rounded-md border border-failed/30 bg-failed/10 px-2.5 py-2 text-xs text-failed">
                {t("cj.errCount", { n: errors.length })}
              </div>
            )}
          </Panel>
        </div>
      </div>

      {/* footer nav */}
      <div className="mt-5 flex items-center justify-between">
        <button className="btn" disabled={step === 0} onClick={() => setStep((s) => Math.max(0, s - 1))}>
          <ChevronLeft size={15} /> {t("cj.prev")}
        </button>
        {step < STEP_KEYS.length - 1 ? (
          <button className="btn btn-primary" disabled={!stepValid(step)} onClick={() => setStep((s) => s + 1)}>
            {t("cj.next")} <ChevronRight size={15} />
          </button>
        ) : (
          <button
            className="btn btn-primary"
            disabled={errors.length > 0 || noQueues || submitting}
            onClick={submit}
          >
            {submitting ? <Loader size={15} className="animate-spin" /> : <Check size={15} />} {t("cj.submit")}
          </button>
        )}
      </div>
    </div>
  );
}

function EstRow({
  icon: Icon,
  label,
  value,
  sub,
  tone = "ink",
}: {
  icon: typeof Zap;
  label: string;
  value: string;
  sub: string;
  tone?: "ink" | "queued";
}) {
  return (
    <div className="flex items-center gap-3">
      <Icon size={16} className="text-ink3" />
      <span className="flex-1 text-xs text-ink2">{label}</span>
      <span className={`text-lg font-semibold tabular-nums ${tone === "queued" ? "text-queued" : "text-ink"}`}>
        {value}
      </span>
      <span className="w-10 text-xs text-ink3">{sub}</span>
    </div>
  );
}

function Field({ label, children, hint }: { label: string; children: React.ReactNode; hint?: string }) {
  return (
    <div className="mb-4">
      <label className="label">{label}</label>
      {children}
      {hint && <p className="mt-1 text-xs text-ink3">{hint}</p>}
    </div>
  );
}

function StepBasic({
  d,
  set,
  projects,
  queues,
}: {
  d: Draft;
  set: (p: Partial<Draft>) => void;
  projects: string[];
  queues: Queue[];
}) {
  return (
    <div>
      <Field label="Job name" hint="小写字母、数字、连字符，例如 sslod26-pretrain-base">
        <input className="input" value={d.name} placeholder="my-training-run" onChange={(e) => set({ name: e.target.value })} />
      </Field>
      <div className="grid grid-cols-2 gap-4">
        <Field label="Project">
          <Select
            value={d.project}
            onChange={(v) => set({ project: v, quotaGroup: v + "-qg", workingDir: "/workspace/" + v })}
            options={
              projects.length
                ? projects.map((p) => ({ value: p, label: p }))
                : [{ value: "", label: "（无项目，请联系管理员）" }]
            }
          />
        </Field>
        <Field label="QuotaGroup">
          <input className="input" value={d.quotaGroup} onChange={(e) => set({ quotaGroup: e.target.value })} />
        </Field>
        <Field label="Queue">
          <Select
            value={d.queue}
            onChange={(v) => set({ queue: v })}
            options={
              queues.length
                ? queues.map((q) => ({ value: q.name, label: `${q.name} (${q.gpuType})` }))
                : [{ value: "", label: "（集群无可用队列）" }]
            }
          />
        </Field>
        <Field label="Priority">
          <Select
            value={d.priority}
            onChange={(v) => set({ priority: v as Draft["priority"] })}
            options={[
              { value: "low", label: "low" },
              { value: "normal", label: "normal" },
              { value: "high", label: "high" },
            ]}
          />
        </Field>
      </div>
      <Field label="Description（可选）">
        <textarea className="input" rows={3} value={d.description} onChange={(e) => set({ description: e.target.value })} />
      </Field>
    </div>
  );
}

function StepCode({ d, set, images }: { d: Draft; set: (p: Partial<Draft>) => void; images: string[] }) {
  return (
    <div>
      <Field label="Runtime Image" hint="从平台镜像库选择（管理员在 Admin · Runtime Images 注册）">
        {images.length ? (
          <Select value={d.image} onChange={(v) => set({ image: v })} options={images.map((i) => ({ value: i, label: i }))} />
        ) : (
          <input className="input font-mono text-xs" value={d.image} placeholder="registry/repo:tag" onChange={(e) => set({ image: e.target.value })} />
        )}
      </Field>
      <Field label="Entrypoint command" hint="例如 python tools/train.py --config configs/xxx.py">
        <input className="input font-mono text-xs" value={d.entrypoint} placeholder="python tools/train.py --config ..." onChange={(e) => set({ entrypoint: e.target.value })} />
      </Field>
      <div className="grid grid-cols-2 gap-4">
        <Field label="Working directory">
          <input className="input font-mono text-xs" value={d.workingDir} onChange={(e) => set({ workingDir: e.target.value })} />
        </Field>
        <Field label="Git commit / branch（可选）">
          <input className="input font-mono text-xs" value={d.gitRef} onChange={(e) => set({ gitRef: e.target.value })} />
        </Field>
      </div>
      <Field label="Environment variables">
        <div className="space-y-2">
          {d.env.map((kv, i) => (
            <div key={i} className="flex items-center gap-2">
              <input
                className="input font-mono text-xs"
                value={kv.key}
                placeholder="KEY"
                onChange={(e) => {
                  const env = [...d.env];
                  env[i] = { ...env[i], key: e.target.value };
                  set({ env });
                }}
              />
              <input
                className="input font-mono text-xs"
                value={kv.value}
                placeholder="value"
                onChange={(e) => {
                  const env = [...d.env];
                  env[i] = { ...env[i], value: e.target.value };
                  set({ env });
                }}
              />
              <button className="btn-ghost rounded p-1.5 text-ink3" onClick={() => set({ env: d.env.filter((_, j) => j !== i) })}>
                ✕
              </button>
            </div>
          ))}
          <button className="btn btn-sm" onClick={() => set({ env: [...d.env, { key: "", value: "" }] })}>
            + 添加变量
          </button>
        </div>
      </Field>
    </div>
  );
}

function NumField({ label, value, onChange, min = 0, max = 1024, suffix }: { label: string; value: number; onChange: (n: number) => void; min?: number; max?: number; suffix?: string }) {
  return (
    <Field label={label}>
      <div className="flex items-center gap-2">
        <input
          type="number"
          className="input"
          value={value}
          min={min}
          max={max}
          onChange={(e) => onChange(Math.max(min, Math.min(max, Number(e.target.value))))}
        />
        {suffix && <span className="text-xs text-ink3">{suffix}</span>}
      </div>
    </Field>
  );
}

function StepResources({ d, set, isCpu }: { d: Draft; set: (p: Partial<Draft>) => void; isCpu: boolean }) {
  return (
    <div>
      <Field label="GPU Type">
        <div className="flex gap-2">
          {(["H20", "A100", "Auto"] as const).map((t) => (
            <button
              key={t}
              onClick={() => set({ gpuType: t, rdma: d.nodes > 1 })}
              className={`flex-1 rounded-md border px-3 py-2.5 text-[13px] font-medium transition-colors ${
                d.gpuType === t ? "border-brand bg-brand/10 text-ink" : "border-borderc bg-panel2 text-ink2 hover:text-ink"
              }`}
            >
              {t}
            </button>
          ))}
        </div>
      </Field>
      <div className="grid grid-cols-2 gap-4">
        <NumField label="Nodes" value={d.nodes} min={1} max={64} onChange={(n) => set({ nodes: n, rdma: n > 1 ? true : d.rdma })} />
        {!isCpu && <NumField label="GPUs per node" value={d.gpusPerNode} min={1} max={8} onChange={(n) => set({ gpusPerNode: n })} />}
        {!isCpu && <NumField label="CPU per GPU" value={d.cpuPerGpu} min={1} max={32} suffix="cores" onChange={(n) => set({ cpuPerGpu: n })} />}
        {!isCpu && <NumField label="Memory per GPU" value={d.memPerGpuGi} min={8} max={256} suffix="GiB" onChange={(n) => set({ memPerGpuGi: n })} />}
        <NumField label="Head CPU" value={d.headCpu} min={1} max={32} suffix="cores" onChange={(n) => set({ headCpu: n })} />
        <NumField label="Head Memory" value={d.headMemGi} min={2} max={128} suffix="GiB" onChange={(n) => set({ headMemGi: n })} />
      </div>
      <div className="mt-2 flex items-center justify-between rounded-md border border-border bg-panel2 px-3 py-2.5">
        <div>
          <div className="text-[13px] font-medium text-ink">RDMA / InfiniBand</div>
          <div className="text-xs text-ink3">多节点训练默认开启以提升通信带宽</div>
        </div>
        <button
          onClick={() => set({ rdma: !d.rdma })}
          className={`relative h-5 w-9 rounded-full transition-colors ${d.rdma ? "bg-brand" : "bg-borderc"}`}
        >
          <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all ${d.rdma ? "left-4" : "left-0.5"}`} />
        </button>
      </div>
    </div>
  );
}

function StepData({ d, set }: { d: Draft; set: (p: Partial<Draft>) => void }) {
  const blocked = d.nodes > 1 && !d.checkpointShared;
  const [datasets, setDatasets] = useState<{ name: string; uri: string }[]>([]);
  useEffect(() => {
    let alive = true;
    apiFetch<{ name: string; uri: string }[]>("/v1/datasets")
      .then((rows) => alive && setDatasets(rows.map((r) => ({ name: r.name, uri: r.uri }))))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);
  return (
    <div>
      <Field label="Dataset (Lance)" hint="选注册的 Lance 数据集 → 注入 RAYTRAIN_DATA_SOURCE_URI，训练用 ray.data.read_lance 读取">
        {datasets.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-1.5">
            {datasets.map((ds) => (
              <button
                key={ds.uri}
                onClick={() => set({ datasetUri: ds.uri })}
                className={`chip border-borderc bg-panel2 hover:border-brand ${d.datasetUri === ds.uri ? "border-brand text-ink" : "text-ink2"}`}
              >
                {ds.name}
              </button>
            ))}
          </div>
        )}
        <div className="flex items-center gap-2">
          <span className="chip border-borderc bg-panel2 text-ink3">/data</span>
          <span className="chip border-borderc bg-panel2 text-ink3">read-only</span>
          <input className="input font-mono text-xs" value={d.datasetUri} onChange={(e) => set({ datasetUri: e.target.value })} placeholder="s3://datasets/scannet.lance" />
        </div>
      </Field>
      <Field label="Checkpoint mount" hint="默认挂载到 /checkpoints，可写">
        <div className="flex items-center gap-2">
          <span className="chip border-borderc bg-panel2 text-ink3">/checkpoints</span>
          <span className="chip border-succeeded/40 bg-succeeded/10 text-succeeded">read-write</span>
          <input className="input font-mono text-xs" value={d.checkpointUri} onChange={(e) => set({ checkpointUri: e.target.value })} placeholder="s3://checkpoints/my-run" />
        </div>
        <label className="mt-2 flex cursor-pointer items-center gap-2 text-[13px] text-ink2">
          <input type="checkbox" checked={d.checkpointShared} onChange={(e) => set({ checkpointShared: e.target.checked })} />
          使用共享存储（RWX，所有节点可写同一路径）
        </label>
      </Field>
      <Field label="Scratch" hint="默认挂载到 /scratch，用于 Ray object spilling">
        <div className="flex items-center gap-2">
          <span className="chip border-borderc bg-panel2 text-ink3">/scratch</span>
          <input type="number" className="input w-28" value={d.scratchGi} onChange={(e) => set({ scratchGi: Number(e.target.value) })} />
          <span className="text-xs text-ink3">GiB</span>
        </div>
      </Field>
      {blocked && (
        <div className="flex items-start gap-2 rounded-md border border-failed/40 bg-failed/10 px-3 py-2.5 text-[13px] text-failed">
          <AlertTriangle size={16} className="mt-0.5 flex-shrink-0" />
          <div>
            <div className="font-medium">阻断：多节点训练 checkpoint 必须使用共享存储</div>
            <div className="mt-0.5 text-xs text-failed/80">
              当前 nodes={d.nodes} 但 checkpoint 非共享。各 worker 写入本地路径会导致 checkpoint 丢失或冲突。请勾选「使用共享存储」。
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function StepReview({ d, errors, submitErr }: { d: Draft; errors: string[]; submitErr: string }) {
  const [showYaml, setShowYaml] = useState(false);
  const effectiveGpuType: GpuType = d.gpuType === "Auto" ? "H20" : d.gpuType;
  const totalGpu = effectiveGpuType === "CPU-only" ? 0 : d.nodes * d.gpusPerNode;

  const rows: [string, string][] = [
    ["Job name", d.name || "—"],
    ["Project / Queue", `${d.project || "—"} · ${d.queue || "—"}`],
    ["Priority", d.priority],
    ["Image", d.image || "—"],
    ["Entrypoint", d.entrypoint || "—"],
    ["Resources", effectiveGpuType === "CPU-only" ? `CPU-only · ${d.nodes} node(s)` : `${effectiveGpuType} · ${d.nodes} node(s) × ${d.gpusPerNode} = ${totalGpu} GPU`],
    ["RDMA", d.rdma ? "enabled" : "disabled"],
    ["Dataset", d.datasetUri ? `${d.datasetUri} → /data (ro)` : "—"],
    ["Checkpoint", d.checkpointUri ? `${d.checkpointUri} → /checkpoints (rw${d.checkpointShared ? ", shared" : ""})` : "—"],
  ];

  return (
    <div>
      {submitErr && (
        <div className="mb-4 flex items-center gap-2 rounded-md border border-failed/40 bg-failed/10 px-3 py-2.5 text-[13px] text-failed">
          <AlertTriangle size={15} /> {submitErr}
        </div>
      )}
      {errors.length > 0 && (
        <div className="mb-4 rounded-md border border-failed/40 bg-failed/10 p-3">
          <div className="mb-1.5 flex items-center gap-2 text-[13px] font-medium text-failed">
            <AlertTriangle size={15} /> 提交前需修复以下问题
          </div>
          <ul className="space-y-1 pl-6 text-xs text-failed/90">
            {errors.map((e, i) => (
              <li key={i} className="list-disc">{e}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="mb-4 overflow-hidden rounded-md border border-border">
        <table className="w-full text-[13px]">
          <tbody>
            {rows.map(([k, v]) => (
              <tr key={k} className="border-b border-border/60 last:border-0">
                <td className="w-44 bg-panel2 px-3 py-2 align-top text-xs text-ink3">{k}</td>
                <td className="px-3 py-2 font-mono text-xs text-ink2">{v}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <button className="flex items-center gap-2 text-[13px] text-brand hover:underline" onClick={() => setShowYaml((v) => !v)}>
        <ChevronRight size={14} className={showYaml ? "rotate-90 transition-transform" : "transition-transform"} />
        {showYaml ? "隐藏" : "查看"}平台生成的 RayJob YAML（dry-run 预览）
      </button>
      {showYaml && (
        <pre className="mt-2 max-h-72 overflow-auto rounded-md border border-border bg-[#0b0f14] p-3 font-mono text-[11px] leading-relaxed text-ink2">
          {rayJobYaml({
            name: d.name || "my-job",
            image: d.image,
            entrypoint: d.entrypoint,
            resources: {
              gpuType: effectiveGpuType,
              nodes: d.nodes,
              gpusPerNode: d.gpusPerNode,
              cpuPerGpu: d.cpuPerGpu,
              memPerGpuGi: d.memPerGpuGi,
              headCpu: d.headCpu,
              headMemGi: d.headMemGi,
              rdma: d.rdma,
            },
          })}
        </pre>
      )}
    </div>
  );
}
