import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { FileBox, Brain, FileText, BarChart3, Download, Search } from "lucide-react";
import { PageHeader, Panel, Select } from "../components/primitives";
import { fetchArtifacts, type ArtifactRow } from "../lib/consoleApi";
import { fmtRelative } from "../lib/format";

const KIND_ICON = { checkpoint: FileBox, model: Brain, log: FileText, eval: BarChart3 };

export function ArtifactsPage() {
  const nav = useNavigate();
  const [kind, setKind] = useState("all");
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<ArtifactRow[]>([]);

  useEffect(() => {
    let alive = true;
    fetchArtifacts().then((r) => alive && setRows(r));
    return () => {
      alive = false;
    };
  }, []);

  const filtered = rows.filter(
    (r) => (kind === "all" || r.kind === kind) && (!q || r.name.toLowerCase().includes(q.toLowerCase()) || r.jobName.includes(q))
  );

  return (
    <div>
      <PageHeader title="Artifacts" subtitle="模型、checkpoint、日志与评估结果" />

      <Panel className="mb-3" bodyClass="p-3">
        <div className="flex items-center gap-2">
          <div className="relative w-56">
            <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-ink3" />
            <input className="input pl-8" placeholder="artifact 或 job name…" value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
          <Select
            value={kind}
            onChange={setKind}
            options={[
              { value: "all", label: "All kinds" },
              { value: "checkpoint", label: "Checkpoint" },
              { value: "model", label: "Model" },
              { value: "log", label: "Log" },
              { value: "eval", label: "Eval" },
            ]}
            className="w-40"
          />
          <span className="ml-auto text-xs text-ink3">{filtered.length} artifacts</span>
        </div>
      </Panel>

      <Panel bodyClass="p-0">
        <table className="w-full text-[13px]">
          <thead>
            <tr className="border-b border-border text-left text-xs text-ink3">
              <th className="px-3 py-2 font-medium">Name</th>
              <th className="px-3 py-2 font-medium">Kind</th>
              <th className="px-3 py-2 font-medium">Job</th>
              <th className="px-3 py-2 font-medium">Path</th>
              <th className="px-3 py-2 text-right font-medium">Size</th>
              <th className="px-3 py-2 font-medium">Created</th>
              <th className="px-3 py-2 text-right font-medium">Action</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((a, i) => {
              const Icon = KIND_ICON[a.kind];
              return (
                <tr key={i} className="border-b border-border/50 last:border-0 hover:bg-panel2">
                  <td className="px-3 py-2.5">
                    <span className="flex items-center gap-2 font-medium text-ink">
                      <Icon size={14} className="text-ink3" />
                      {a.name}
                    </span>
                  </td>
                  <td className="px-3 py-2.5 text-ink2">{a.kind}</td>
                  <td className="px-3 py-2.5">
                    <button className="text-brand hover:underline" onClick={() => nav(`/jobs/${a.jobId}`)}>
                      {a.jobName}
                    </button>
                  </td>
                  <td className="px-3 py-2.5 font-mono text-xs text-ink3">{a.path}</td>
                  <td className="px-3 py-2.5 text-right tabular-nums text-ink2">{a.size}</td>
                  <td className="px-3 py-2.5 text-ink3">{fmtRelative(a.created_at)}</td>
                  <td className="px-3 py-2.5 text-right">
                    <button className="btn btn-sm">
                      <Download size={12} /> Download
                    </button>
                  </td>
                </tr>
              );
            })}
            {filtered.length === 0 && (
              <tr>
                <td colSpan={7} className="px-4 py-10 text-center text-ink3">
                  没有匹配的 artifacts
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </Panel>
    </div>
  );
}
