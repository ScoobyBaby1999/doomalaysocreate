// Benchmarks panel — meaningful model comparison from OpenRouter.
// Shows: capabilities, pricing, context, modality with filters and sorting.

import { useEffect, useState, useMemo, useCallback } from "react";
import { AgentClient } from "../api/agent";
import type { Settings } from "../api/panel";

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

export function BenchmarksScreen({ settings }: { settings: Settings }) {
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
      {/* Header */}
      <div className="flex items-center gap-2 px-3 h-9 border-b border-border text-[11px] text-muted shrink-0">
        <span className="font-medium text-text">Models</span>
        {data && (
          <span className="text-[10px] text-muted-foreground/60">
            {filtered.length} of {data.count}
          </span>
        )}
        <button
          onClick={fetchBenchmarks}
          disabled={loading}
          className="ml-auto px-2 py-0.5 rounded bg-accent/10 text-accent text-[10px] disabled:opacity-50"
        >
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>

      {/* Summary stats */}
      {data && (
        <div className="flex items-center gap-2 px-3 py-1.5 border-b border-border/50 text-[10px] text-muted-foreground shrink-0">
          <span className="text-emerald-400">🆓 {data.free_count} free</span>
          <span>👁️ {data.vision_count} vision</span>
          <span>💻 {data.coding_count} coding</span>
          <span>🤖 {data.agentic_count} agentic</span>
        </div>
      )}

      {/* Filters */}
      <div className="flex flex-col gap-1.5 px-3 py-2 border-b border-border shrink-0">
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search models…"
          className="w-full bg-surface2 border border-border rounded-xl px-2.5 py-1 text-xs text-foreground placeholder:text-muted-foreground/50 outline-none focus:border-accent/50 transition-colors"
        />
        <div className="flex items-center gap-1.5 flex-wrap">
          {/* Capability filters */}
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
                  {/* Capability badges */}
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
                {/* Supported params */}
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
