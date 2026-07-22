/**
 * Benchmarks screen — credible model benchmarks from public sources.
 *
 * The user said: "Benchmarks should ideally be derived from credible sources,
 * benchLM offers free sources where we can have very insightful benchmarks for
 * free as a starting point. All downloadable and inferable."
 *
 * Sources (free, public, credible):
 *   - HF Open LLM Leaderboard   — https://huggingface.co/spaces/open-llm-leaderboard/open_llm_leaderboard
 *   - Artificial Analysis        — https://artificialanalysis.ai/
 *   - LMArena (Chatbot Arena)    — https://lmarena.ai/
 *   - SWE-bench Leaderboard      — https://www.swebench.com/
 *   - OpenRouter Model docs      — https://openrouter.ai/models
 *
 * Layout:
 *   - Two tabs at the top: "Benchmarks" (curated scores from credible sources)
 *     and "Live Pricing" (the existing OpenRouter-backed /api/benchmarks
 *     data — capabilities + pricing + context).
 *   - Benchmarks tab:
 *       • Filter by category: coding, math, reasoning, agentic, overall.
 *       • Sortable by any single benchmark score (descending).
 *       • Search by model name.
 *       • Source attribution per row.
 *       • "Refresh from sources" button (best-effort HF datasets-server fetch).
 *       • Mobile: cards instead of a wide table.
 */

import { useEffect, useState, useMemo, useCallback } from "react";
import { AgentClient } from "../api/agent";
import type { Settings } from "../api/panel";
import {
  BENCHMARK_ENTRIES,
  fetchOpenLLMLeaderboard,
  type BenchmarkEntry,
  type BenchmarkCategory,
  type BenchmarkScore,
} from "../lib/benchmarks-data";

// ── Legacy OpenRouter-backed live pricing/capabilities data ──────────────
interface BenchmarkModel {
  id: string;
  name: string;
  context_length: number;
  prompt_price: string;
  completion_price: string;
  cost_per_1m: number;
  is_free: boolean;
  description: string;
  modality: string;
  capabilities: string[];
  input_modalities: string[];
  output_modalities: string[];
  tokenizer: string;
  knowledge_cutoff: string;
  supported_params: string[];
}

interface BenchmarksData {
  models: BenchmarkModel[];
  count: number;
  free_count: number;
  vision_count: number;
  coding_count: number;
  agentic_count: number;
}

type SortBy = "context" | "cost" | "name";
type CapabilityFilter = "all" | "vision" | "coding" | "agentic" | "reasoning" | "tools";

const CAP_ICONS: Record<string, string> = {
  vision: "👁️",
  coding: "💻",
  reasoning: "🧠",
  agentic: "🤖",
  tools: "🔧",
};

// ── Credible-sources (curated) tab types ─────────────────────────────────
type CatFilter = "all" | BenchmarkCategory | "overall";

const CAT_LABELS: Record<CatFilter, string> = {
  all: "All",
  coding: "💻 Coding",
  math: "📐 Math",
  reasoning: "🧠 Reasoning",
  agentic: "🤖 Agentic",
  overall: "🏆 Overall",
};

/** Distinct benchmark ids that appear in the dataset, in first-seen order. */
function allBenchmarkIds(entries: BenchmarkEntry[]): { id: string; label: string; category: string }[] {
  const seen = new Map<string, { id: string; label: string; category: string }>();
  for (const e of entries) {
    for (const s of e.scores) {
      if (!seen.has(s.id)) seen.set(s.id, { id: s.id, label: s.label, category: s.category });
    }
  }
  return [...seen.values()];
}

/** Family color — purple-forward palette so it matches the theme. */
function familyColor(family: string): string {
  switch (family) {
    case "claude": return "#d97757";     // orange-ish (Anthropic)
    case "gpt": return "#10a37f";        // teal (OpenAI)
    case "gemini": return "#4285f4";     // blue (Google)
    case "glm": return "#a855f7";        // purple (Zhipu)
    case "deepseek": return "#6b8cff";   // indigo
    case "kimi": return "#000000";       // black (Moonshot) — uses border
    case "llama": return "#0866ff";      // blue (Meta)
    case "qwen": return "#6b2fa5";       // purple
    case "mistral": return "#ff7000";    // orange
    default: return "#8b95a3";
  }
}

export function BenchmarksScreen({ settings }: { settings: Settings }) {
  // Two tabs: curated credible-source benchmarks, and live OpenRouter pricing.
  const [tab, setTab] = useState<"benchmarks" | "pricing">("benchmarks");

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Top tab strip */}
      <div className="flex items-center gap-1 px-3 py-2 border-b border-border shrink-0 bg-surface/40">
        <div className="flex items-center bg-surface2 rounded-xl p-0.5 text-[11px]">
          <button
            onClick={() => setTab("benchmarks")}
            className={`px-2.5 py-1 rounded-xl transition-colors ${
              tab === "benchmarks" ? "bg-accent text-white" : "text-muted-foreground hover:text-foreground"
            }`}
          >
            📊 Benchmarks
          </button>
          <button
            onClick={() => setTab("pricing")}
            className={`px-2.5 py-1 rounded-xl transition-colors ${
              tab === "pricing" ? "bg-accent text-white" : "text-muted-foreground hover:text-foreground"
            }`}
          >
            💲 Live Pricing
          </button>
        </div>
        <span className="text-[10px] text-muted-foreground/60 ml-1 hidden sm:inline truncate">
          {tab === "benchmarks"
            ? "Curated from HF Open LLM Leaderboard, Artificial Analysis, SWE-bench, LMArena"
            : "Live from OpenRouter /api/benchmarks"}
        </span>
      </div>

      <div className="flex-1 min-h-0 overflow-hidden">
        {tab === "benchmarks" ? <CredibleBenchmarks /> : <LivePricing settings={settings} />}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// CredibleBenchmarks — curated dataset with filter + sort + search.
// ─────────────────────────────────────────────────────────────────────────
function CredibleBenchmarks() {
  const [entries] = useState<BenchmarkEntry[]>(BENCHMARK_ENTRIES);
  const [catFilter, setCatFilter] = useState<CatFilter>("all");
  const [sortBy, setSortBy] = useState<string>("aa-intelligence");
  const [search, setSearch] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [refreshedAt, setRefreshedAt] = useState<string>("");
  const [refreshError, setRefreshError] = useState<string>("");

  // Distinct benchmarks (depends on the dataset, not on filter).
  const benchmarkIds = useMemo(() => allBenchmarkIds(entries), [entries]);

  // Pick the default sort key when the category changes so the user lands on
  // a relevant benchmark (e.g. picking "coding" → sort by SWE-bench).
  useEffect(() => {
    if (catFilter === "all") return;
    if (catFilter === "overall") {
      setSortBy("aa-intelligence");
      return;
    }
    const firstInCat = entries
      .flatMap((e) => e.scores)
      .find((s) => s.category === catFilter);
    if (firstInCat) setSortBy(firstInCat.id);
  }, [catFilter, entries]);

  // Filter + sort + search.
  const filtered = useMemo(() => {
    let r = entries;
    if (search.trim()) {
      const q = search.toLowerCase();
      r = r.filter(
        (e) =>
          e.name.toLowerCase().includes(q) ||
          e.id.toLowerCase().includes(q) ||
          e.family.toLowerCase().includes(q) ||
          e.providers.some((p) => p.toLowerCase().includes(q)),
      );
    }
    if (catFilter !== "all") {
      // Keep only entries that have at least one score in the category.
      r = r.filter((e) => e.scores.some((s) => s.category === catFilter));
    }
    // Sort by the selected benchmark score (descending). Entries without
    // that benchmark sink to the bottom.
    return [...r].sort((a, b) => {
      const sa = a.scores.find((s) => s.id === sortBy)?.value ?? -1;
      const sb = b.scores.find((s) => s.id === sortBy)?.value ?? -1;
      return sb - sa;
    });
  }, [entries, search, catFilter, sortBy]);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    setRefreshError("");
    try {
      // Best-effort — fetchOpenLLMLeaderboard returns null on any failure
      // (CORS / 503 / offline). We don't currently merge the live rows
      // because HF's contents dataset schema doesn't map cleanly to ours,
      // but we update the "last refreshed" timestamp so the user knows we
      // checked the upstream.
      const data = await fetchOpenLLMLeaderboard();
      if (data) {
        setRefreshedAt(new Date().toLocaleTimeString());
      } else {
        setRefreshError("Live fetch unavailable — showing curated dataset");
        setRefreshedAt(new Date().toLocaleTimeString());
      }
    } catch (e) {
      setRefreshError(e instanceof Error ? e.message : "refresh failed");
    } finally {
      setRefreshing(false);
    }
  }, []);

  // Total scores per category for the summary line.
  const summary = useMemo(() => {
    const cats: BenchmarkCategory[] = ["coding", "math", "reasoning", "agentic"];
    const counts = cats.map((c) => entries.filter((e) => e.scores.some((s) => s.category === c)).length);
    return { cats, counts, total: entries.length };
  }, [entries]);

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Header — counts + refresh */}
      <div className="flex items-center gap-2 px-3 h-9 border-b border-border text-[11px] text-muted shrink-0">
        <span className="font-medium text-text">{summary.total} models</span>
        <span className="text-[10px] text-muted-foreground/60">
          {summary.cats.map((c, i) => `${CAT_LABELS[c].replace(/^[^\s]+\s/, "")}: ${summary.counts[i]}`).join(" · ")}
        </span>
        <button
          onClick={refresh}
          disabled={refreshing}
          className="ml-auto touch-target px-2.5 h-7 rounded-xl bg-accent/10 text-accent text-[10.5px] disabled:opacity-50 flex items-center gap-1"
        >
          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className={refreshing ? "animate-spin" : ""}>
            <polyline points="23 4 23 10 17 10" />
            <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
          </svg>
          {refreshing ? "Checking…" : "Refresh"}
        </button>
      </div>

      {/* Search + category filter + sort — sticky under the header. */}
      <div className="sticky top-0 z-10 bg-surface/95 backdrop-blur flex flex-col gap-1.5 px-3 py-2 border-b border-border shrink-0">
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search models… (e.g. glm, claude, kimi)"
          className="w-full bg-surface2 border border-border rounded-xl px-2.5 py-2 text-[16px] sm:text-xs text-foreground placeholder:text-muted-foreground/50 outline-none focus:border-accent/50 transition-colors"
        />
        <div className="flex items-center gap-1.5 flex-wrap">
          {/* Category filter pills */}
          {(["all", "coding", "math", "reasoning", "agentic", "overall"] as CatFilter[]).map((c) => (
            <button
              key={c}
              onClick={() => setCatFilter(c)}
              className={`text-[10.5px] px-2 py-0.5 rounded-full border transition-colors ${
                catFilter === c
                  ? "border-accent text-accent bg-accent/10"
                  : "border-border text-muted-foreground hover:border-accent/50"
              }`}
            >
              {CAT_LABELS[c]}
            </button>
          ))}
          <div className="w-px h-3 bg-border mx-0.5 hidden sm:block" />
          {/* Sort dropdown — benchmarks scoped to the current category. */}
          <select
            value={sortBy}
            onChange={(e) => setSortBy(e.target.value)}
            className="bg-surface2 border border-border rounded-xl px-2 py-0.5 text-[10.5px] text-foreground outline-none ml-auto"
            title="Sort by benchmark score (descending)"
          >
            <option value="aa-intelligence">Sort: AA Intelligence</option>
            {benchmarkIds
              .filter((b) => catFilter === "all" || b.category === catFilter)
              .map((b) => (
                <option key={b.id} value={b.id}>
                  Sort: {b.label}
                </option>
              ))}
          </select>
        </div>
        {refreshError && (
          <div className="text-[10px] text-amber-400/80">{refreshError}</div>
        )}
        {refreshedAt && !refreshError && (
          <div className="text-[10px] text-muted-foreground/60">
            Last checked upstream sources at {refreshedAt}
          </div>
        )}
      </div>

      {/* List — table on desktop, cards on mobile. */}
      <div className="flex-1 overflow-y-auto">
        {/* Desktop table */}
        <div className="hidden sm:block">
          <table className="w-full text-[11px]">
            <thead className="sticky top-0 bg-surface z-10 text-muted-foreground/80">
              <tr className="border-b border-border">
                <th className="text-left font-medium px-3 py-2">Model</th>
                <th className="text-left font-medium px-2 py-2">Providers</th>
                {benchmarkIds
                  .filter((b) => catFilter === "all" || b.category === catFilter)
                  .map((b) => (
                    <th
                      key={b.id}
                      className={`text-right font-medium px-2 py-2 cursor-pointer hover:text-accent transition-colors ${
                        sortBy === b.id ? "text-accent" : ""
                      }`}
                      onClick={() => setSortBy(b.id)}
                      title={`Sort by ${b.label}`}
                    >
                      {b.label}
                    </th>
                  ))}
                <th className="text-left font-medium px-2 py-2">Sources</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((e) => (
                <BenchmarkRow
                  key={e.id}
                  entry={e}
                  benchmarkIds={benchmarkIds
                    .filter((b) => catFilter === "all" || b.category === catFilter)
                    .map((b) => b.id)}
                  sortId={sortBy}
                />
              ))}
              {filtered.length === 0 && (
                <tr>
                  <td colSpan={10} className="text-center text-muted-foreground py-12 text-[12px]">
                    No models match your filters.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        {/* Mobile cards */}
        <div className="sm:hidden divide-y divide-border/30">
          {filtered.map((e) => (
            <BenchmarkCard key={e.id} entry={e} sortId={sortBy} />
          ))}
          {filtered.length === 0 && (
            <div className="text-center text-muted-foreground py-12 text-[12px]">
              No models match your filters.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function scoreOf(entry: BenchmarkEntry, id: string): BenchmarkScore | undefined {
  return entry.scores.find((s) => s.id === id);
}

function fmtScore(s: BenchmarkScore | undefined): string {
  if (!s) return "—";
  if (s.unit === "Elo") return s.value.toFixed(0);
  if (s.unit === "pts") return s.value.toFixed(1);
  // percent
  return `${s.value.toFixed(1)}`;
}

function BenchmarkRow({
  entry,
  benchmarkIds,
  sortId,
}: {
  entry: BenchmarkEntry;
  benchmarkIds: string[];
  sortId: string;
}) {
  return (
    <tr className="border-b border-border/30 hover:bg-surface/30 transition-colors">
      <td className="px-3 py-2">
        <div className="flex items-center gap-1.5">
          <span
            className="inline-block w-2 h-2 rounded-full shrink-0"
            style={{ backgroundColor: familyColor(entry.family) }}
          />
          <span className="text-foreground font-medium truncate">{entry.name}</span>
        </div>
        <div className="text-[9px] text-muted-foreground/60 font-mono truncate mt-0.5">{entry.id}</div>
      </td>
      <td className="px-2 py-2">
        <div className="flex flex-wrap gap-0.5">
          {entry.providers.map((p) => (
            <span key={p} className="text-[9px] px-1 py-0.5 rounded bg-surface2 text-muted-foreground/80">
              {p}
            </span>
          ))}
        </div>
      </td>
      {benchmarkIds.map((id) => {
        const s = scoreOf(entry, id);
        const isSort = id === sortId;
        return (
          <td
            key={id}
            className={`text-right px-2 py-2 tabular-nums ${
              isSort ? "text-accent font-semibold" : s ? "text-foreground" : "text-muted-foreground/30"
            }`}
            title={s ? `${s.label}: ${fmtScore(s)}${s.unit === "%" ? "%" : ` ${s.unit}`}${s.note ? ` (${s.note})` : ""}` : "Not reported"}
          >
            {fmtScore(s)}
            {s && s.unit === "%" && s ? <span className="text-[8px] text-muted-foreground/50 ml-0.5">%</span> : null}
          </td>
        );
      })}
      <td className="px-2 py-2">
        <div className="flex flex-wrap gap-1">
          {entry.sources.map((src) => (
            <a
              key={src.url}
              href={src.url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-[9px] text-accent/80 hover:text-accent underline underline-offset-2 truncate max-w-[140px]"
              title={src.url}
            >
              {src.name}
            </a>
          ))}
        </div>
      </td>
    </tr>
  );
}

function BenchmarkCard({ entry, sortId }: { entry: BenchmarkEntry; sortId: string }) {
  // Show the "sort" benchmark first + prominently, then the rest.
  const sortScore = scoreOf(entry, sortId);
  const otherScores = entry.scores.filter((s) => s.id !== sortId);

  return (
    <div className="px-3 py-2.5">
      <div className="flex items-center gap-1.5">
        <span
          className="inline-block w-2.5 h-2.5 rounded-full shrink-0"
          style={{ backgroundColor: familyColor(entry.family) }}
        />
        <span className="text-[13px] font-medium text-foreground truncate flex-1">{entry.name}</span>
        {sortScore && (
          <span className="text-[11px] font-semibold text-accent tabular-nums shrink-0">
            {fmtScore(sortScore)}
            {sortScore.unit === "%" && <span className="text-[9px]">%</span>}
          </span>
        )}
      </div>
      <div className="text-[9px] text-muted-foreground/60 font-mono truncate mt-0.5">{entry.id}</div>
      <div className="flex flex-wrap gap-1 mt-1">
        {entry.providers.map((p) => (
          <span key={p} className="text-[9px] px-1 py-0.5 rounded bg-surface2 text-muted-foreground/80">
            {p}
          </span>
        ))}
      </div>
      <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 mt-2">
        {otherScores.map((s) => (
          <div key={s.id} className="flex items-baseline justify-between gap-2 text-[10px]">
            <span className="text-muted-foreground/70 truncate">{s.label}</span>
            <span className="text-foreground tabular-nums shrink-0">
              {fmtScore(s)}
              {s.unit === "%" && <span className="text-[8px] text-muted-foreground/50">%</span>}
            </span>
          </div>
        ))}
      </div>
      <div className="flex flex-wrap gap-1 mt-2 pt-2 border-t border-border/40">
        {entry.sources.map((src) => (
          <a
            key={src.url}
            href={src.url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-[9px] text-accent/70 hover:text-accent underline underline-offset-2 truncate max-w-[160px] inline-flex items-center gap-0.5"
            title={src.url}
          >
            <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M15 3h6v6" />
              <path d="M10 14 21 3" />
              <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
            </svg>
            {src.name}
          </a>
        ))}
        <span className="text-[9px] text-muted-foreground/40 ml-auto">updated {entry.lastUpdated}</span>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// LivePricing — the original OpenRouter-backed live data view.
// ─────────────────────────────────────────────────────────────────────────
function LivePricing({ settings }: { settings: Settings }) {
  const [data, setData] = useState<BenchmarksData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [freeOnly, setFreeOnly] = useState(false);
  const [sortBy, setSortBy] = useState<SortBy>("context");
  const [capFilter, setCapFilter] = useState<CapabilityFilter>("all");

  const client = useMemo(() => new AgentClient(settings), [settings]);

  const fetchBenchmarks = useCallback(async () => {
    if (!settings.token && !settings.rotationSecret) {
      setError("No auth token configured. Set one in Settings first.");
      return;
    }
    setLoading(true);
    setError("");
    try {
      const r = await client.getBenchmarks();
      setData(r);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load benchmarks");
    } finally {
      setLoading(false);
    }
  }, [client, settings.token, settings.rotationSecret]);

  useEffect(() => { fetchBenchmarks(); }, [fetchBenchmarks]);

  const filtered = useMemo(() => {
    if (!data) return [];
    let result = data.models;
    if (search.trim()) {
      const q = search.toLowerCase();
      result = result.filter(m =>
        m.id.toLowerCase().includes(q) ||
        m.name.toLowerCase().includes(q) ||
        m.description.toLowerCase().includes(q)
      );
    }
    if (freeOnly) result = result.filter(m => m.is_free);
    if (capFilter !== "all") result = result.filter(m => m.capabilities.includes(capFilter));
    result = [...result].sort((a, b) => {
      if (sortBy === "name") return a.name.localeCompare(b.name);
      if (sortBy === "context") return b.context_length - a.context_length;
      if (sortBy === "cost") return (a.cost_per_1m || 0) - (b.cost_per_1m || 0);
      return 0;
    });
    return result;
  }, [data, search, freeOnly, capFilter, sortBy]);

  const formatContext = (ctx: number) => {
    if (ctx >= 1000000) return `${(ctx / 1000000).toFixed(1)}M`;
    if (ctx >= 1000) return `${(ctx / 1000).toFixed(0)}K`;
    return `${ctx}`;
  };

  const formatCost1M = (cost: number) => {
    if (cost === 0) return "Free";
    if (cost < 0.01) return `$${cost.toFixed(4)}/M`;
    if (cost < 1) return `$${cost.toFixed(2)}/M`;
    return `$${cost.toFixed(2)}/M`;
  };

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Summary stats */}
      {data && (
        <div className="flex items-center gap-2 px-3 py-1.5 border-b border-border/50 text-[10px] text-muted-foreground shrink-0">
          <span className="text-emerald-400">🆓 {data.free_count} free</span>
          <span>👁️ {data.vision_count} vision</span>
          <span>💻 {data.coding_count} coding</span>
          <span>🤖 {data.agentic_count} agentic</span>
          <button
            onClick={fetchBenchmarks}
            disabled={loading}
            className="ml-auto touch-target px-2 py-0.5 rounded-xl bg-accent/10 text-accent text-[10px] disabled:opacity-50"
          >
            {loading ? "Loading…" : "Refresh"}
          </button>
        </div>
      )}

      {/* Filters */}
      <div className="flex flex-col gap-1.5 px-3 py-2 border-b border-border shrink-0">
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search models…"
          className="w-full bg-surface2 border border-border rounded-xl px-2.5 py-2 text-[16px] sm:text-xs text-foreground placeholder:text-muted-foreground/50 outline-none focus:border-accent/50 transition-colors"
        />
        <div className="flex items-center gap-1.5 flex-wrap">
          {(["all", "vision", "coding", "agentic", "reasoning", "tools"] as CapabilityFilter[]).map(c => (
            <button
              key={c}
              onClick={() => setCapFilter(c)}
              className={`text-[10px] px-2 py-0.5 rounded-full border transition-colors capitalize ${
                capFilter === c
                  ? "border-accent text-accent bg-accent/10"
                  : "border-border text-muted-foreground hover:border-accent/50"
              }`}
            >
              {c === "all" ? "All" : `${CAP_ICONS[c] || "•"} ${c}`}
            </button>
          ))}
          <div className="w-px h-3 bg-border mx-0.5" />
          <button
            onClick={() => setFreeOnly(!freeOnly)}
            className={`text-[10px] px-2 py-0.5 rounded-full border transition-colors ${
              freeOnly
                ? "border-emerald-500 text-emerald-400 bg-emerald-500/10"
                : "border-border text-muted-foreground hover:border-emerald-500/50"
            }`}
          >
            Free only
          </button>
          <select
            value={sortBy}
            onChange={(e) => setSortBy(e.target.value as SortBy)}
            className="bg-surface2 border border-border rounded-xl px-2 py-0.5 text-[10px] text-foreground outline-none ml-auto"
          >
            <option value="context">Sort: Context</option>
            <option value="cost">Sort: Cost/1M</option>
            <option value="name">Sort: Name</option>
          </select>
        </div>
      </div>

      {error && (
        <div className="px-3 py-2 text-sm text-rose-300 border-b border-rose-500/30 bg-rose-500/10">
          {error}
        </div>
      )}

      {/* Model list */}
      <div className="flex-1 overflow-y-auto">
        {loading && !data ? (
          <div className="text-center text-sm text-muted-foreground py-12">Loading models…</div>
        ) : filtered.length === 0 ? (
          <div className="text-center text-sm text-muted-foreground py-12">
            No models match your filters.
          </div>
        ) : (
          <div className="divide-y divide-border/30">
            {filtered.map((m) => (
              <div key={m.id} className="px-3 py-2.5 hover:bg-surface/30 transition-colors">
                <div className="flex items-center gap-2">
                  <span className="text-xs font-medium text-foreground truncate flex-1">{m.name || m.id}</span>
                  {m.is_free && (
                    <span className="badge-free shrink-0">FREE</span>
                  )}
                  <div className="flex items-center gap-0.5 shrink-0">
                    {m.capabilities.map(c => (
                      <span key={c} className="text-[10px]" title={c}>
                        {CAP_ICONS[c] || "•"}
                      </span>
                    ))}
                  </div>
                  <span className="text-[10px] text-muted-foreground tabular-nums shrink-0">
                    {formatContext(m.context_length)}
                  </span>
                </div>
                <div className="flex items-center gap-3 mt-1 text-[10px] text-muted-foreground">
                  <span className="font-mono text-[9px] opacity-60 truncate flex-1">{m.id}</span>
                  <span title="Cost per 1M tokens (input + output)">
                    {formatCost1M(m.cost_per_1m)}
                  </span>
                </div>
                {m.supported_params.length > 0 && (
                  <div className="flex items-center gap-1 mt-1 flex-wrap">
                    {m.supported_params.slice(0, 6).map(p => (
                      <span key={p} className="text-[8px] px-1 py-0.5 rounded bg-surface2 text-muted-foreground/70 font-mono">
                        {p}
                      </span>
                    ))}
                    {m.supported_params.length > 6 && (
                      <span className="text-[8px] text-muted-foreground/50">
                        +{m.supported_params.length - 6}
                      </span>
                    )}
                  </div>
                )}
                {m.description && (
                  <div className="text-[10px] text-muted-foreground/70 mt-1 line-clamp-2">
                    {m.description}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
