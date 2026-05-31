import type { ReactNode } from "react";
import { pct } from "../lib/format";

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="mb-5 flex items-start justify-between gap-4">
      <div>
        <h1 className="text-lg font-semibold text-ink">{title}</h1>
        {subtitle && <p className="mt-0.5 text-[13px] text-ink3">{subtitle}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Panel({
  title,
  right,
  children,
  className = "",
  bodyClass = "",
}: {
  title?: string;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClass?: string;
}) {
  return (
    <div className={`panel ${className}`}>
      {title && (
        <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
          <h2 className="text-[13px] font-semibold text-ink">{title}</h2>
          {right}
        </div>
      )}
      <div className={bodyClass || "p-4"}>{children}</div>
    </div>
  );
}

export function Stat({
  label,
  value,
  tone = "ink",
  sub,
}: {
  label: string;
  value: ReactNode;
  tone?: "ink" | "running" | "queued" | "failed" | "succeeded";
  sub?: string;
}) {
  const toneCls = {
    ink: "text-ink",
    running: "text-running",
    queued: "text-queued",
    failed: "text-failed",
    succeeded: "text-succeeded",
  }[tone];
  return (
    <div className="panel p-4">
      <div className="text-xs text-ink3">{label}</div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums ${toneCls}`}>{value}</div>
      {sub && <div className="mt-0.5 text-xs text-ink3">{sub}</div>}
    </div>
  );
}

export function QuotaUsageBar({
  label,
  used,
  total,
  unit = "",
}: {
  label: string;
  used: number;
  total: number;
  unit?: string;
}) {
  const p = pct(used, total);
  const tone = p >= 90 ? "bg-failed" : p >= 75 ? "bg-queued" : "bg-running";
  return (
    <div>
      <div className="mb-1 flex items-center justify-between text-xs">
        <span className="text-ink2">{label}</span>
        <span className="tabular-nums text-ink3">
          {used}
          {unit} / {total}
          {unit} · {p}%
        </span>
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-panel2">
        <div className={`h-full rounded-full ${tone}`} style={{ width: `${p}%` }} />
      </div>
    </div>
  );
}

export function Tabs({
  tabs,
  active,
  onChange,
}: {
  tabs: { key: string; label: string; count?: number }[];
  active: string;
  onChange: (k: string) => void;
}) {
  return (
    <div className="flex items-center gap-1 border-b border-border">
      {tabs.map((t) => (
        <button
          key={t.key}
          onClick={() => onChange(t.key)}
          className={`relative px-3 py-2 text-[13px] font-medium transition-colors ${
            active === t.key ? "text-ink" : "text-ink3 hover:text-ink2"
          }`}
        >
          {t.label}
          {typeof t.count === "number" && (
            <span className="ml-1.5 rounded bg-panel2 px-1.5 py-0.5 text-[11px] tabular-nums text-ink2">
              {t.count}
            </span>
          )}
          {active === t.key && (
            <span className="absolute inset-x-2 -bottom-px h-0.5 rounded-full bg-brand" />
          )}
        </button>
      ))}
    </div>
  );
}

export function EmptyState({ icon, text }: { icon: ReactNode; text: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-12 text-ink3">
      {icon}
      <p className="text-[13px]">{text}</p>
    </div>
  );
}

export function Select({
  value,
  onChange,
  options,
  className = "",
}: {
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
  className?: string;
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className={`input cursor-pointer ${className}`}
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

export function Modal({
  title,
  open,
  onClose,
  footer,
  children,
  width = 560,
}: {
  title: string;
  open: boolean;
  onClose: () => void;
  footer?: ReactNode;
  children: ReactNode;
  width?: number;
}) {
  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/50 p-4 pt-20"
      onClick={onClose}
    >
      <div
        className="panel w-full"
        style={{ maxWidth: width }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <h2 className="text-[13px] font-semibold text-ink">{title}</h2>
          <button className="btn-ghost rounded p-1 text-ink3" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="max-h-[60vh] overflow-y-auto p-4">{children}</div>
        {footer && (
          <div className="flex justify-end gap-2 border-t border-border px-4 py-3">{footer}</div>
        )}
      </div>
    </div>
  );
}

export function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: ReactNode;
  hint?: string;
}) {
  return (
    <div className="mb-3">
      <label className="label">{label}</label>
      {children}
      {hint && <p className="mt-1 text-xs text-ink3">{hint}</p>}
    </div>
  );
}
