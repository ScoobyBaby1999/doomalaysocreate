// Benchmarks panel — live model comparison from OpenRouter.
// Shows: model name, context length, pricing, free/paid filter, search.

import { useEffect, useState, useMemo, useCallback } from "react";
import { AgentClient } from "../api/agent";
import type { Settings } from "../api/panel";

interface BenchmarkModel {
  id: string;
  name: string;
  context_length: number;
  prompt_price: string;
  completion_price: string;
  is_free: boolean;
  description: string;
}

export function BenchmarksScreen({ settings }: { settings: Settings }) {
  const [models, setModels] = useState<BenchmarkModel[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [freeOnly, setFreeOnly] = useState(false);
  const [sortBy, setSortBy] = useState<"name" | "context" | "price">("context");

  const client = new AgentClient(settings);

  const fetchBenchmarks = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const r = await client.getBenchmarks();
      setModels(r.models || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load benchmarks");
    } finally {
      setLoading(false);
    }
  }, [settings]);

  useEffect(() => {
    fetchBenchmarks();
  }, [fetchBenchmarks]);

  const filtered = useMemo(() => {
    let result = models;
    if (search.trim()) {
      const q = search.toLowerCase();
      result = result.filter(m =>
        m.id.toLowerCase().includes(q) ||
        m.name.toLowerCase().includes(q) ||
        m.description.toLowerCase().includes(q)
      );
    }
    if (freeOnly) {
      result = result.filter(m => m.is_free);
    }
    // Sort
    result = [...result].sort((a, b) => {
      if (sortBy === "name") return a.name.localeCompare(b.name);
      if (sortBy === "context") return b.context_length - a.context_length;
      if (sortBy === "price") {
        const pa = parseFloat(a.prompt_price) || 0;
        const pb = parseFloat(b.prompt_price) || 0;
        return pa - pb;
      }
      return 0;
    });
    return result;
  }, [models, search, freeOnly, sortBy]);

  const formatPrice = (price: string) => {
    const p = parseFloat(price);
    if (p === 0) return "Free";
    if (p < 0.000001) return `$${(p * 1e6).toFixed(2)}/M`;
    if (p < 0.001) return `$${(p * 1e3).toFixed(4)}/K`;
    return `$${p.toFixed(6)}/tok`;
  };

  const formatContext = (ctx: number) => {
    if (ctx >= 1000000) return `${(ctx / 1000000).toFixed(1)}M`;
    if (ctx >= 1000) return `${(ctx / 1000).toFixed(0)}K`;
    return `${ctx}`;
  };

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-2 px-3 h-9 border-b border-border text-[11px] text-muted shrink-0">
        <span className="font-medium text-text">Benchmarks</span>
        <span className="text-[10px] text-muted-foreground/60">
          {filtered.length} of {models.length} models
        </span>
        <button
          onClick={fetchBenchmarks}
          disabled={loading}
          className="ml-auto px-2 py-0.5 rounded bg-accent/10 text-accent text-[10px] disabled:opacity-50"
        >
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>

      {/* Filters */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border shrink-0">
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search models…"
          className="flex-1 bg-surface2 border border-border rounded-md px-2.5 py-1 text-xs text-foreground placeholder:text-muted-foreground/50 outline-none focus:border-accent/50"
        />
        <button
          onClick={() => setFreeOnly(!freeOnly)}
          className={`text-[10px] px-2 py-1 rounded-md border transition-colors shrink-0 ${
            freeOnly
              ? "border-accent text-accent bg-accent/10"
              : "border-border text-muted-foreground hover:border-accent/50"
          }`}
        >
          Free only
        </button>
        <select
          value={sortBy}
          onChange={(e) => setSortBy(e.target.value as "name" | "context" | "price")}
          className="bg-surface2 border border-border rounded-md px-2 py-1 text-[10px] text-foreground outline-none"
        >
          <option value="context">Sort: Context</option>
          <option value="price">Sort: Price</option>
          <option value="name">Sort: Name</option>
        </select>
      </div>

      {error && (
        <div className="px-3 py-2 text-sm text-rose-300 border-b border-rose-500/30 bg-rose-500/10">
          {error}
        </div>
      )}

      {/* Model list */}
      <div className="flex-1 overflow-y-auto">
        {loading && models.length === 0 ? (
          <div className="text-center text-sm text-muted-foreground py-12">Loading benchmarks…</div>
        ) : filtered.length === 0 ? (
          <div className="text-center text-sm text-muted-foreground py-12">
            No models match your filters.
          </div>
        ) : (
          <div className="divide-y divide-border/30">
            {filtered.map((m) => (
              <div key={m.id} className="px-3 py-2 hover:bg-surface/30 transition-colors">
                <div className="flex items-center gap-2">
                  <span className="text-xs font-medium text-foreground truncate flex-1">{m.name || m.id}</span>
                  {m.is_free && (
                    <span className="text-[9px] px-1.5 py-0.5 rounded-full bg-emerald-500/15 text-emerald-400 font-medium shrink-0">
                      FREE
                    </span>
                  )}
                  <span className="text-[10px] text-muted-foreground tabular-nums shrink-0">
                    {formatContext(m.context_length)} ctx
                  </span>
                </div>
                <div className="flex items-center gap-3 mt-0.5 text-[10px] text-muted-foreground">
                  <span className="font-mono text-[9px] opacity-60">{m.id}</span>
                  <span>In: {formatPrice(m.prompt_price)}</span>
                  <span>Out: {formatPrice(m.completion_price)}</span>
                </div>
                {m.description && (
                  <div className="text-[10px] text-muted-foreground/70 mt-0.5 line-clamp-1">
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
