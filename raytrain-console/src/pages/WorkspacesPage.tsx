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
  AlertTriangle,
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
import { useI18n } from "../i18n";

function stateChip(state: string) {
  const s = state.toLowerCase();
  if (s === "running") return "border-succeeded/40 bg-succeeded/10 text-succeeded";
  if (s === "creating" || s === "starting") return "border-queued/40 bg-queued/10 text-queued";
  if (s === "stopping") return "border-queued/40 bg-queued/10 text-queued";
  if (s === "error") return "border-failed/40 bg-failed/10 text-failed";
  if (s === "stopped" || s === "expired") return "border-cancelled/40 bg-cancelled/10 text-ink3";
  return "border-borderc bg-panel2 text-ink2";
}

export function WorkspacesPage() {
  const { t } = useI18n();
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
        title={t("ws.title")}
        subtitle={t("ws.subtitle")}
        actions={
          <div className="flex items-center gap-2">
            <button className="btn-ghost rounded p-1.5 text-ink3" onClick={load} title={t("common.refresh")}>
              <RefreshCw size={14} />
            </button>
            <button className="btn btn-primary" onClick={() => setCreateWs(true)}>
              <Plus size={14} /> {t("ws.new")}
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

      {!loading && workspaces.length === 0 && !err && (
        <Panel bodyClass="py-12 text-center text-ink3">{t("ws.empty")}</Panel>
      )}

      <div className="space-y-3">
        {workspaces.map((ws) => {
          const sess = sessFor(ws.id);
          const isRunning = ws.state === "running";
          const ideLinks = Object.entries(ws.ide_urls || {});
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
                    {ws.reason && (
                      <div className="mt-1 flex items-center gap-1 text-xs text-failed">
                        <AlertTriangle size={12} /> {ws.reason}
                      </div>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-1">
                  {/* IDE/SSH entries only when truly running (Req 3.6) */}
                  {isRunning && ideLinks.length > 0 ? (
                    ideLinks.map(([k, url]) => (
                      <a key={k} href={url} target="_blank" rel="noreferrer" className="btn btn-sm">
                        {k === "ssh" ? <TerminalSquare size={12} /> : <ExternalLink size={12} />} {k}
                      </a>
                    ))
                  ) : (
                    <span className="mr-1 text-xs text-ink3" title={t("ws.notReady")}>
                      {t("ws.notReady")}
                    </span>
                  )}
                  <button
                    className="btn btn-sm"
                    onClick={() => setGpuFor(ws)}
                    title={t("ws.attachGpuTitle")}
                    disabled={!isRunning}
                  >
                    <Zap size={12} /> {t("ws.attachGpu")}
                  </button>
                  {ws.state === "stopped" ? (
                    <button className="btn-ghost rounded p-1.5 text-ink3" title={t("common.start")} onClick={() => act(ws.id, "start", load, setErr)}>
                      <Play size={14} />
                    </button>
                  ) : (
                    <button
                      className="btn-ghost rounded p-1.5 text-ink3"
                      title={t("common.stop")}
                      disabled={ws.state === "stopping"}
                      onClick={() => act(ws.id, "stop", load, setErr)}
                    >
                      <Square size={14} />
                    </button>
                  )}
                  <button
                    className="btn-ghost rounded p-1.5 text-failed"
                    title={t("common.delete")}
                    onClick={() => {
                      if (confirm(t("ws.confirmDelete", { name: ws.name }))) act(ws.id, "delete", load, setErr);
                    }}
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              </div>

              {/* attached GPU debug sessions */}
              <div className="px-4 py-2">
                {sess.length === 0 ? (
                  <div className="py-1 text-xs text-ink3">{t("ws.noSession")}</div>
                ) : (
                  <table className="w-full text-[13px]">
                    <thead>
                      <tr className="text-left text-xs text-ink3">
                        <th className="py-1.5 font-medium">{t("ws.sessionCol")}</th>
                        <th className="py-1.5 font-medium">GPU</th>
                        <th className="py-1.5 font-medium">{t("common.status")}</th>
                        <th className="py-1.5 font-medium">{t("ws.ide")}</th>
                        <th className="py-1.5 text-right font-medium">{t("common.actions")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {sess.map((s) => {
                        const sRunning = s.state === "running";
                        const sLinks = Object.entries(s.ide_urls || {});
                        return (
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
                              {sRunning && sLinks.length > 0
                                ? sLinks.map(([k, url]) => (
                                    <a key={k} href={url} target="_blank" rel="noreferrer" className="mr-2 text-brand hover:underline">
                                      {k}
                                    </a>
                                  ))
                                : <span className="text-xs text-ink3">—</span>}
                            </td>
                            <td className="py-1.5 text-right">
                              <button
                                className="btn-ghost rounded p-1 text-failed"
                                title={t("ws.endSession")}
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
                        );
                      })}
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
  const { t } = useI18n();
  const [name, setName] = useState("");
  const [cpu, setCpu] = useState(4);
  const [mem, setMem] = useState(8);
  const [pvc, setPvc] = useState(100);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const submit = async () => {
    if (!name.trim()) return setErr(t("ws.nameRequired"));
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
      title={t("ws.createTitle")}
      open
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>{t("common.cancel")}</button>
          <button className="btn btn-primary" disabled={busy} onClick={submit}>
            {busy && <Loader size={13} className="animate-spin" />} {t("common.create")}
          </button>
        </>
      }
    >
      {err && <div className="mb-3 rounded-md border border-failed/30 bg-failed/10 px-3 py-2 text-xs text-failed">{err}</div>}
      <Field label={t("ws.name")}><input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="ws-pointcept" /></Field>
      <div className="grid grid-cols-3 gap-3">
        <Field label={t("ws.cpu")}><input type="number" min={1} className="input" value={cpu} onChange={(e) => setCpu(+e.target.value)} /></Field>
        <Field label={t("ws.memGi")}><input type="number" min={1} className="input" value={mem} onChange={(e) => setMem(+e.target.value)} /></Field>
        <Field label={t("ws.pvcGi")}><input type="number" min={10} className="input" value={pvc} onChange={(e) => setPvc(+e.target.value)} /></Field>
      </div>
      <p className="text-xs text-ink3">{t("ws.createHint")}</p>
    </Modal>
  );
}

function AttachGpuModal({ ws, onClose, onDone }: { ws: Workspace; onClose: () => void; onDone: () => void }) {
  const { t } = useI18n();
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
      title={`${t("ws.attachGpuTitle")} · ${ws.name}`}
      open
      width={420}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>{t("common.cancel")}</button>
          <button className="btn btn-primary" disabled={busy} onClick={submit}>
            {busy && <Loader size={13} className="animate-spin" />} {t("ws.createSession")}
          </button>
        </>
      }
    >
      {err && <div className="mb-3 rounded-md border border-failed/30 bg-failed/10 px-3 py-2 text-xs text-failed">{err}</div>}
      <div className="grid grid-cols-2 gap-3">
        <Field label={t("ws.gpuType")}>
          <Select value={gpuType} onChange={setGpuType} options={[{ value: "h20", label: "h20" }, { value: "a100", label: "a100" }]} />
        </Field>
        <Field label={t("ws.gpuCount")}><input type="number" min={1} max={8} className="input" value={count} onChange={(e) => setCount(+e.target.value)} /></Field>
      </div>
      <p className="text-xs text-ink3">{t("ws.gpuHint")}</p>
    </Modal>
  );
}
