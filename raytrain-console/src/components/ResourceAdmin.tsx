import { useEffect, useState } from "react";
import { Plus, Pencil, Trash2, Loader, RefreshCw } from "lucide-react";
import { Panel, Modal, Field } from "./primitives";
import {
  fetchResources,
  createResource,
  updateResource,
  deleteResource,
  type ResourceKind,
  type ResourceRecord,
} from "../lib/consoleApi";
import { errMsg } from "../lib/api";

// Per-kind spec field schema so one component manages all three catalog kinds.
interface SpecField {
  key: string;
  label: string;
  type?: "text" | "number";
}

const SCHEMA: Record<ResourceKind, { title: string; fields: SpecField[] }> = {
  project: {
    title: "Projects",
    fields: [
      { key: "owner", label: "Owner" },
      { key: "description", label: "描述" },
    ],
  },
  quota_group: {
    title: "QuotaGroups",
    fields: [
      { key: "gpu_type", label: "GPU 类型" },
      { key: "max_gpus", label: "GPU 上限", type: "number" },
      { key: "max_cpus", label: "CPU 上限", type: "number" },
    ],
  },
  runtime_image: {
    title: "Runtime Images",
    fields: [
      { key: "uri", label: "镜像 URI" },
      { key: "cuda", label: "CUDA 版本" },
      { key: "framework", label: "框架" },
    ],
  },
};

export function ResourceAdmin({ kind }: { kind: ResourceKind }) {
  const schema = SCHEMA[kind];
  const [items, setItems] = useState<ResourceRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [editing, setEditing] = useState<ResourceRecord | null>(null);
  const [creating, setCreating] = useState(false);

  const load = () => {
    setLoading(true);
    setErr("");
    fetchResources(kind)
      .then(setItems)
      .catch((e) => setErr(errMsg(e)))
      .finally(() => setLoading(false));
  };
  useEffect(load, [kind]);

  return (
    <Panel
      title={schema.title}
      right={
        <div className="flex items-center gap-2">
          <button className="btn-ghost rounded p-1 text-ink3" onClick={load} title="刷新">
            <RefreshCw size={13} />
          </button>
          <button className="btn btn-sm" onClick={() => setCreating(true)}>
            <Plus size={12} /> 新建
          </button>
        </div>
      }
      bodyClass="p-0"
    >
      {err && <div className="border-b border-failed/30 bg-failed/10 px-4 py-2 text-xs text-failed">{err}</div>}
      <table className="w-full text-[13px]">
        <thead>
          <tr className="border-b border-border text-left text-xs text-ink3">
            <th className="px-4 py-2 font-medium">Name</th>
            {schema.fields.map((f) => (
              <th key={f.key} className="px-4 py-2 font-medium">{f.label}</th>
            ))}
            <th className="px-4 py-2 font-medium">状态</th>
            <th className="px-4 py-2 text-right font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          {items.map((r) => (
            <tr key={r.id} className="border-b border-border/50 last:border-0 hover:bg-panel2">
              <td className="px-4 py-2.5 font-medium text-ink">{r.name}</td>
              {schema.fields.map((f) => (
                <td key={f.key} className="px-4 py-2.5 text-ink2">{String(r.spec[f.key] ?? "—")}</td>
              ))}
              <td className="px-4 py-2.5">
                <span className={`chip ${r.enabled ? "border-succeeded/40 bg-succeeded/10 text-succeeded" : "border-failed/40 bg-failed/10 text-failed"}`}>
                  {r.enabled ? "启用" : "禁用"}
                </span>
              </td>
              <td className="px-4 py-2.5">
                <div className="flex items-center justify-end gap-0.5">
                  <button className="btn-ghost rounded p-1.5 text-ink3" title="编辑" onClick={() => setEditing(r)}>
                    <Pencil size={13} />
                  </button>
                  <button
                    className="btn-ghost rounded p-1.5 text-failed"
                    title="删除"
                    onClick={async () => {
                      if (!confirm(`删除 ${r.name}？`)) return;
                      try {
                        await deleteResource(kind, r.id);
                        load();
                      } catch (e) {
                        alert(errMsg(e));
                      }
                    }}
                  >
                    <Trash2 size={13} />
                  </button>
                </div>
              </td>
            </tr>
          ))}
          {!loading && items.length === 0 && (
            <tr>
              <td colSpan={schema.fields.length + 3} className="px-4 py-8 text-center text-ink3">
                还没有记录，点右上角「新建」
              </td>
            </tr>
          )}
          {loading && (
            <tr>
              <td colSpan={schema.fields.length + 3} className="px-4 py-8 text-center text-ink3">
                <Loader size={16} className="mx-auto animate-spin" />
              </td>
            </tr>
          )}
        </tbody>
      </table>

      {(creating || editing) && (
        <ResourceModal
          kind={kind}
          schema={schema}
          record={editing}
          onClose={() => {
            setCreating(false);
            setEditing(null);
          }}
          onDone={() => {
            setCreating(false);
            setEditing(null);
            load();
          }}
        />
      )}
    </Panel>
  );
}

function ResourceModal({
  kind,
  schema,
  record,
  onClose,
  onDone,
}: {
  kind: ResourceKind;
  schema: { title: string; fields: SpecField[] };
  record: ResourceRecord | null;
  onClose: () => void;
  onDone: () => void;
}) {
  const [name, setName] = useState(record?.name || "");
  const [spec, setSpec] = useState<Record<string, string>>(() => {
    const init: Record<string, string> = {};
    schema.fields.forEach((f) => {
      init[f.key] = record ? String(record.spec[f.key] ?? "") : "";
    });
    return init;
  });
  const [enabled, setEnabled] = useState(record?.enabled ?? true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const submit = async () => {
    if (!name.trim()) return setErr("Name 必填");
    setBusy(true);
    setErr("");
    // coerce number fields
    const outSpec: Record<string, string | number> = {};
    schema.fields.forEach((f) => {
      const v = spec[f.key];
      outSpec[f.key] = f.type === "number" ? Number(v || 0) : v;
    });
    try {
      if (record) {
        await updateResource(kind, record.id, { name: name.trim(), spec: outSpec, enabled });
      } else {
        await createResource(kind, { name: name.trim(), spec: outSpec, enabled });
      }
      onDone();
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={`${record ? "编辑" : "新建"} · ${schema.title}`}
      open
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn btn-primary" disabled={busy} onClick={submit}>
            {busy && <Loader size={13} className="animate-spin" />} {record ? "保存" : "创建"}
          </button>
        </>
      }
    >
      {err && <div className="mb-3 rounded-md border border-failed/30 bg-failed/10 px-3 py-2 text-xs text-failed">{err}</div>}
      <Field label="Name">
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
      </Field>
      {schema.fields.map((f) => (
        <Field key={f.key} label={f.label}>
          <input
            type={f.type === "number" ? "number" : "text"}
            className="input"
            value={spec[f.key]}
            onChange={(e) => setSpec((p) => ({ ...p, [f.key]: e.target.value }))}
          />
        </Field>
      ))}
      {record && (
        <label className="mt-1 flex items-center gap-2 text-[13px] text-ink2">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          启用
        </label>
      )}
    </Modal>
  );
}
