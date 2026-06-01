import { useEffect, useState } from "react";
import { Database, Lock, Globe, Users, Loader } from "lucide-react";
import { PageHeader, Panel } from "../components/primitives";
import { apiFetch, errMsg } from "../lib/api";

interface DS {
  name: string;
  type: string;
  uri: string;
  visibility: "private" | "tenant" | "public";
  rows: string;
  size: string;
  mountPath: string;
  tags: string[];
}

// Backend /v1/datasets response (raytrain_server/api/datasets.py).
interface ApiDataset {
  name: string;
  type: string;
  uri: string;
  visibility: "private" | "tenant" | "public";
  rows: number;
  size_bytes: number;
  tags: string[];
}

function fmtBytes(n: number): string {
  if (!n) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(1)} ${u[i]}`;
}

const VIS = {
  private: { icon: Lock, cls: "border-failed/40 bg-failed/10 text-failed" },
  tenant: { icon: Users, cls: "border-brand/40 bg-brand/10 text-brand" },
  public: { icon: Globe, cls: "border-succeeded/40 bg-succeeded/10 text-succeeded" },
};

export function DatasetsPage() {
  const [datasets, setDatasets] = useState<DS[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    apiFetch<ApiDataset[]>("/v1/datasets")
      .then((rows) => {
        if (!alive) return;
        setDatasets(
          rows.map((r) => ({
            name: r.name,
            type: r.type,
            uri: r.uri,
            visibility: r.visibility,
            rows: r.rows ? r.rows.toLocaleString() : "—",
            size: fmtBytes(r.size_bytes),
            mountPath: `/data/${r.name}`,
            tags: r.tags || [],
          }))
        );
      })
      .catch((e) => alive && setErr(errMsg(e)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, []);

  return (
    <div>
      <PageHeader
        title="Datasets"
        subtitle="数据挂载注册表。提交训练时选中即注入到 /data（只读）"
      />
      {err && (
        <div className="mb-3 rounded-md border border-failed/30 bg-failed/10 px-3 py-2 text-xs text-failed">{err}</div>
      )}
      {loading && (
        <div className="py-12 text-center text-ink3"><Loader size={18} className="mx-auto animate-spin" /></div>
      )}
      {!loading && !err && datasets.length === 0 && (
        <Panel bodyClass="py-12 text-center text-ink3">
          还没有注册数据集。数据集由平台管理员注册（Lance/对象存储），注册后会出现在这里并可在创建训练时选择。
        </Panel>
      )}
      <div className="grid grid-cols-2 gap-3">
        {datasets.map((d) => {
          const V = VIS[d.visibility];
          const Icon = V.icon;
          return (
            <Panel key={d.name} bodyClass="p-4">
              <div className="flex items-start justify-between">
                <div className="flex items-center gap-2">
                  <Database size={16} className="text-ink3" />
                  <div>
                    <div className="font-semibold text-ink">{d.name}</div>
                    <div className="font-mono text-xs text-ink3">{d.uri}</div>
                  </div>
                </div>
                <span className={`chip ${V.cls}`}>
                  <Icon size={12} />
                  {d.visibility}
                </span>
              </div>
              <div className="mt-3 grid grid-cols-3 gap-2 border-y border-border py-2 text-center text-[13px]">
                <div>
                  <div className="font-medium tabular-nums text-ink">{d.rows}</div>
                  <div className="text-xs text-ink3">rows</div>
                </div>
                <div>
                  <div className="font-medium text-ink">{d.size}</div>
                  <div className="text-xs text-ink3">size</div>
                </div>
                <div>
                  <div className="font-medium uppercase text-ink">{d.type}</div>
                  <div className="text-xs text-ink3">format</div>
                </div>
              </div>
              <div className="mt-2 flex items-center justify-between">
                <div className="flex flex-wrap gap-1">
                  {d.tags.map((t) => (
                    <span key={t} className="chip border-borderc bg-panel2 text-ink3">
                      {t}
                    </span>
                  ))}
                </div>
                <code className="text-xs text-ink3">{d.mountPath}</code>
              </div>
            </Panel>
          );
        })}
      </div>
    </div>
  );
}
