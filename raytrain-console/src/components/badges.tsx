import {
  Activity,
  CircleCheck,
  CircleX,
  Clock,
  Loader,
  Cpu,
  Zap,
} from "lucide-react";
import type { GpuType, JobStatus } from "../lib/types";

const STATUS_STYLE: Record<JobStatus, { cls: string; icon: typeof Activity; label: string }> = {
  Running: { cls: "border-running/40 bg-running/10 text-running", icon: Activity, label: "Running" },
  Queued: { cls: "border-queued/40 bg-queued/10 text-queued", icon: Clock, label: "Queued" },
  Failed: { cls: "border-failed/40 bg-failed/10 text-failed", icon: CircleX, label: "Failed" },
  Succeeded: { cls: "border-succeeded/40 bg-succeeded/10 text-succeeded", icon: CircleCheck, label: "Succeeded" },
  Cancelled: { cls: "border-cancelled/40 bg-cancelled/10 text-cancelled", icon: CircleX, label: "Cancelled" },
  Starting: { cls: "border-starting/40 bg-starting/10 text-starting", icon: Loader, label: "Starting" },
};

export function StatusBadge({ status, dot }: { status: JobStatus; dot?: boolean }) {
  const s = STATUS_STYLE[status];
  const Icon = s.icon;
  return (
    <span className={`chip ${s.cls}`}>
      {dot ? (
        <span className={`h-1.5 w-1.5 rounded-full bg-current ${status === "Running" ? "animate-pulse" : ""}`} />
      ) : (
        <Icon size={12} className={status === "Starting" ? "animate-spin" : ""} />
      )}
      {s.label}
    </span>
  );
}

export function GpuTypeBadge({ type }: { type: GpuType }) {
  const map: Record<GpuType, string> = {
    H20: "border-blue-500/40 bg-blue-500/10 text-blue-300",
    A100: "border-violet-500/40 bg-violet-500/10 text-violet-300",
    "CPU-only": "border-borderc bg-panel2 text-ink2",
  };
  const Icon = type === "CPU-only" ? Cpu : Zap;
  return (
    <span className={`chip ${map[type]}`}>
      <Icon size={12} />
      {type}
    </span>
  );
}

export function QueueBadge({ name, health }: { name: string; health?: "healthy" | "degraded" | "down" }) {
  const dot =
    health === "degraded" ? "bg-queued" : health === "down" ? "bg-failed" : "bg-succeeded";
  return (
    <span className="chip border-borderc bg-panel2 text-ink2">
      {health && <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />}
      {name}
    </span>
  );
}

export function PriorityBadge({ p }: { p: "low" | "normal" | "high" }) {
  if (p === "normal") return <span className="text-ink3">normal</span>;
  const cls = p === "high" ? "text-orange-400" : "text-ink3";
  return <span className={cls}>{p}</span>;
}
