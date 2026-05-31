import { useEffect, useState } from "react";
import { Plus, Trash2, Loader, RefreshCw } from "lucide-react";
import { Panel, Modal, Field, Select } from "./primitives";
import {
  fetchAdminQueues,
  createQueue,
  deleteQueue,
  type AdminQueue,
} from "../lib/consoleApi";
import { errMsg } from "../lib/api";

export function QueueAdmin() {
  const [queues, setQueues] = useState<AdminQueue[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [creating, setCreating] = useState(false);

  const load = () => {
    setLoading(true);
    setErr("");
    fetchAdminQueues()
      .then(setQueues)
      .catch((e) => setErr(errMsg(e)))
      .finally(() => setLoading(false));
  };
  useEffect(load, []);

  return (
    <Panel
      title="Queues"
      right={
        <div className="flex items-center gap-2">
          <button className="btn-ghost rounded p-1 text-ink3" onClick={load} title="刷新">
            <RefreshCw size={13} />
          </button>
          <button className="btn btn-sm" onClick={() => setCreating(true)}>
            <Plus size={12} /> 新建队列
          </button>
        </div>
      }
      bodyClass="p-0"
    >
      {err && <div className="border-b border-failed/30 bg-failed/10 px-4 py-2 text-xs text-failed">{err}</div>}
      <table className="w-full text-[13px]">
        <thead>
          <tr className="border-b border-border text-left text-xs text-ink3">
            <th className="px-4 py-2 font-medium">Queue</th>
            <th className="px-4 py-2 font-medium">ClusterQueue</th>
            <th className="px-4 py-2 font-medium">GPU</th>
            <th className="px-4 py-2 font-medium">Nominal</th>
            <th className="px-4 py-2 font-medium">Used / Pending</th>
            <th className="px-4 py-2 font-medium">健康</th>
            <th className="px-4 py-2 text-right font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          {queues.map((q) => (
            <tr key={q.name} className="border-b border-border/50 last:border-0 hover:bg-panel2">
              <td className="px-4 py-2.5 font-medium text-ink">{q.name}</td>
              <td className="px-4 py-2.5 font-mono text-xs text-ink3">{q.cluster_queue}</td>
              <td className="px-4 py-2.5 text-ink2">{q.gpu_type}</td>
              <td className="px-4 py-2.5 tabular-nums text-ink2">{q.nominal}</td>
              <td className="px-4 py-2.5 tabular-nums text-ink2">{q.used} / {q.pending}</td>
              <td className="px-4 py-2.5">
                <span className={`chip ${q.health === "healthy" ? "border-succeeded/40 bg-succeeded/10 text-succeeded" : "border-queued/40 bg-queued/10 text-queued"}`}>
                  {q.health}
                </span>
              </td>
              <td className="px-4 py-2.5">
                <div className="flex justify-end">
                  <button
                    className="btn-ghost rounded p-1.5 text-failed"
                    title="删除"
                    onClick={async () => {
                      if (!confirm(`删除队列 ${q.name}？`)) return;
                      try {
                        await deleteQueue(q.name);
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
          {loading && (
            <tr>
              <td colSpan={7} className="px-4 py-8 text-center text-ink3">
                <Loader size={16} className="mx-auto animate-spin" />
              </td>
            </tr>
          )}
        </tbody>
      </table>

      {creating && (
        <CreateQueueModal
          onClose={() => setCreating(false)}
          onDone={() => {
            setCreating(false);
            load();
          }}
        />
      )}
    </Panel>
  );
}

function CreateQueueModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState("");
  const [cq, setCq] = useState("cq-h20");
  const [gpu, setGpu] = useState("H20");
  const [nominal, setNominal] = useState(32);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const submit = async () => {
    if (!name.trim()) return setErr("队列名必填");
    setBusy(true);
    setErr("");
    try {
      await createQueue({ name: name.trim(), cluster_queue: cq, gpu_type: gpu, nominal });
      onDone();
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title="新建队列"
      open
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn btn-primary" disabled={busy} onClick={submit}>
            {busy && <Loader size={13} className="animate-spin" />} 创建
          </button>
        </>
      }
    >
      {err && <div className="mb-3 rounded-md border border-failed/30 bg-failed/10 px-3 py-2 text-xs text-failed">{err}</div>}
      <div className="grid grid-cols-2 gap-3">
        <Field label="队列名">
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="h20-research" />
        </Field>
        <Field label="ClusterQueue">
          <input className="input" value={cq} onChange={(e) => setCq(e.target.value)} />
        </Field>
        <Field label="GPU 类型">
          <Select value={gpu} onChange={setGpu} options={[
            { value: "H20", label: "H20" },
            { value: "A100", label: "A100" },
            { value: "CPU-only", label: "CPU-only" },
          ]} />
        </Field>
        <Field label="Nominal 配额 (GPU)">
          <input type="number" min={0} className="input" value={nominal} onChange={(e) => setNominal(+e.target.value)} />
        </Field>
      </div>
    </Modal>
  );
}
