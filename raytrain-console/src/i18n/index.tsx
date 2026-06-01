// Lightweight i18n: a LanguageProvider with a `t(key, vars)` helper and a
// `lang` toggle, persisted to localStorage. Default locale is zh; any missing
// key falls back to zh, then to the raw key (so a forgotten string is visible
// but never crashes). No external dependency — keeps the bundle lean.

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { MESSAGES, type Locale } from "./messages";

const LANG_KEY = "raytrain.console.lang";

function initialLocale(): Locale {
  const saved = (typeof localStorage !== "undefined" && localStorage.getItem(LANG_KEY)) || "";
  return saved === "en" || saved === "zh" ? saved : "zh";
}

interface I18nValue {
  lang: Locale;
  setLang: (l: Locale) => void;
  t: (key: string, vars?: Record<string, string | number>) => string;
}

const Ctx = createContext<I18nValue | null>(null);

function interpolate(template: string, vars?: Record<string, string | number>): string {
  if (!vars) return template;
  return template.replace(/\{(\w+)\}/g, (_, k) =>
    k in vars ? String(vars[k]) : `{${k}}`
  );
}

export function LanguageProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Locale>(initialLocale);

  const setLang = useCallback((l: Locale) => {
    setLangState(l);
    try {
      localStorage.setItem(LANG_KEY, l);
    } catch {
      /* ignore */
    }
  }, []);

  const t = useCallback(
    (key: string, vars?: Record<string, string | number>): string => {
      const table = MESSAGES[lang] || MESSAGES.zh;
      const raw = table[key] ?? MESSAGES.zh[key] ?? key;
      return interpolate(raw, vars);
    },
    [lang]
  );

  const value = useMemo<I18nValue>(() => ({ lang, setLang, t }), [lang, setLang, t]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useI18n(): I18nValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useI18n must be used within LanguageProvider");
  return v;
}

// Localize an ApiError-ish object by its FriendlyError code, falling back to
// the server-provided message + hint. Used by toast/error surfaces.
export function localizeError(
  t: (k: string, vars?: Record<string, string | number>) => string,
  e: { code?: string; message?: string; hint?: string } | null | undefined
): string {
  if (!e) return "";
  const localized = e.code ? t(`err.${e.code}`) : "";
  // t() returns the key itself when missing → treat that as "no translation".
  const base = localized && localized !== `err.${e.code}` ? localized : e.message || "";
  return e.hint ? `${base}（${e.hint}）` : base;
}
