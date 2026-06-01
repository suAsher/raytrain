import { useState } from "react";
import {
  FolderKanban,
  Gauge,
  Users,
  Cpu,
  Layers,
  Container,
  Server,
} from "lucide-react";
import { PageHeader, Panel } from "../components/primitives";
import { UsersAdmin } from "../components/UsersAdmin";
import { ResourceAdmin } from "../components/ResourceAdmin";
import { QueueAdmin } from "../components/QueueAdmin";
import type { ResourceKind } from "../lib/consoleApi";

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

export function AdminPage() {
  const [active, setActive] = useState(SECTIONS[0].key);

  return (
    <div>
      <PageHeader
        title="Admin"
        subtitle="用户/项目/配额组/队列/镜像可在线管理；资源模板与节点 Flavor 由集群配置决定"
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
            <Panel title={active} bodyClass="py-12 text-center text-ink3">
              {active === "Resource Profiles"
                ? "资源模板由集群上的 Kueue / 调度配置决定，暂不在控制台编辑。"
                : "节点 ResourceFlavor 由集群上的 Kueue ResourceFlavor 决定，暂不在控制台编辑。"}
            </Panel>
          )}
        </div>
      </div>
    </div>
  );
}
