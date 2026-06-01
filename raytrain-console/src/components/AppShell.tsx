import { NavLink, Outlet, useNavigate } from "react-router-dom";
import {
  LayoutDashboard,
  ListTree,
  PlusCircle,
  Layers,
  FlaskConical,
  Package,
  Database,
  Shield,
  Search,
  ChevronDown,
  Cpu,
  Zap,
  MemoryStick,
  User,
  CloudCog,
  Languages,
} from "lucide-react";
import { useState } from "react";
import { StoreProvider, useStore } from "../lib/store";
import { pct } from "../lib/format";
import { useI18n } from "../i18n";

const NAV = [
  { to: "/overview", key: "nav.overview", icon: LayoutDashboard },
  { to: "/workspaces", key: "nav.workspaces", icon: CloudCog },
  { to: "/jobs", key: "nav.jobs", icon: ListTree },
  { to: "/jobs/new", key: "nav.create", icon: PlusCircle },
  { to: "/queues", key: "nav.queues", icon: Layers },
  { to: "/experiments", key: "nav.experiments", icon: FlaskConical },
  { to: "/artifacts", key: "nav.artifacts", icon: Package },
  { to: "/datasets", key: "nav.datasets", icon: Database },
  { to: "/admin", key: "nav.admin", icon: Shield },
];

function Sidebar() {
  const { t } = useI18n();
  return (
    <aside className="flex w-56 flex-shrink-0 flex-col border-r border-border bg-panel">
      <div className="flex h-12 items-center gap-2 border-b border-border px-4">
        <div className="flex h-6 w-6 items-center justify-center rounded bg-brand text-[13px] font-bold text-white">
          r
        </div>
        <span className="font-semibold text-ink">raytrain</span>
        <span className="ml-auto rounded bg-panel2 px-1.5 py-0.5 text-[10px] text-ink3">
          {t("app.console")}
        </span>
      </div>
      <nav className="flex-1 overflow-y-auto p-2">
        {NAV.map((n) => {
          const Icon = n.icon;
          return (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.to === "/jobs"}
              className={({ isActive }) =>
                `mb-0.5 flex items-center gap-2.5 rounded-md px-2.5 py-2 text-[13px] transition-colors ${
                  isActive
                    ? "bg-brand/15 font-medium text-ink"
                    : "text-ink2 hover:bg-panel2 hover:text-ink"
                }`
              }
            >
              <Icon size={16} />
              {t(n.key)}
            </NavLink>
          );
        })}
      </nav>
      <div className="border-t border-border p-3 text-[11px] text-ink3">
        <div className="flex items-center justify-between">
          <span>{t("app.cluster")}</span>
          <span className="flex items-center gap-1 text-succeeded">
            <span className="h-1.5 w-1.5 rounded-full bg-succeeded" />
            {t("app.healthy")}
          </span>
        </div>
        <div className="mt-1">KubeRay 2.54 · Kueue 0.8</div>
      </div>
    </aside>
  );
}

function QuotaPill({
  icon: Icon,
  label,
  used,
  total,
  unit,
}: {
  icon: typeof Cpu;
  label: string;
  used: number;
  total: number;
  unit?: string;
}) {
  const p = pct(used, total);
  const tone = total === 0 ? "text-ink2" : p >= 90 ? "text-failed" : p >= 75 ? "text-queued" : "text-ink";
  return (
    <div className="flex items-center gap-2 rounded-md border border-border bg-panel px-2.5 py-1">
      <Icon size={14} className="text-ink3" />
      <span className="text-[11px] text-ink3">{label}</span>
      <span className={`text-xs font-medium tabular-nums ${tone}`}>
        {used}
        {unit} / {total === 0 ? "∞" : `${total}${unit || ""}`}
      </span>
    </div>
  );
}

function LangToggle() {
  const { lang, setLang, t } = useI18n();
  return (
    <button
      onClick={() => setLang(lang === "zh" ? "en" : "zh")}
      title={t("app.lang")}
      className="flex items-center gap-1.5 rounded-md border border-border bg-panel px-2 py-1 text-[13px] text-ink2 hover:bg-panel2"
    >
      <Languages size={14} className="text-ink3" />
      {lang === "zh" ? "中文" : "EN"}
    </button>
  );
}

function TopBar() {
  const { project, setProject, projects, me, logout, quota } = useStore();
  const { t } = useI18n();
  const [openProj, setOpenProj] = useState(false);
  const [openUser, setOpenUser] = useState(false);
  const nav = useNavigate();
  const tenant = me?.tenant || "—";

  return (
    <header className="flex h-12 flex-shrink-0 items-center gap-3 border-b border-border bg-panel px-4">
      {/* tenant / project context */}
      <div className="flex items-center gap-1.5 text-[13px]">
        <span className="text-ink3">{tenant}</span>
        <span className="text-ink3">/</span>
        <div className="relative">
          <button
            onClick={() => setOpenProj((v) => !v)}
            className="flex items-center gap-1 rounded-md px-2 py-1 font-medium text-ink hover:bg-panel2"
          >
            {project}
            <ChevronDown size={14} className="text-ink3" />
          </button>
          {openProj && (
            <>
              <div className="fixed inset-0 z-10" onClick={() => setOpenProj(false)} />
              <div className="absolute left-0 top-9 z-20 w-48 rounded-md border border-borderc bg-panel2 py-1 shadow-xl">
                {projects.map((p) => (
                  <button
                    key={p}
                    onClick={() => {
                      setProject(p);
                      setOpenProj(false);
                    }}
                    className={`block w-full px-3 py-1.5 text-left text-[13px] hover:bg-panel ${
                      p === project ? "text-brand" : "text-ink2"
                    }`}
                  >
                    {p}
                  </button>
                ))}
              </div>
            </>
          )}
        </div>
      </div>

      {/* quota summary */}
      <div className="ml-auto flex items-center gap-2">
        <QuotaPill icon={Zap} label="GPU" used={quota.gpu.used} total={quota.gpu.total} />
        <QuotaPill icon={Cpu} label="CPU" used={quota.cpu.used} total={quota.cpu.total} />
        <QuotaPill icon={MemoryStick} label="MEM" used={quota.memGi.used} total={quota.memGi.total} unit="Gi" />
      </div>

      {/* global search */}
      <div className="relative w-56">
        <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-ink3" />
        <input
          placeholder={t("app.search")}
          onKeyDown={(e) => {
            if (e.key === "Enter") nav("/jobs");
          }}
          className="input pl-8"
        />
      </div>

      <LangToggle />

      <div className="relative">
        <button
          onClick={() => setOpenUser((v) => !v)}
          className="flex items-center gap-2 rounded-md border border-border bg-panel px-2 py-1 hover:bg-panel2"
        >
          <span className="flex h-6 w-6 items-center justify-center rounded-full bg-brand text-[11px] font-medium text-white">
            {(me?.user || "?").charAt(0).toUpperCase()}
          </span>
          <span className="text-[13px] text-ink2">{me ? `${me.user}` : "…"}</span>
          {me?.role === "admin" && (
            <span className="rounded bg-amber-500/15 px-1 text-[10px] text-amber-400">admin</span>
          )}
          <User size={14} className="text-ink3" />
        </button>
        {openUser && (
          <>
            <div className="fixed inset-0 z-10" onClick={() => setOpenUser(false)} />
            <div className="absolute right-0 top-9 z-20 w-44 rounded-md border border-borderc bg-panel2 py-1 shadow-xl">
              <div className="px-3 py-1.5 text-xs text-ink3">
                {me?.user} · {me?.role}
              </div>
              <button
                onClick={logout}
                className="block w-full px-3 py-1.5 text-left text-[13px] text-ink2 hover:bg-panel"
              >
                {t("app.logout")}
              </button>
            </div>
          </>
        )}
      </div>
    </header>
  );
}

export function AppShell() {
  return (
    <StoreProvider>
      <div className="flex h-screen overflow-hidden">
        <Sidebar />
        <div className="flex flex-1 flex-col overflow-hidden">
          <TopBar />
          <main className="flex-1 overflow-y-auto p-6">
            <Outlet />
          </main>
        </div>
      </div>
    </StoreProvider>
  );
}
