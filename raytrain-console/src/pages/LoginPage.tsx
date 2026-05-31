import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { User, Lock, KeyRound, Loader } from "lucide-react";
import { login, setToken, whoami, clearToken, errMsg } from "../lib/api";

type Mode = "password" | "token";

export function LoginPage() {
  const nav = useNavigate();
  const [mode, setMode] = useState<Mode>("password");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [token, setTok] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      if (mode === "password") {
        if (!username.trim() || !password) {
          setErr("请输入用户名和密码");
          return;
        }
        await login(username.trim(), password);
        nav("/overview");
      } else {
        if (!token.trim()) {
          setErr("请输入访问令牌");
          return;
        }
        setToken(token.trim());
        await whoami(); // validate
        nav("/overview");
      }
    } catch (e2) {
      clearToken();
      setErr(mode === "password" ? "用户名或密码错误" : errMsg(e2) || "token 无效或已过期");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-bg">
      <form onSubmit={submit} className="w-[400px] rounded-lg border border-border bg-panel p-7">
        <div className="mb-5 flex items-center gap-2.5">
          <div className="flex h-8 w-8 items-center justify-center rounded-md bg-brand text-sm font-bold text-white">
            r
          </div>
          <div>
            <div className="font-semibold text-ink">raytrain console</div>
            <div className="text-xs text-ink3">训练任务工作台</div>
          </div>
        </div>

        {/* mode tabs */}
        <div className="mb-4 flex rounded-md border border-border p-0.5">
          <button
            type="button"
            onClick={() => { setMode("password"); setErr(""); }}
            className={`flex-1 rounded px-3 py-1.5 text-[13px] transition-colors ${
              mode === "password" ? "bg-panel2 font-medium text-ink" : "text-ink3 hover:text-ink2"
            }`}
          >
            账号密码登录
          </button>
          <button
            type="button"
            onClick={() => { setMode("token"); setErr(""); }}
            className={`flex-1 rounded px-3 py-1.5 text-[13px] transition-colors ${
              mode === "token" ? "bg-panel2 font-medium text-ink" : "text-ink3 hover:text-ink2"
            }`}
          >
            令牌登录
          </button>
        </div>

        {mode === "password" ? (
          <>
            <label className="label">用户名</label>
            <div className="relative mb-3">
              <User size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-ink3" />
              <input
                autoFocus
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="zhangsan"
                className="input pl-8"
              />
            </div>
            <label className="label">密码</label>
            <div className="relative">
              <Lock size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-ink3" />
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                className="input pl-8"
              />
            </div>
          </>
        ) : (
          <>
            <label className="label">访问令牌 (Token)</label>
            <div className="relative">
              <KeyRound size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-ink3" />
              <input
                type="password"
                autoFocus
                value={token}
                onChange={(e) => setTok(e.target.value)}
                placeholder="eyJhbGc..."
                className="input pl-8"
              />
            </div>
          </>
        )}

        {err && <p className="mt-2 text-xs text-failed">{err}</p>}

        <button type="submit" disabled={busy} className="btn btn-primary mt-4 w-full justify-center">
          {busy && <Loader size={14} className="animate-spin" />}
          登录
        </button>

        <p className="mt-4 text-xs leading-relaxed text-ink3">
          {mode === "password"
            ? "账号由管理员在「Admin · 用户」中创建并设置密码。忘记密码请联系管理员重置。"
            : "令牌用于自动化 / CLI；浏览器登录推荐使用账号密码。"}
        </p>
      </form>
    </div>
  );
}
