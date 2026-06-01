import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { User, Lock, KeyRound, Loader, Languages } from "lucide-react";
import { login, setToken, whoami, clearToken, errMsg } from "../lib/api";
import { useI18n } from "../i18n";

type Mode = "password" | "token";

export function LoginPage() {
  const nav = useNavigate();
  const { t, lang, setLang } = useI18n();
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
          setErr(t("login.needCreds"));
          return;
        }
        await login(username.trim(), password);
        nav("/overview");
      } else {
        if (!token.trim()) {
          setErr(t("login.needToken"));
          return;
        }
        setToken(token.trim());
        await whoami(); // validate
        nav("/overview");
      }
    } catch (e2) {
      clearToken();
      setErr(mode === "password" ? t("login.badCreds") : errMsg(e2) || t("login.badToken"));
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
            <div className="font-semibold text-ink">{t("login.title")}</div>
            <div className="text-xs text-ink3">{t("login.subtitle")}</div>
          </div>
          <button
            type="button"
            onClick={() => setLang(lang === "zh" ? "en" : "zh")}
            className="ml-auto flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs text-ink2 hover:bg-panel2"
          >
            <Languages size={13} className="text-ink3" />
            {lang === "zh" ? "中文" : "EN"}
          </button>
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
            {t("login.byPassword")}
          </button>
          <button
            type="button"
            onClick={() => { setMode("token"); setErr(""); }}
            className={`flex-1 rounded px-3 py-1.5 text-[13px] transition-colors ${
              mode === "token" ? "bg-panel2 font-medium text-ink" : "text-ink3 hover:text-ink2"
            }`}
          >
            {t("login.byToken")}
          </button>
        </div>

        {mode === "password" ? (
          <>
            <label className="label">{t("login.username")}</label>
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
            <label className="label">{t("login.password")}</label>
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
            <label className="label">{t("login.token")}</label>
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
          {t("login.submit")}
        </button>

        <p className="mt-4 text-xs leading-relaxed text-ink3">
          {mode === "password" ? t("login.hintPassword") : t("login.hintToken")}
        </p>
      </form>
    </div>
  );
}
