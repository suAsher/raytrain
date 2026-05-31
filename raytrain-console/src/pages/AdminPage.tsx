import { useState } from "react";
import {
  FolderKanban,
  Gauge,
  Users,
  Cpu,
  Layers,
  Container,
  Server,
  Plus,
  Pencil,
} from "lucide-react";
import { PageHeader, Panel } from "../components/primitives";
import { UsersAdmin } from "../components/UsersAdmin";
import { ResourceAdmin } from "../components/ResourceAdmin";
import { QueueAdmin } from "../components/QueueAdmin";
import type { ResourceKind } from "../lib/consoleApi";
import { ADMIN } from "../lib/mockData";

const SECTIONS = [
  { key: "Users / Roles", icon: Users },
  { key: "Projects", icon: FolderKanban },
  { key: "QuotaGroups", icon: Gauge },
  { key: "Queues", icon: Layers },
  { key: "Runtime Images", icon: Container },
  { key: "Resource Profiles", icon: Cpu },
  { key: "Node ResourceFlavors", icon: Server },
];

// Sections backed by a real resource kind (interactive CRUD).
const RESOURCE_KIND: Record<string, ResourceKind> = {
  Projects: "project",
  QuotaGroups: "quota_group",
  "Runtime Images": "runtime_image",
};

// Remaining read-only sections still use mock catalog data.
const READONLY_MOCK_KEY: Record<string, string> = {
  "Resource Profiles": "Resource Profiles",
  "Node ResourceFlavors": "Node ResourceFlavors",
};

export function AdminPage() {
  const [active, setActive] = useState(SECTIONS[0].key);
  const readonlyEntities = ADMIN[READONLY_MOCK_KEY[active]] || [];

  return (
    <div>
      <PageHeader
        title="Admin"
        subtitle="用户/项目/配额组/队列/镜像可在线管理；资源模板与节点 Flavor 暂为只读"
      />
      <div className="grid grid-cols-4 gap-4">
        {/* section nav */}
        <div className="col-span-1">
          <Panel bodyClass="p-1.5">
            {SECTIONS.map((s) => {
              const Icon = s.icon;
              const manageable = s.key === "Users / Roles" || s.key === "Queues" || s.key in RESOURCE_KIND;
              return (
                <button
                  key={s.key}
                  onClick={() => setActive(s.key)}
                  className={`mb-0.5 flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-left text-[13px] transition-colors ${
                    active === s.key ? "bg-brand/15 font-medium text-ink" : "text-ink2 hover:bg-panel2"
                  }`}
                >
                  <Icon size={15} />
                  {s.key}
                  {!manageable && <span className="ml-auto text-[10px] text-ink3">只读</span>}
                </button>
              );
            })}
          </Panel>
        </div>

        {/* content */}
        <div className="col-span-3">
          {active === "Users / Roles" ? (
            <UsersAdmin />
          ) : active === "Queues" ? (
            <QueueAdmin />
          ) : active in RESOURCE_KIND ? (
            <ResourceAdmin kind={RESOURCE_KIND[active]} />
          ) : (
            <Panel
              title={active}
              right={
                <button className="btn btn-sm" title="该资源类型暂为只读" disabled>
                  <Plus size={12} /> New
                </button>
              }
              bodyClass="p-0"
            >
              <table className="w-full text-[13px]">
                <thead>
                  <tr className="border-b border-border text-left text-xs text-ink3">
                    <th className="px-4 py-2 font-medium">Name</th>
                    <th className="px-4 py-2 font-medium">Spec</th>
                    <th className="px-4 py-2 font-medium">Detail</th>
                    <th className="px-4 py-2 font-medium">Status</th>
                    <th className="px-4 py-2 text-right font-medium">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {readonlyEntities.map((e) => (
                    <tr key={e.id} className="border-b border-border/50 last:border-0 hover:bg-panel2">
                      <td className="px-4 py-2.5 font-medium text-ink">{e.name}</td>
                      <td className="px-4 py-2.5 text-ink2">{e.meta}</td>
                      <td className="px-4 py-2.5 text-ink3">{e.detail}</td>
                      <td className="px-4 py-2.5">
                        {e.status && (
                          <span
                            className={`chip ${
                              e.status === "disabled" || e.status === "down"
                                ? "border-failed/40 bg-failed/10 text-failed"
                                : e.status === "degraded"
                                ? "border-queued/40 bg-queued/10 text-queued"
                                : "border-succeeded/40 bg-succeeded/10 text-succeeded"
                            }`}
                          >
                            {e.status}
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-2.5 text-right">
                        <button className="btn-ghost rounded p-1.5 text-ink3" title="该资源类型暂为只读" disabled>
                          <Pencil size={13} />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Panel>
          )}
        </div>
      </div>
    </div>
  );
}
