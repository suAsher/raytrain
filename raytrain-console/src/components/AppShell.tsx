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
} from "lucide-react";
import { useState, useEffect } from "react";
import { StoreProvider, useStore, ALL_PROJECTS } from "../lib/store";
import { pct } from "../lib/format";
import { onMockChange, usingMock } from "../lib/api";

const NAV = [
  { to: "/overview", label: "Overview", icon: LayoutDashboard },
  { to: "/workspaces", label: "开发机", icon: CloudCog },
  { to: "/jobs", label: "Training Jobs", icon: ListTree },
  { to: "/jobs/new", label: "Create Job", icon: PlusCircle },
  { to: "/queues", label: "Queues", icon: Layers },
  { to: "/experiments", label: "Experiments", icon: FlaskConical },
  { to: "/artifacts", label: "Artifacts", icon: Package },
  { to: "/datasets", label: "Datasets", icon: Database },
  { to: "/admin", label: "Admin", icon: Shield },
];

function Sidebar() {
  return (
    <aside className="flex w-56 flex-shrink-0 flex-col border-r border-border bg-panel">
      <div className="flex h-12 items-center gap-2 border-b border-border px-4">
        <div className="flex h-6 w-6 items-center justify-center rounded bg-brand text-[13px] font-bold text-white">
          r
        </div>
        <span className="font-semibold text-ink">raytrain</span>
        <span className="ml-auto rounded bg-panel2 px-1.5 py-0.5 text-[10px] text-ink3">
          console
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
              {n.label}
            </NavLink>
          );
        })}
      </nav>
      <div className="border-t border-border p-3 text-[11px] text-ink3">
        <div className="flex items-center justify-between">
          <span>cluster</span>
          <span className="flex items-center gap-1 text-succeeded">
            <span className="h-1.5 w-1.5 rounded-full bg-succeeded" />
            healthy
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
  const tone = p >= 90 ? "text-failed" : p >= 75 ? "text-queued" : "text-ink";
  return (
    <div className="flex items-center gap-2 rounded-md border border-border bg-panel px-2.5 py-1">
      <Icon size={14} className="text-ink3" />
      <span className="text-[11px] text-ink3">{label}</span>
      <span className={`text-xs font-medium tabular-nums ${tone}`}>
        {used}
        {unit} / {total}
        {unit}
      </span>
    </div>
  );
}

function TopBar() {
  const { project, setProject, me, logout, quota } = useStore();
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
                {ALL_PROJECTS.map((p) => (
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

      <span className="chip border-borderc bg-panel2 text-ink2">QuotaGroup: research-default</span>

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
          placeholder="Search jobs, queues…"
          onKeyDown={(e) => {
            if (e.key === "Enter") nav("/jobs");
          }}
          className="input pl-8"
        />
      </div>

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
                退出登录
              </button>
            </div>
          </>
        )}
      </div>
    </header>
  );
}

function DemoBanner() {
  const [mock, setMock] = useState(usingMock());
  useEffect(() => onMockChange(setMock), []);
  if (!mock) return null;
  return (
    <div className="flex items-center justify-center gap-2 bg-amber-500/10 px-4 py-1.5 text-center text-xs text-amber-400">
      <span className="h-1.5 w-1.5 rounded-full bg-amber-400" />
      演示数据：后端暂不可达，当前页面显示本地示例数据（请检查登录态或后端连接）
    </div>
  );
}

export function AppShell() {
  return (
    <StoreProvider>
      <div className="flex h-screen overflow-hidden">
        <Sidebar />
        <div className="flex flex-1 flex-col overflow-hidden">
          <TopBar />
          <DemoBanner />
          <main className="flex-1 overflow-y-auto p-6">
            <Outlet />
          </main>
        </div>
      </div>
    </StoreProvider>
  );
}
