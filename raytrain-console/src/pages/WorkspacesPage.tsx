import { useEffect, useState } from "react";
import {
  CloudCog,
  Cpu,
  Play,
  Square,
  Trash2,
  Plus,
  Loader,
  RefreshCw,
  TerminalSquare,
  Zap,
  ExternalLink,
} from "lucide-react";
import { PageHeader, Panel, Modal, Field, Select } from "../components/primitives";
import {
  fetchWorkspaces,
  createWorkspace,
  workspaceAction,
  fetchDevSessions,
  createDevSession,
  deleteDevSession,
  type Workspace,
  type DevSession,
} from "../lib/consoleApi";
import { errMsg } from "../lib/api";

function stateChip(state: string) {
  const s = state.toLowerCase();
  if (s === "running") return "border-succeeded/40 bg-succeeded/10 text-succeeded";
  if (s === "creating" || s === "starting") return "border-queued/40 bg-queued/10 text-queued";
  if (s === "stopped" || s === "expired") return "border-cancelled/40 bg-cancelled/10 text-ink3";
  return "border-borderc bg-panel2 text-ink2";
}

export function WorkspacesPage() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [sessions, setSessions] = useState<DevSession[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [createWs, setCreateWs] = useState(false);
  const [gpuFor, setGpuFor] = useState<Workspace | null>(null);

  const load = () => {
    setLoading(true);
    setErr("");
    Promise.all([fetchWorkspaces(), fetchDevSessions()])
      .then(([ws, ds]) => {
        setWorkspaces(ws);
        setSessions(ds);
      })
      .catch((e) => setErr(errMsg(e)))
      .finally(() => setLoading(false));
  };
  useEffect(load, []);

  const sessFor = (wsId: string) => sessions.filter((s) => s.workspace_id === wsId);

  return (
    <div>
      <PageHeader
        title="开发机 Workspaces"
        subtitle="浏览器内的个人开发环境（Jupyter / VS Code / SSH）+ 按需挂 GPU 调试。代码改完直接提交训练（code-as-submission）"
        actions={
          <div className="flex items-center gap-2">
            <button className="btn-ghost rounded p-1.5 text-ink3" onClick={load} title="刷新">
              <RefreshCw size={14} />
            </button>
            <button className="btn btn-primary" onClick={() => setCreateWs(true)}>
              <Plus size={14} /> 新建开发机
            </button>
          </div>
        }
      />

      {err && <div className="mb-3 rounded-md border border-failed/30 bg-failed/10 px-3 py-2 text-xs text-failed">{err}</div>}

      {loading && (
        <div className="py-12 text-center text-ink3">
          <Loader size={18} className="mx-auto animate-spin" />
        </div>
      )}

      {!loading && workspaces.length === 0 && (
        <Panel bodyClass="py-12 text-center text-ink3">
          还没有开发机。点右上角「新建开发机」创建一台（CPU + 持久卷），进 IDE 写代码。
        </Panel>
      )}

      <div className="space-y-3">
        {workspaces.map((ws) => {
          const sess = sessFor(ws.id);
          return (
            <Panel key={ws.id} bodyClass="p-0">
              <div className="flex items-center justify-between border-b border-border px-4 py-3">
                <div className="flex items-center gap-3">
                  <CloudCog size={18} className="text-ink3" />
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="font-semibold text-ink">{ws.name}</span>
                      <span className={`chip ${stateChip(ws.state)}`}>{ws.state}</span>
                      {ws.pod_phase && <span className="text-xs text-ink3">{ws.pod_phase}</span>}
                    </div>
                    <div className="mt-0.5 text-xs text-ink3">
                      {ws.cpu} vCPU · {ws.memory_gi} GiB · PVC {ws.pvc_gi} GiB · {ws.image}
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-1">
                  {Object.entries(ws.ide_urls || {}).map(([k, url]) => (
                    <a key={k} href={url} target="_blank" rel="noreferrer" className="btn btn-sm">
                      {k === "ssh" ? <TerminalSquare size={12} /> : <ExternalLink size={12} />} {k}
                    </a>
                  ))}
                  <button className="btn btn-sm" onClick={() => setGpuFor(ws)} title="挂 GPU 调试会话">
                    <Zap size={12} /> 挂 GPU
                  </button>
                  {ws.state === "stopped" ? (
                    <button className="btn-ghost rounded p-1.5 text-ink3" title="启动" onClick={() => act(ws.id, "start", load, setErr)}>
                      <Play size={14} />
                    </button>
                  ) : (
                    <button className="btn-ghost rounded p-1.5 text-ink3" title="停止" onClick={() => act(ws.id, "stop", load, setErr)}>
                      <Square size={14} />
                    </button>
                  )}
                  <button
                    className="btn-ghost rounded p-1.5 text-failed"
                    title="删除"
                    onClick={() => {
                      if (confirm(`删除开发机 ${ws.name}？（含其持久卷）`)) act(ws.id, "delete", load, setErr);
                    }}
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              </div>

              {/* attached GPU debug sessions */}
              <div className="px-4 py-2">
                {sess.length === 0 ? (
                  <div className="py-1 text-xs text-ink3">无 GPU 调试会话。点「挂 GPU」给这台开发机挂卡联调，空闲自动回收。</div>
                ) : (
                  <table className="w-full text-[13px]">
                    <thead>
                      <tr className="text-left text-xs text-ink3">
                        <th className="py-1.5 font-medium">调试会话</th>
                        <th className="py-1.5 font-medium">GPU</th>
                        <th className="py-1.5 font-medium">状态</th>
                        <th className="py-1.5 font-medium">IDE</th>
                        <th className="py-1.5 text-right font-medium">操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      {sess.map((s) => (
                        <tr key={s.id} className="border-t border-border/50">
                          <td className="py-1.5 font-mono text-xs text-ink2">{s.id}</td>
                          <td className="py-1.5 text-ink2">
                            <span className="inline-flex items-center gap-1">
                              <Cpu size={12} className="text-ink3" /> {s.gpu_type} × {s.gpu_count}
                            </span>
                          </td>
                          <td className="py-1.5">
                            <span className={`chip ${stateChip(s.state)}`}>{s.state}</span>
                          </td>
                          <td className="py-1.5">
                            {Object.entries(s.ide_urls || {}).map(([k, url]) => (
                              <a key={k} href={url} target="_blank" rel="noreferrer" className="mr-2 text-brand hover:underline">{k}</a>
                            ))}
                          </td>
                          <td className="py-1.5 text-right">
                            <button
                              className="btn-ghost rounded p-1 text-failed"
                              title="结束会话"
                              onClick={async () => {
                                try {
                                  await deleteDevSession(s.id);
                                  load();
                                } catch (e) {
                                  setErr(errMsg(e));
                                }
                              }}
                            >
                              <Trash2 size={13} />
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            </Panel>
          );
        })}
      </div>

      {createWs && (
        <CreateWorkspaceModal onClose={() => setCreateWs(false)} onDone={() => { setCreateWs(false); load(); }} />
      )}
      {gpuFor && (
        <AttachGpuModal ws={gpuFor} onClose={() => setGpuFor(null)} onDone={() => { setGpuFor(null); load(); }} />
      )}
    </div>
  );
}

async function act(id: string, action: "start" | "stop" | "delete", reload: () => void, setErr: (s: string) => void) {
  try {
    await workspaceAction(id, action);
    reload();
  } catch (e) {
    setErr(errMsg(e));
  }
}

function CreateWorkspaceModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState("");
  const [cpu, setCpu] = useState(4);
  const [mem, setMem] = useState(8);
  const [pvc, setPvc] = useState(100);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const submit = async () => {
    if (!name.trim()) return setErr("名称必填");
    setBusy(true);
    setErr("");
    try {
      await createWorkspace({ name: name.trim(), cpu, memory_gi: mem, pvc_gi: pvc });
      onDone();
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      title="新建开发机"
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
      <Field label="名称"><input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="ws-pointcept" /></Field>
      <div className="grid grid-cols-3 gap-3">
        <Field label="CPU"><input type="number" min={1} className="input" value={cpu} onChange={(e) => setCpu(+e.target.value)} /></Field>
        <Field label="内存 GiB"><input type="number" min={1} className="input" value={mem} onChange={(e) => setMem(+e.target.value)} /></Field>
        <Field label="PVC GiB"><input type="number" min={10} className="input" value={pvc} onChange={(e) => setPvc(+e.target.value)} /></Field>
      </div>
      <p className="text-xs text-ink3">开发机是常驻 CPU Pod + 持久卷，内置 Jupyter / VS Code / SSH。需要 GPU 联调时在卡片上「挂 GPU」开调试会话。</p>
    </Modal>
  );
}

function AttachGpuModal({ ws, onClose, onDone }: { ws: Workspace; onClose: () => void; onDone: () => void }) {
  const [gpuType, setGpuType] = useState("h20");
  const [count, setCount] = useState(1);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const submit = async () => {
    setBusy(true);
    setErr("");
    try {
      await createDevSession({ workspace_id: ws.id, gpu_type: gpuType, gpu_count: count });
      onDone();
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      title={`挂 GPU 调试会话 · ${ws.name}`}
      open
      width={420}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn btn-primary" disabled={busy} onClick={submit}>
            {busy && <Loader size={13} className="animate-spin" />} 创建会话
          </button>
        </>
      }
    >
      {err && <div className="mb-3 rounded-md border border-failed/30 bg-failed/10 px-3 py-2 text-xs text-failed">{err}</div>}
      <div className="grid grid-cols-2 gap-3">
        <Field label="GPU 类型">
          <Select value={gpuType} onChange={setGpuType} options={[{ value: "h20", label: "h20" }, { value: "a100", label: "a100" }]} />
        </Field>
        <Field label="GPU 数量"><input type="number" min={1} max={8} className="input" value={count} onChange={(e) => setCount(+e.target.value)} /></Field>
      </div>
      <p className="text-xs text-ink3">调试会话挂载本开发机的同一个持久卷（共享代码），空闲超时自动回收，避免占用 GPU。</p>
    </Modal>
  );
}
