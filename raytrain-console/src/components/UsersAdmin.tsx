import { useEffect, useState } from "react";
import { Plus, Pencil, Trash2, KeyRound, Loader, RefreshCw } from "lucide-react";
import { Panel, Modal, Field, Select } from "./primitives";
import {
  fetchUsers,
  createUser,
  updateUser,
  deleteUser,
  type PlatformUser,
  type CreateUserBody,
} from "../lib/consoleApi";
import { errMsg } from "../lib/api";

const EMPTY_FORM = {
  user: "",
  tenant: "default",
  role: "user" as "user" | "admin",
  password: "",
  max_gpus: 0,
  max_jobs: 0,
  max_cpus: 0,
  max_memory_gi: 0,
  projects: "",
  datasets: "",
  image_prefixes: "",
};
type FormState = typeof EMPTY_FORM;

function csv(s: string): string[] {
  return s.split(",").map((x) => x.trim()).filter(Boolean);
}

export function UsersAdmin() {
  const [users, setUsers] = useState<PlatformUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [editing, setEditing] = useState<PlatformUser | null>(null);
  const [pwUser, setPwUser] = useState<PlatformUser | null>(null);

  const load = () => {
    setLoading(true);
    setErr("");
    fetchUsers()
      .then(setUsers)
      .catch((e) => setErr(errMsg(e)))
      .finally(() => setLoading(false));
  };
  useEffect(load, []);

  return (
    <Panel
      title="Users / Roles"
      right={
        <div className="flex items-center gap-2">
          <button className="btn-ghost rounded p-1 text-ink3" onClick={load} title="刷新">
            <RefreshCw size={13} />
          </button>
          <button className="btn btn-sm" onClick={() => setCreateOpen(true)}>
            <Plus size={12} /> 新建用户
          </button>
        </div>
      }
      bodyClass="p-0"
    >
      {err && <div className="border-b border-failed/30 bg-failed/10 px-4 py-2 text-xs text-failed">{err}</div>}
      <table className="w-full text-[13px]">
        <thead>
          <tr className="border-b border-border text-left text-xs text-ink3">
            <th className="px-4 py-2 font-medium">用户</th>
            <th className="px-4 py-2 font-medium">租户</th>
            <th className="px-4 py-2 font-medium">角色</th>
            <th className="px-4 py-2 font-medium">配额 (GPU/Jobs)</th>
            <th className="px-4 py-2 font-medium">授权项目</th>
            <th className="px-4 py-2 font-medium">状态</th>
            <th className="px-4 py-2 text-right font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.user} className="border-b border-border/50 last:border-0 hover:bg-panel2">
              <td className="px-4 py-2.5 font-medium text-ink">{u.user}</td>
              <td className="px-4 py-2.5 text-ink2">{u.tenant}</td>
              <td className="px-4 py-2.5">
                <span className={`chip ${u.role === "admin" ? "border-amber-500/40 bg-amber-500/10 text-amber-400" : "border-borderc bg-panel2 text-ink2"}`}>
                  {u.role}
                </span>
              </td>
              <td className="px-4 py-2.5 tabular-nums text-ink2">
                {u.quota.max_gpus || "∞"} / {u.quota.max_jobs || "∞"}
              </td>
              <td className="px-4 py-2.5 text-ink3">{u.projects.join(", ") || "—"}</td>
              <td className="px-4 py-2.5">
                <span className={`chip ${u.enabled ? "border-succeeded/40 bg-succeeded/10 text-succeeded" : "border-failed/40 bg-failed/10 text-failed"}`}>
                  {u.enabled ? "启用" : "禁用"}
                </span>
              </td>
              <td className="px-4 py-2.5">
                <div className="flex items-center justify-end gap-0.5">
                  <button className="btn-ghost rounded p-1.5 text-ink3" title="编辑" onClick={() => setEditing(u)}>
                    <Pencil size={13} />
                  </button>
                  <button className="btn-ghost rounded p-1.5 text-ink3" title="重置密码" onClick={() => setPwUser(u)}>
                    <KeyRound size={13} />
                  </button>
                  <button
                    className="btn-ghost rounded p-1.5 text-failed"
                    title="删除"
                    onClick={async () => {
                      if (!confirm(`删除用户 ${u.user}？其 token 立即失效。`)) return;
                      try {
                        await deleteUser(u.user);
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
          {!loading && users.length === 0 && (
            <tr>
              <td colSpan={7} className="px-4 py-8 text-center text-ink3">
                还没有用户，点右上角「新建用户」
              </td>
            </tr>
          )}
          {loading && (
            <tr>
              <td colSpan={7} className="px-4 py-8 text-center text-ink3">
                <Loader size={16} className="mx-auto animate-spin" />
              </td>
            </tr>
          )}
        </tbody>
      </table>

      {createOpen && (
        <CreateUserModal
          onClose={() => setCreateOpen(false)}
          onDone={() => {
            setCreateOpen(false);
            load();
          }}
        />
      )}
      {editing && (
        <EditUserModal
          user={editing}
          onClose={() => setEditing(null)}
          onDone={() => {
            setEditing(null);
            load();
          }}
        />
      )}
      {pwUser && (
        <PasswordModal
          user={pwUser}
          onClose={() => setPwUser(null)}
          onDone={() => setPwUser(null)}
        />
      )}
    </Panel>
  );
}

function QuotaFields({ f, set }: { f: FormState; set: (p: Partial<FormState>) => void }) {
  return (
    <div className="grid grid-cols-2 gap-3">
      <Field label="GPU 上限 (0=不限)">
        <input type="number" min={0} className="input" value={f.max_gpus} onChange={(e) => set({ max_gpus: +e.target.value })} />
      </Field>
      <Field label="并发任务上限 (0=不限)">
        <input type="number" min={0} className="input" value={f.max_jobs} onChange={(e) => set({ max_jobs: +e.target.value })} />
      </Field>
      <Field label="CPU 上限 (0=不限)">
        <input type="number" min={0} className="input" value={f.max_cpus} onChange={(e) => set({ max_cpus: +e.target.value })} />
      </Field>
      <Field label="内存上限 GiB (0=不限)">
        <input type="number" min={0} className="input" value={f.max_memory_gi} onChange={(e) => set({ max_memory_gi: +e.target.value })} />
      </Field>
    </div>
  );
}

function GrantFields({ f, set }: { f: FormState; set: (p: Partial<FormState>) => void }) {
  return (
    <>
      <Field label="授权项目 (逗号分隔)">
        <input className="input" value={f.projects} onChange={(e) => set({ projects: e.target.value })} placeholder="pointcept, sslod26" />
      </Field>
      <Field label="授权数据集 (逗号分隔)">
        <input className="input" value={f.datasets} onChange={(e) => set({ datasets: e.target.value })} placeholder="scannet, nuscenes" />
      </Field>
      <Field label="允许镜像前缀 (逗号分隔)">
        <input className="input" value={f.image_prefixes} onChange={(e) => set({ image_prefixes: e.target.value })} placeholder="raytrain/" />
      </Field>
    </>
  );
}

function CreateUserModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [f, setF] = useState<FormState>(EMPTY_FORM);
  const set = (p: Partial<FormState>) => setF((prev) => ({ ...prev, ...p }));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const submit = async () => {
    if (!f.user.trim()) return setErr("用户名必填");
    if (f.password && f.password.length < 6) return setErr("密码至少 6 位");
    setBusy(true);
    setErr("");
    const body: CreateUserBody = {
      user: f.user.trim(),
      tenant: f.tenant || "default",
      role: f.role,
      password: f.password || undefined,
      quota: { max_gpus: f.max_gpus, max_jobs: f.max_jobs, max_cpus: f.max_cpus, max_memory_gi: f.max_memory_gi },
      projects: csv(f.projects),
      datasets: csv(f.datasets),
      image_prefixes: csv(f.image_prefixes),
    };
    try {
      await createUser(body);
      onDone();
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title="新建用户"
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
        <Field label="用户名">
          <input className="input" value={f.user} onChange={(e) => set({ user: e.target.value })} placeholder="zhangsan" />
        </Field>
        <Field label="租户">
          <input className="input" value={f.tenant} onChange={(e) => set({ tenant: e.target.value })} />
        </Field>
        <Field label="角色">
          <Select value={f.role} onChange={(v) => set({ role: v as "user" | "admin" })} options={[{ value: "user", label: "user" }, { value: "admin", label: "admin" }]} />
        </Field>
        <Field label="登录密码" hint="设置后该用户即可账号密码登录；留空则仅令牌登录">
          <input type="password" className="input" value={f.password} onChange={(e) => set({ password: e.target.value })} placeholder="≥ 6 位" />
        </Field>
      </div>
      <QuotaFields f={f} set={set} />
      <GrantFields f={f} set={set} />
    </Modal>
  );
}

function EditUserModal({ user, onClose, onDone }: { user: PlatformUser; onClose: () => void; onDone: () => void }) {
  const [f, setF] = useState<FormState>({
    ...EMPTY_FORM,
    user: user.user,
    tenant: user.tenant,
    role: user.role,
    max_gpus: user.quota.max_gpus,
    max_jobs: user.quota.max_jobs,
    max_cpus: user.quota.max_cpus,
    max_memory_gi: user.quota.max_memory_gi,
    projects: user.projects.join(", "),
    datasets: user.datasets.join(", "),
    image_prefixes: user.image_prefixes.join(", "),
  });
  const set = (p: Partial<FormState>) => setF((prev) => ({ ...prev, ...p }));
  const [enabled, setEnabled] = useState(user.enabled);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const submit = async () => {
    setBusy(true);
    setErr("");
    try {
      await updateUser(user.user, {
        role: f.role,
        enabled,
        quota: { max_gpus: f.max_gpus, max_jobs: f.max_jobs, max_cpus: f.max_cpus, max_memory_gi: f.max_memory_gi },
        projects: csv(f.projects),
        datasets: csv(f.datasets),
        image_prefixes: csv(f.image_prefixes),
      });
      onDone();
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={`编辑用户 · ${user.user}`}
      open
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn btn-primary" disabled={busy} onClick={submit}>
            {busy && <Loader size={13} className="animate-spin" />} 保存
          </button>
        </>
      }
    >
      {err && <div className="mb-3 rounded-md border border-failed/30 bg-failed/10 px-3 py-2 text-xs text-failed">{err}</div>}
      <div className="grid grid-cols-2 gap-3">
        <Field label="角色">
          <Select value={f.role} onChange={(v) => set({ role: v as "user" | "admin" })} options={[{ value: "user", label: "user" }, { value: "admin", label: "admin" }]} />
        </Field>
        <Field label="状态">
          <Select value={enabled ? "1" : "0"} onChange={(v) => setEnabled(v === "1")} options={[{ value: "1", label: "启用" }, { value: "0", label: "禁用" }]} />
        </Field>
      </div>
      <QuotaFields f={f} set={set} />
      <GrantFields f={f} set={set} />
    </Modal>
  );
}

function PasswordModal({ user, onClose, onDone }: { user: PlatformUser; onClose: () => void; onDone: () => void }) {
  const [pw, setPw] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const submit = async () => {
    if (pw.length < 6) return setErr("密码至少 6 位");
    setBusy(true);
    setErr("");
    try {
      await updateUser(user.user, { password: pw });
      onDone();
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      title={`重置密码 · ${user.user}`}
      open
      width={420}
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>取消</button>
          <button className="btn btn-primary" disabled={busy} onClick={submit}>
            {busy && <Loader size={13} className="animate-spin" />} 设置新密码
          </button>
        </>
      }
    >
      {err && <div className="mb-3 rounded-md border border-failed/30 bg-failed/10 px-3 py-2 text-xs text-failed">{err}</div>}
      <Field label="新密码" hint="设置后该用户用新密码登录">
        <input type="password" autoFocus className="input" value={pw} onChange={(e) => setPw(e.target.value)} placeholder="≥ 6 位" />
      </Field>
    </Modal>
  );
}
