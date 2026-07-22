/**
 * TemplateLibrary — full-screen overlay for browsing, creating, editing,
 * hearting, and downloading prompt templates.
 *
 * Two tabs:
 *  - "My Templates": the user's local templates (CRUD + publish/unpublish).
 *  - "Explore": public templates from every author (search, sort, filter,
 *    heart, download).
 *
 * Layout: two columns on desktop (list ~60%, preview ~40%); single column
 * on mobile (list, with the selected template's preview sliding up as a sheet).
 *
 * Opened from the ToolIcons popovers (web search / deep research / judge) via
 * the chatStore's `openTemplateLibrary(kind)` action. When the user picks a
 * template (via the "Use" button), `applyTemplate` routes it to the right
 * tool slot based on its kind.
 */
import { useEffect, useMemo, useState, useCallback } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { TemplateClient, type Template, type TemplateKind } from "../api/templates";
import type { Settings } from "../api/panel";
import { Markdown } from "./Markdown";
// BATCH-2 Task 6.4 — chatStore is used by the AI-generate flow to drop
// the user's template request into the chat input (so the agent can
// create the template).
import { useChatStore } from "../state/chatStore";

type Tab = "mine" | "explore";
type Sort = "hearts" | "recent" | "relevant";

interface TemplateLibraryProps {
  open: boolean;
  /** Kind filter to pre-apply (e.g. "websearch" when opened from the web
   *  search popover). Empty string = no filter. */
  initialKind: string;
  initialTab: Tab;
  settings: Settings;
  onClose: () => void;
  onApply: (template: Template) => void;
}

const KIND_LABELS: Record<string, string> = {
  websearch: "Web Search",
  deepresearch: "Deep Research",
  judge: "Judge",
  chat: "Chat",
  custom: "Custom",
};

const KIND_COLORS: Record<string, string> = {
  websearch: "#a855f7",
  deepresearch: "#a855f7",
  judge: "#f59e0b",
  chat: "#22c55e",
  custom: "#8b95a3",
};

const ALL_KINDS: TemplateKind[] = ["websearch", "deepresearch", "judge", "chat", "custom"];

export function TemplateLibrary({
  open,
  initialKind,
  initialTab,
  settings,
  onClose,
  onApply,
}: TemplateLibraryProps) {
  const client = useMemo(() => new TemplateClient(settings), [settings]);

  const [tab, setTab] = useState<Tab>(initialTab);
  // kind filter — empty string = all kinds
  const [kindFilter, setKindFilter] = useState<string>(initialKind);

  // My Templates state
  const [mine, setMine] = useState<Template[]>([]);
  const [loadingMine, setLoadingMine] = useState(false);
  const [mineError, setMineError] = useState<string | null>(null);

  // Explore state
  const [explore, setExplore] = useState<Template[]>([]);
  const [loadingExplore, setLoadingExplore] = useState(false);
  const [exploreError, setExploreError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<Sort>("hearts");

  // Selected template (right panel preview)
  const [selected, setSelected] = useState<Template | null>(null);

  /** On mobile (<=480px), selecting a template takes over the entire panel
   *  with a "back" button. We track this separately from `selected` so the
   *  desktop right-column preview can stay in sync without forcing the
   *  mobile takeover on desktop. */
  const [mobilePreview, setMobilePreview] = useState(false);

  // Editor state — when set, the editor form replaces the list+preview
  // (full-modal on desktop, bottom sheet on mobile). Null = no editor open.
  const [editing, setEditing] = useState<Template | null>(null);
  const [creating, setCreating] = useState(false);

  // BATCH-2 Task 6.4 — AI template generation dialog. When open, asks the
  // user what kind of template they want, then sends a message to the agent
  // to create it. The agent's reply (a markdown template) is captured and
  // saved as a new template via the editor flow.
  const [aiDialogOpen, setAiDialogOpen] = useState(false);

  // Sync tab + kind filter when the overlay opens with new initial values.
  useEffect(() => {
    if (open) {
      setTab(initialTab);
      setKindFilter(initialKind);
      setSelected(null);
      setMobilePreview(false);
      setEditing(null);
      setCreating(false);
    }
  }, [open, initialKind, initialTab]);

  // -- Loaders --------------------------------------------------------------
  const loadMine = useCallback(async () => {
    setLoadingMine(true);
    setMineError(null);
    try {
      const r = await client.listMine();
      setMine(r.templates || []);
    } catch (e) {
      setMineError(e instanceof Error ? e.message : "Failed to load templates");
    } finally {
      setLoadingMine(false);
    }
  }, [client]);

  const loadExplore = useCallback(async () => {
    setLoadingExplore(true);
    setExploreError(null);
    try {
      const r = await client.explore({
        sort,
        query: query || undefined,
        kind: kindFilter || undefined,
        limit: 100,
        offset: 0,
      });
      setExplore(r.templates || []);
    } catch (e) {
      setExploreError(e instanceof Error ? e.message : "Failed to load explore");
    } finally {
      setLoadingExplore(false);
    }
  }, [client, sort, query, kindFilter]);

  useEffect(() => {
    if (!open) return;
    if (tab === "mine") loadMine();
    else loadExplore();
  }, [open, tab, loadMine, loadExplore]);

  // Close on Escape
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        if (editing || creating) {
          setEditing(null);
          setCreating(false);
          return;
        }
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose, editing, creating]);

  // -- Actions --------------------------------------------------------------
  const handleHeart = useCallback(
    async (tpl: Template) => {
      // Optimistic: toggle the heart + bump the count locally.
      const newHearted = !tpl.hearted;
      const newHearts = newHearted ? tpl.hearts + 1 : Math.max(0, tpl.hearts - 1);
      const patch = (arr: Template[]) =>
        arr.map((t) =>
          t.id === tpl.id ? { ...t, hearted: newHearted, hearts: newHearts } : t,
        );
      setMine(patch);
      setExplore(patch);
      setSelected((s) =>
        s && s.id === tpl.id ? { ...s, hearted: newHearted, hearts: newHearts } : s,
      );
      try {
        const r = await client.heart(tpl.id);
        const fix = (arr: Template[]) =>
          arr.map((t) =>
            t.id === tpl.id ? { ...t, hearted: r.hearted, hearts: r.hearts } : t,
          );
        setMine(fix);
        setExplore(fix);
        setSelected((s) =>
          s && s.id === tpl.id ? { ...s, hearted: r.hearted, hearts: r.hearts } : s,
        );
      } catch {
        // Revert on failure.
        const revert = (arr: Template[]) =>
          arr.map((t) =>
            t.id === tpl.id ? { ...t, hearted: tpl.hearted, hearts: tpl.hearts } : t,
          );
        setMine(revert);
        setExplore(revert);
        setSelected((s) =>
          s && s.id === tpl.id ? { ...s, hearted: tpl.hearted, hearts: tpl.hearts } : s,
        );
      }
    },
    [client],
  );

  const handleDownload = useCallback(
    async (tpl: Template) => {
      try {
        const r = await client.download(tpl.id);
        // After download, the template becomes "mine" — refresh the mine list.
        loadMine();
        // Patch the explore list's downloaded flag.
        setExplore((arr) =>
          arr.map((t) =>
            t.id === tpl.id ? { ...t, downloaded: true, downloads: t.downloads + 1 } : t,
          ),
        );
        setSelected((s) =>
          s && s.id === tpl.id
            ? { ...s, downloaded: true, downloads: s.downloads + 1 }
            : s,
        );
        // Optionally switch to "My Templates" so the user sees their new copy.
        setTab("mine");
        // Select the newly-downloaded local copy so the user can preview it.
        setSelected(r.template);
      } catch {
        /* non-fatal */
      }
    },
    [client, loadMine],
  );

  const handlePublish = useCallback(
    async (tpl: Template, makePublic: boolean) => {
      try {
        const r = makePublic
          ? await client.publish(tpl.id)
          : await client.unpublish(tpl.id);
        const updated = r.template;
        const patch = (arr: Template[]) =>
          arr.map((t) => (t.id === tpl.id ? updated : t));
        setMine(patch);
        setExplore(patch);
        setSelected((s) => (s && s.id === tpl.id ? updated : s));
      } catch {
        /* non-fatal */
      }
    },
    [client],
  );

  const handleDelete = useCallback(
    async (tpl: Template) => {
      if (!confirm(`Delete "${tpl.name}"? This cannot be undone.`)) return;
      try {
        await client.delete(tpl.id);
        setMine((arr) => arr.filter((t) => t.id !== tpl.id));
        setExplore((arr) => arr.filter((t) => t.id !== tpl.id));
        if (selected?.id === tpl.id) setSelected(null);
      } catch {
        /* non-fatal */
      }
    },
    [client, selected],
  );

  const handleSaved = useCallback(
    (tpl: Template) => {
      // Refresh both lists so the new/edited template shows up.
      loadMine();
      if (tab === "explore") loadExplore();
      setEditing(null);
      setCreating(false);
      setSelected(tpl);
    },
    [loadMine, loadExplore, tab],
  );

  // -- Filtered mine list (by kind) ----------------------------------------
  const mineFiltered = useMemo(() => {
    if (!kindFilter) return mine;
    return mine.filter((t) => t.kind === kindFilter);
  }, [mine, kindFilter]);

  const exploreFiltered = explore; // backend already filters by kind

  // -- Render ---------------------------------------------------------------
  return (
    <AnimatePresence>
      {open && (
        <>
          {/* Backdrop */}
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="fixed inset-0 z-[60] bg-black/50 backdrop-blur-sm"
            onClick={onClose}
            aria-hidden="true"
          />

          {/* Modal */}
          {/* Modal — full-screen takeover on mobile (no padding), centered
              rounded card on sm:+. The inner container fills the viewport on
              mobile so the list/preview/editor each take the full height. */}
          <motion.div
            initial={{ opacity: 0, scale: 0.97, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.97, y: 8 }}
            transition={{ duration: 0.2, ease: [0.19, 1, 0.22, 1] }}
            className="fixed inset-0 z-[60] flex items-stretch sm:items-center justify-stretch sm:justify-center p-0 sm:p-6 pointer-events-none"
          >
            <div
              className="pointer-events-auto w-full flex flex-col rounded-none sm:rounded-xl border-0 sm:border border-border bg-surface shadow-2xl shadow-black/30 overflow-hidden sm:max-w-5xl"
              style={{ height: "100dvh", maxHeight: "100dvh" }}
              onClick={(e) => e.stopPropagation()}
              role="dialog"
              aria-modal="true"
              aria-label="Template Library"
            >
              {/* Header — tabs + close */}
              <div className="flex items-center justify-between gap-3 px-3.5 h-11 border-b border-border shrink-0">
                <div className="flex items-center gap-1.5 min-w-0">
                  <svg
                    width="14"
                    height="14"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    className="text-accent shrink-0"
                  >
                    <path d="M4 4v16a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V8.343a2 2 0 0 0-.586-1.414l-4.343-4.343A2 2 0 0 0 15.657 2H6a2 2 0 0 0-2 2z" />
                    <path d="M14 2v6h6" />
                    <path d="M9 14h6" />
                    <path d="M9 18h6" />
                  </svg>
                  <span className="text-[13px] font-semibold text-foreground shrink-0">
                    Template Library
                  </span>
                  {/* Tabs */}
                  <div className="flex items-center bg-surface2 rounded-xl p-0.5 ml-2">
                    <button
                      onClick={() => setTab("mine")}
                      className={`px-2 py-0.5 rounded text-[11px] transition-colors ${
                        tab === "mine"
                          ? "bg-accent text-white"
                          : "text-muted-foreground hover:text-foreground"
                      }`}
                    >
                      My Templates
                    </button>
                    <button
                      onClick={() => setTab("explore")}
                      className={`px-2 py-0.5 rounded text-[11px] transition-colors ${
                        tab === "explore"
                          ? "bg-accent text-white"
                          : "text-muted-foreground hover:text-foreground"
                      }`}
                    >
                      Explore
                    </button>
                  </div>
                  {kindFilter && (
                    <span
                      className="text-[9px] px-1.5 py-0.5 rounded-full font-medium shrink-0"
                      style={{
                        backgroundColor: `${KIND_COLORS[kindFilter] || "#8b95a3"}20`,
                        color: KIND_COLORS[kindFilter] || "#8b95a3",
                      }}
                    >
                      {KIND_LABELS[kindFilter] || kindFilter}
                    </span>
                  )}
                </div>
                <button
                  onClick={onClose}
                  className="text-muted-foreground hover:text-foreground p-1.5 rounded-xl hover:bg-surface2 transition-colors shrink-0"
                  title="Close"
                  aria-label="Close"
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <line x1="18" y1="6" x2="6" y2="18" />
                    <line x1="6" y1="6" x2="18" y2="18" />
                  </svg>
                </button>
              </div>

              {/* Body — two columns on desktop, one column on mobile */}
              {creating || editing ? (
                <TemplateEditor
                  client={client}
                  existing={editing}
                  defaultKind={(kindFilter as TemplateKind) || "websearch"}
                  onCancel={() => {
                    setCreating(false);
                    setEditing(null);
                  }}
                  onSaved={handleSaved}
                />
              ) : mobilePreview && selected ? (
                /* Mobile full-screen preview take-over. Replaces the list with
                   a single-column preview + a back button. Hidden on sm:+. */
                <div className="flex-1 flex flex-col min-h-0 sm:hidden">
                  <div className="flex items-center gap-2 px-3 h-11 border-b border-border shrink-0">
                    <button
                      onClick={() => setMobilePreview(false)}
                      className="touch-target flex items-center gap-1 -ml-1 px-2 h-8 rounded-xl text-accent hover:bg-surface2 transition-colors text-[12px]"
                      aria-label="Back to template list"
                    >
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                        <polyline points="15 18 9 12 15 6" />
                      </svg>
                      Back
                    </button>
                    <span className="text-[12px] font-medium text-foreground truncate flex-1">
                      {selected.name}
                    </span>
                    <button
                      onClick={onClose}
                      className="touch-target p-1.5 rounded-xl text-muted-foreground hover:text-foreground hover:bg-surface2 transition-colors shrink-0"
                      aria-label="Close"
                    >
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <line x1="18" y1="6" x2="6" y2="18" />
                        <line x1="6" y1="6" x2="18" y2="18" />
                      </svg>
                    </button>
                  </div>
                  <div className="flex-1 min-h-0 overflow-hidden">
                    <TemplatePreview
                      template={selected}
                      onApply={onApply}
                      onHeart={handleHeart}
                      onDownload={handleDownload}
                      onEdit={tab === "mine" ? (t) => setEditing(t) : undefined}
                      onDelete={tab === "mine" ? handleDelete : undefined}
                      onPublish={tab === "mine" ? handlePublish : undefined}
                      compact
                    />
                  </div>
                </div>
              ) : (
                <div className="flex-1 flex flex-col md:flex-row min-h-0">
                  {/* Left column — list + filters */}
                  <div className={`flex-1 flex flex-col min-h-0 border-b md:border-b-0 md:border-r border-border ${mobilePreview ? "hidden sm:flex" : ""}`}>
                    {/* Filters — sticky at the top on mobile so search
                        stays visible while scrolling the list. */}
                    <div className="sticky top-0 z-10 bg-surface/95 backdrop-blur flex items-center gap-2 px-3 py-2 border-b border-border shrink-0">
                      {tab === "explore" && (
                        <>
                          <div className="relative flex-1 min-w-0">
                            <svg
                              width="12"
                              height="12"
                              viewBox="0 0 24 24"
                              fill="none"
                              stroke="currentColor"
                              strokeWidth="2"
                              strokeLinecap="round"
                              strokeLinejoin="round"
                              className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground/60 pointer-events-none"
                            >
                              <circle cx="11" cy="11" r="8" />
                              <path d="m21 21-4.3-4.3" />
                            </svg>
                            <input
                              type="text"
                              value={query}
                              onChange={(e) => setQuery(e.target.value)}
                              onKeyDown={(e) => {
                                if (e.key === "Enter") loadExplore();
                              }}
                              placeholder="Search templates…"
                              className="w-full pl-7 pr-2 py-1.5 rounded-xl bg-surface2 border border-border text-[12px] text-foreground placeholder:text-muted-foreground/50 outline-none focus:border-accent"
                            />
                          </div>
                          <select
                            value={sort}
                            onChange={(e) => setSort(e.target.value as Sort)}
                            className="text-[11px] bg-surface2 border border-border rounded-xl px-1.5 py-1.5 text-muted-foreground outline-none focus:border-accent shrink-0"
                            title="Sort by"
                          >
                            <option value="hearts">Most Hearted</option>
                            <option value="recent">Most Recent</option>
                            <option value="relevant">Most Relevant</option>
                          </select>
                        </>
                      )}
                      <select
                        value={kindFilter}
                        onChange={(e) => setKindFilter(e.target.value)}
                        className="text-[11px] bg-surface2 border border-border rounded-xl px-1.5 py-1.5 text-muted-foreground outline-none focus:border-accent shrink-0"
                        title="Filter by kind"
                      >
                        <option value="">All kinds</option>
                        {ALL_KINDS.map((k) => (
                          <option key={k} value={k}>
                            {KIND_LABELS[k]}
                          </option>
                        ))}
                      </select>
                      {tab === "mine" && (
                        <>
                          {/* BATCH-2 Task 6.4 — "Generate with AI" button.
                              Opens a dialog asking what kind of template
                              the user wants, then sends a message to the
                              agent to create it. */}
                          <button
                            onClick={() => setAiDialogOpen(true)}
                            className="text-[11px] px-2 py-1.5 rounded-xl border border-accent/40 text-accent hover:bg-accent/10 transition-colors shrink-0 flex items-center gap-1"
                            title="Generate a template with AI"
                          >
                            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                              <path d="M12 2L3 14h9l-1 8 10-12h-9l1-8z" />
                            </svg>
                            AI
                          </button>
                          <button
                            onClick={() => setCreating(true)}
                            className="text-[11px] px-2 py-1.5 rounded-xl bg-accent text-white hover:bg-accent/90 transition-colors shrink-0 flex items-center gap-1"
                            title="Create a new template"
                          >
                            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                              <line x1="12" y1="5" x2="12" y2="19" />
                              <line x1="5" y1="12" x2="19" y2="12" />
                            </svg>
                            New
                          </button>
                        </>
                      )}
                    </div>

                    {/* List */}
                    <div className="flex-1 overflow-y-auto">
                      {tab === "mine" ? (
                        loadingMine ? (
                          <div className="p-4 text-[12px] text-muted-foreground">
                            Loading your templates…
                          </div>
                        ) : mineError ? (
                          <div className="p-4 text-[12px] text-red-400">{mineError}</div>
                        ) : mineFiltered.length === 0 ? (
                          <EmptyState
                            title="No templates yet"
                            body="Browse what the community has shared in the Explore tab, or generate one with AI."
                            cta={
                              <div className="flex items-center gap-2">
                                {/* BATCH-2 Task 6.3 — empty state CTA is now
                                    "Explore community templates" (not "Create
                                    new template"). Users rarely manually
                                    create templates — they download from the
                                    community or generate with AI. */}
                                <button
                                  onClick={() => setTab("explore")}
                                  className="text-[12px] px-3 py-1.5 rounded-xl bg-accent text-white hover:bg-accent/90 transition-colors"
                                >
                                  Explore community templates
                                </button>
                                <button
                                  onClick={() => setAiDialogOpen(true)}
                                  className="text-[12px] px-3 py-1.5 rounded-xl border border-border text-foreground hover:bg-surface2 transition-colors flex items-center gap-1.5"
                                  title="Generate a template with AI"
                                >
                                  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                    <path d="M12 2L3 14h9l-1 8 10-12h-9l1-8z" />
                                  </svg>
                                  Generate with AI
                                </button>
                              </div>
                            }
                          />
                        ) : (
                          <TemplateList
                            templates={mineFiltered}
                            selectedId={selected?.id}
                            onSelect={(t) => {
                              setSelected(t);
                              setMobilePreview(true);
                            }}
                            onEdit={(t) => setEditing(t)}
                            onDelete={handleDelete}
                            onPublish={handlePublish}
                            onApply={onApply}
                            showAuthor={false}
                          />
                        )
                      ) : loadingExplore ? (
                        <div className="p-4 text-[12px] text-muted-foreground">
                          Loading public templates…
                        </div>
                      ) : exploreError ? (
                        <div className="p-4 text-[12px] text-red-400">{exploreError}</div>
                      ) : exploreFiltered.length === 0 ? (
                        <EmptyState
                          title="No templates found"
                          body={query ? `No templates match "${query}".` : "No public templates have been published yet."}
                          cta={null}
                        />
                      ) : (
                        <TemplateList
                          templates={exploreFiltered}
                          selectedId={selected?.id}
                          onSelect={(t) => {
                            setSelected(t);
                            setMobilePreview(true);
                          }}
                          onHeart={handleHeart}
                          onDownload={handleDownload}
                          onApply={onApply}
                          showAuthor
                        />
                      )}
                    </div>
                  </div>

                  {/* Right column — preview panel (desktop) */}
                  <div className="hidden md:flex md:w-[40%] md:flex-col min-h-0">
                    {selected ? (
                      <TemplatePreview
                        template={selected}
                        onApply={onApply}
                        onHeart={handleHeart}
                        onDownload={handleDownload}
                        onEdit={tab === "mine" ? (t) => setEditing(t) : undefined}
                        onDelete={tab === "mine" ? handleDelete : undefined}
                        onPublish={tab === "mine" ? handlePublish : undefined}
                      />
                    ) : (
                      <div className="flex-1 flex items-center justify-center p-6 text-center text-muted-foreground/60 text-[12px]">
                        <div>
                          <svg
                            width="28"
                            height="28"
                            viewBox="0 0 24 24"
                            fill="none"
                            stroke="currentColor"
                            strokeWidth="1.5"
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            className="mx-auto mb-2 text-muted-foreground/40 empty-state-icon"
                          >
                            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                            <path d="M14 2v6h6" />
                          </svg>
                          Select a template to preview its markdown.
                        </div>
                      </div>
                    )}
                  </div>

                  {/* (Mobile preview is handled by the `mobilePreview` take-over above.) */}
                </div>
              )}

              {/* BATCH-2 Task 6.4 — AI template generation dialog. Modal on
                  top of the library; asks what kind of template the user
                  wants, then drops the request into the chat input so the
                  agent can create it. */}
              <AiGenerateDialog
                open={aiDialogOpen}
                onClose={() => setAiDialogOpen(false)}
                onSubmit={(kind, description) => {
                  // Compose the prompt the agent will see. The agent's
                  // backend has a template-creation tool it can invoke.
                  const prompt = [
                    `Create a ${KIND_LABELS[kind] || kind} template for me.`,
                    description.trim() ? `What it should do: ${description.trim()}` : "",
                    "",
                    "Use the roles.py system (planner, generator, critiquer, etc.).",
                    "Return the full markdown prompt — I'll save it to my template library.",
                  ].filter(Boolean).join("\n");
                  useChatStore.getState().setInputText(prompt);
                  useChatStore.getState().closeTemplateLibrary();
                  setAiDialogOpen(false);
                }}
              />
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}

// ---------------------------------------------------------------------------
// TemplateList — renders cards for a list of templates
// ---------------------------------------------------------------------------

interface TemplateListProps {
  templates: Template[];
  selectedId?: string;
  onSelect: (t: Template) => void;
  onApply?: (t: Template) => void;
  onHeart?: (t: Template) => void;
  onDownload?: (t: Template) => void;
  onEdit?: (t: Template) => void;
  onDelete?: (t: Template) => void;
  onPublish?: (t: Template, makePublic: boolean) => void;
  showAuthor: boolean;
}

function TemplateList({
  templates,
  selectedId,
  onSelect,
  onApply,
  onHeart,
  onDownload,
  onEdit,
  onDelete,
  onPublish,
  showAuthor,
}: TemplateListProps) {
  return (
    <div className="p-2 space-y-1.5">
      {templates.map((t) => (
        <TemplateCard
          key={t.id}
          template={t}
          selected={t.id === selectedId}
          onSelect={() => onSelect(t)}
          onApply={onApply}
          onHeart={onHeart}
          onDownload={onDownload}
          onEdit={onEdit}
          onDelete={onDelete}
          onPublish={onPublish}
          showAuthor={showAuthor}
        />
      ))}
    </div>
  );
}

interface TemplateCardProps extends Omit<TemplateListProps, "templates" | "selectedId" | "onSelect"> {
  template: Template;
  selected: boolean;
  onSelect: () => void;
}

function TemplateCard({
  template: t,
  selected,
  onSelect,
  onApply,
  onHeart,
  onDownload,
  onEdit,
  onDelete,
  onPublish,
  showAuthor,
}: TemplateCardProps) {
  return (
    <div
      onClick={onSelect}
      className={`rounded-xl border p-2.5 cursor-pointer transition-colors card-hover ${
        selected
          ? "border-accent bg-accent/5"
          : "border-border hover:border-accent/40 hover:bg-surface2/50"
      }`}
    >
      <div className="flex items-start gap-2">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span
              className="text-[9px] px-1.5 py-0.5 rounded-full font-medium shrink-0"
              style={{
                backgroundColor: `${KIND_COLORS[t.kind] || "#8b95a3"}20`,
                color: KIND_COLORS[t.kind] || "#8b95a3",
              }}
            >
              {KIND_LABELS[t.kind] || t.kind}
            </span>
            {t.is_public && (
              <span className="text-[9px] px-1.5 py-0.5 rounded-full bg-emerald-500/15 text-emerald-400 font-medium shrink-0">
                Public
              </span>
            )}
            <span className="text-[12.5px] font-medium text-foreground truncate flex-1 min-w-0">
              {t.name}
            </span>
          </div>
          {t.description && (
            <div className="text-[11px] text-muted-foreground mt-1 truncate-2 leading-snug">
              {t.description}
            </div>
          )}
          <div className="flex items-center gap-3 mt-1.5 text-[9.5px] text-muted-foreground/70">
            {showAuthor && t.author_name && (
              <span className="flex items-center gap-1">
                <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
                  <circle cx="12" cy="7" r="4" />
                </svg>
                {t.author_name}
              </span>
            )}
            <span className="flex items-center gap-1">
              <svg width="9" height="9" viewBox="0 0 24 24" fill={t.hearted ? "currentColor" : "none"} stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className={t.hearted ? "text-red-400" : ""}>
                <path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.29 1.51 4.04 3 5.5l7 7Z" />
              </svg>
              {t.hearts}
            </span>
            <span className="flex items-center gap-1">
              <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                <polyline points="7 10 12 15 17 10" />
                <line x1="12" y1="15" x2="12" y2="3" />
              </svg>
              {t.downloads}
            </span>
            {t.tags && t.tags.length > 0 && (
              <span className="text-muted-foreground/50 truncate">
                {t.tags.slice(0, 3).map((tag) => `#${tag}`).join(" ")}
              </span>
            )}
          </div>
        </div>

        {/* Quick actions — appear at the right of the card */}
        <div className="flex items-center gap-0.5 shrink-0" onClick={(e) => e.stopPropagation()}>
          {onHeart && (
            <button
              onClick={() => onHeart(t)}
              className={`p-1 rounded hover:bg-surface2 transition-colors ${t.hearted ? "text-red-400" : "text-muted-foreground hover:text-red-400"}`}
              title={t.hearted ? "Unheart" : "Heart"}
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill={t.hearted ? "currentColor" : "none"} stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.29 1.51 4.04 3 5.5l7 7Z" />
              </svg>
            </button>
          )}
          {onDownload && !t.downloaded && (
            <button
              onClick={() => onDownload(t)}
              className="p-1 rounded text-muted-foreground hover:text-accent hover:bg-surface2 transition-colors"
              title="Download (create a local copy)"
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                <polyline points="7 10 12 15 17 10" />
                <line x1="12" y1="15" x2="12" y2="3" />
              </svg>
            </button>
          )}
          {onDownload && t.downloaded && (
            <span
              className="p-1 text-emerald-400/70"
              title="Already downloaded — find it in My Templates"
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="20 6 9 17 4 12" />
              </svg>
            </span>
          )}
        </div>
      </div>

      {/* Per-card actions row (Edit/Delete/Publish/Use) */}
      <div className="flex items-center gap-1 mt-2 pt-2 border-t border-border/50">
        {onEdit && (
          <button
            onClick={(e) => { e.stopPropagation(); onEdit(t); }}
            className="text-[10px] px-1.5 py-0.5 rounded text-muted-foreground hover:text-foreground hover:bg-surface2 transition-colors"
          >
            Edit
          </button>
        )}
        {onPublish && (
          <button
            onClick={(e) => { e.stopPropagation(); onPublish(t, !t.is_public); }}
            className="text-[10px] px-1.5 py-0.5 rounded text-muted-foreground hover:text-foreground hover:bg-surface2 transition-colors"
          >
            {t.is_public ? "Unpublish" : "Publish"}
          </button>
        )}
        {onDelete && (
          <button
            onClick={(e) => { e.stopPropagation(); onDelete(t); }}
            className="text-[10px] px-1.5 py-0.5 rounded text-muted-foreground hover:text-red-400 hover:bg-surface2 transition-colors"
          >
            Delete
          </button>
        )}
        <div className="flex-1" />
        {onApply && (
          <button
            onClick={(e) => { e.stopPropagation(); onApply(t); }}
            className="text-[10px] px-2 py-0.5 rounded-xl bg-accent text-white hover:bg-accent/90 transition-colors font-medium"
            title="Use this template in the current chat tool"
          >
            Use
          </button>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// TemplatePreview — right-panel markdown preview + actions
// ---------------------------------------------------------------------------

interface TemplatePreviewProps {
  template: Template;
  onApply: (t: Template) => void;
  onHeart?: (t: Template) => void;
  onDownload?: (t: Template) => void;
  onEdit?: (t: Template) => void;
  onDelete?: (t: Template) => void;
  onPublish?: (t: Template, makePublic: boolean) => void;
  compact?: boolean;
}

function TemplatePreview({
  template: t,
  onApply,
  onHeart,
  onDownload,
  onEdit,
  onDelete,
  onPublish,
  compact,
}: TemplatePreviewProps) {
  // "Human Readable" renders the rendered markdown; "See Raw" shows the
  // raw markdown source in a scrollable <pre> code block. Power users want
  // this — the raw source is what actually gets sent to the model (system
  // prompt, role instructions, fanout config, shard definitions, etc.).
  const [viewMode, setViewMode] = useState<"human" | "raw">("human");
  // Reset to human-readable when the template changes.
  useEffect(() => { setViewMode("human"); }, [t.id]);

  // Copy raw markdown to clipboard (used by the "See Raw" view).
  const [copied, setCopied] = useState(false);
  const handleCopyRaw = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(t.markdown || "");
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked — non-fatal */
    }
  }, [t.markdown]);

  return (
    <div className="flex-1 flex flex-col min-h-0">
      {/* Header */}
      <div className="px-3 py-2 border-b border-border shrink-0">
        <div className="flex items-center gap-1.5 flex-wrap">
          <span
            className="text-[9px] px-1.5 py-0.5 rounded-full font-medium shrink-0"
            style={{
              backgroundColor: `${KIND_COLORS[t.kind] || "#8b95a3"}20`,
              color: KIND_COLORS[t.kind] || "#8b95a3",
            }}
          >
            {KIND_LABELS[t.kind] || t.kind}
          </span>
          <span className="text-[13px] font-semibold text-foreground truncate flex-1 min-w-0">
            {t.name}
          </span>
        </div>
        {t.description && (
          <div className="text-[11px] text-muted-foreground mt-1 leading-snug">
            {t.description}
          </div>
        )}
        <div className="flex items-center gap-2 mt-1.5 text-[9.5px] text-muted-foreground/70 flex-wrap">
          {t.author_name && (
            <span>by {t.author_name}</span>
          )}
          <span className="flex items-center gap-1">
            <svg width="9" height="9" viewBox="0 0 24 24" fill={t.hearted ? "currentColor" : "none"} stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className={t.hearted ? "text-red-400" : ""}>
              <path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.29 1.51 4.04 3 5.5l7 7Z" />
            </svg>
            {t.hearts}
          </span>
          <span className="flex items-center gap-1">
            <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
              <polyline points="7 10 12 15 17 10" />
              <line x1="12" y1="15" x2="12" y2="3" />
            </svg>
            {t.downloads}
          </span>
          {t.is_public && (
            <span className="text-emerald-400">Public</span>
          )}
          <span>· updated {new Date(t.updated_at).toLocaleDateString()}</span>
        </div>
        {t.tags && t.tags.length > 0 && (
          <div className="flex items-center gap-1 mt-1.5 flex-wrap">
            {t.tags.map((tag) => (
              <span key={tag} className="text-[9px] px-1.5 py-0.5 rounded-full bg-surface2 text-muted-foreground">
                #{tag}
              </span>
            ))}
          </div>
        )}
      </div>

      {/* View-mode toggle — Human Readable (default) | See Raw.
          Sticky at the top of the markdown body so it's always reachable. */}
      <div className="sticky top-0 z-10 flex items-center gap-1.5 px-3 py-1.5 border-b border-border bg-surface/95 backdrop-blur shrink-0">
        <div className="flex items-center bg-surface2 rounded-xl p-0.5 text-[10.5px]">
          <button
            onClick={() => setViewMode("human")}
            className={`px-2 py-0.5 rounded-xl transition-colors flex items-center gap-1 ${
              viewMode === "human"
                ? "bg-accent text-white"
                : "text-muted-foreground hover:text-foreground"
            }`}
            aria-pressed={viewMode === "human"}
          >
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M4 7V4h16v3" />
              <path d="M9 20h6" />
              <path d="M12 4v16" />
            </svg>
            Human Readable
          </button>
          <button
            onClick={() => setViewMode("raw")}
            className={`px-2 py-0.5 rounded-xl transition-colors flex items-center gap-1 ${
              viewMode === "raw"
                ? "bg-accent text-white"
                : "text-muted-foreground hover:text-foreground"
            }`}
            aria-pressed={viewMode === "raw"}
            title="Show the raw markdown source — system prompt, role instructions, fanout config, shards"
          >
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="16 18 22 12 16 6" />
              <polyline points="8 6 2 12 8 18" />
            </svg>
            See Raw
          </button>
        </div>
        <div className="flex-1" />
        {viewMode === "raw" && (
          <>
            <span className="text-[9.5px] text-muted-foreground/60 hidden sm:inline tabular-nums">
              {(t.markdown || "").length.toLocaleString()} chars
            </span>
            <button
              onClick={handleCopyRaw}
              className="touch-target text-[10px] px-2 h-7 rounded-xl border border-border text-muted-foreground hover:text-foreground hover:bg-surface2 transition-colors flex items-center gap-1"
              title="Copy raw markdown to clipboard"
            >
              {copied ? (
                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className="text-emerald-400">
                  <polyline points="20 6 9 17 4 12" />
                </svg>
              ) : (
                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
                  <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
                </svg>
              )}
              {copied ? "Copied" : "Copy"}
            </button>
          </>
        )}
      </div>

      {/* Body — rendered markdown OR color-coded raw source.
          BATCH-2 Task 6.2 — raw view now uses .md-raw (CSS-based syntax
          highlighting with purple/pink/green/cyan colors for headers,
          code, lists, etc.) instead of a flat black/white <pre>. */}
      {viewMode === "human" ? (
        <div className={`flex-1 overflow-y-auto ${compact ? "p-2" : "p-3"}`}>
          <Markdown text={t.markdown || "_No markdown body._"} />
        </div>
      ) : (
        <div className="flex-1 overflow-auto bg-background/60">
          <MarkdownRaw
            text={t.markdown || "(empty template — no markdown body)"}
            className={compact ? "p-2" : "p-3"}
          />
        </div>
      )}

      {/* Actions */}
      <div className="px-3 py-2 border-t border-border shrink-0 flex items-center gap-1.5 flex-wrap">
        <button
          onClick={() => onApply(t)}
          className="text-[11px] px-3 py-1.5 rounded-xl bg-accent text-white hover:bg-accent/90 transition-colors font-medium flex items-center gap-1.5"
        >
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="20 6 9 17 4 12" />
          </svg>
          Use this template
        </button>
        {onHeart && (
          <button
            onClick={() => onHeart(t)}
            className={`text-[11px] px-2 py-1.5 rounded-xl border transition-colors flex items-center gap-1 ${
              t.hearted
                ? "border-red-400/40 text-red-400 bg-red-400/10"
                : "border-border text-muted-foreground hover:text-red-400 hover:border-red-400/40"
            }`}
            title={t.hearted ? "Unheart" : "Heart"}
          >
            <svg width="11" height="11" viewBox="0 0 24 24" fill={t.hearted ? "currentColor" : "none"} stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.29 1.51 4.04 3 5.5l7 7Z" />
            </svg>
            {t.hearted ? "Hearted" : "Heart"}
          </button>
        )}
        {onDownload && !t.downloaded && (
          <button
            onClick={() => onDownload(t)}
            className="text-[11px] px-2 py-1.5 rounded-xl border border-border text-muted-foreground hover:text-accent hover:border-accent/40 transition-colors flex items-center gap-1"
            title="Download — creates a local copy you can edit"
          >
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
              <polyline points="7 10 12 15 17 10" />
              <line x1="12" y1="15" x2="12" y2="3" />
            </svg>
            Download
          </button>
        )}
        {onDownload && t.downloaded && (
          <span className="text-[11px] px-2 py-1.5 rounded-xl border border-emerald-400/40 text-emerald-400 flex items-center gap-1">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="20 6 9 17 4 12" />
            </svg>
            Downloaded
          </span>
        )}
        <div className="flex-1" />
        {onEdit && (
          <button
            onClick={() => onEdit(t)}
            className="text-[10px] px-1.5 py-1 rounded text-muted-foreground hover:text-foreground hover:bg-surface2 transition-colors"
          >
            Edit
          </button>
        )}
        {onPublish && (
          <button
            onClick={() => onPublish(t, !t.is_public)}
            className="text-[10px] px-1.5 py-1 rounded text-muted-foreground hover:text-foreground hover:bg-surface2 transition-colors"
          >
            {t.is_public ? "Unpublish" : "Publish"}
          </button>
        )}
        {onDelete && (
          <button
            onClick={() => onDelete(t)}
            className="text-[10px] px-1.5 py-1 rounded text-muted-foreground hover:text-red-400 hover:bg-surface2 transition-colors"
          >
            Delete
          </button>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// TemplateEditor — create/edit form
// ---------------------------------------------------------------------------

interface TemplateEditorProps {
  client: TemplateClient;
  existing: Template | null;
  defaultKind: TemplateKind;
  onCancel: () => void;
  onSaved: (t: Template) => void;
}

function TemplateEditor({
  client,
  existing,
  defaultKind,
  onCancel,
  onSaved,
}: TemplateEditorProps) {
  const [name, setName] = useState(existing?.name || "");
  const [description, setDescription] = useState(existing?.description || "");
  const [markdown, setMarkdown] = useState(existing?.markdown || "");
  const [kind, setKind] = useState<TemplateKind>(existing?.kind || defaultKind);
  const [tagsInput, setTagsInput] = useState((existing?.tags || []).join(", "));
  const [isPublic, setIsPublic] = useState(existing?.is_public ?? false);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const handleSave = async () => {
    if (!name.trim()) {
      setErr("Name is required");
      return;
    }
    if (!markdown.trim()) {
      setErr("Markdown body is required");
      return;
    }
    setSaving(true);
    setErr(null);
    const tags = tagsInput
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    try {
      let result: Template;
      if (existing) {
        const r = await client.update(existing.id, {
          name: name.trim(),
          description: description.trim() || undefined,
          markdown,
          tags,
          is_public: isPublic,
        });
        result = r.template;
      } else {
        const r = await client.create({
          name: name.trim(),
          description: description.trim() || undefined,
          markdown,
          kind,
          tags,
          is_public: isPublic,
        });
        result = r.template;
        // If they want to publish on creation, do it as a separate call
        // (the create endpoint may or may not honor is_public in v1).
        if (isPublic && !result.is_public) {
          try {
            const p = await client.publish(result.id);
            result = p.template;
          } catch {
            /* non-fatal */
          }
        }
      }
      onSaved(result);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed to save template");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex-1 flex flex-col min-h-0">
      <div className="flex items-center justify-between px-3 py-2 border-b border-border shrink-0">
        <span className="text-[12px] font-medium text-foreground">
          {existing ? "Edit template" : "Create a new template"}
        </span>
        <button
          onClick={onCancel}
          className="text-[11px] px-2 py-1 rounded text-muted-foreground hover:text-foreground hover:bg-surface2 transition-colors"
        >
          Cancel
        </button>
      </div>

      <div className="flex-1 overflow-y-auto p-3 space-y-3">
        <div>
          <label className="block text-[10px] uppercase tracking-wide text-muted-foreground/70 mb-1">
            Name <span className="text-red-400">*</span>
          </label>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Repo Audit"
            className="w-full px-2.5 py-1.5 rounded-xl bg-surface2 border border-border text-[16px] sm:text-[12.5px] text-foreground placeholder:text-muted-foreground/50 outline-none focus:border-accent"
          />
        </div>

        <div>
          <label className="block text-[10px] uppercase tracking-wide text-muted-foreground/70 mb-1">
            Description
          </label>
          <input
            type="text"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Short one-line summary"
            className="w-full px-2.5 py-1.5 rounded-xl bg-surface2 border border-border text-[16px] sm:text-[12.5px] text-foreground placeholder:text-muted-foreground/50 outline-none focus:border-accent"
          />
        </div>

        <div className="flex items-start gap-3">
          <div className="flex-1">
            <label className="block text-[10px] uppercase tracking-wide text-muted-foreground/70 mb-1">
              Kind
            </label>
            <select
              value={kind}
              onChange={(e) => setKind(e.target.value as TemplateKind)}
              disabled={!!existing}
              className="w-full px-2.5 py-1.5 rounded-xl bg-surface2 border border-border text-[16px] sm:text-[12.5px] text-foreground outline-none focus:border-accent disabled:opacity-60"
            >
              {ALL_KINDS.map((k) => (
                <option key={k} value={k}>
                  {KIND_LABELS[k]}
                </option>
              ))}
            </select>
          </div>
          <div className="flex-1">
            <label className="block text-[10px] uppercase tracking-wide text-muted-foreground/70 mb-1">
              Tags <span className="text-muted-foreground/40 normal-case tracking-normal">(comma-separated)</span>
            </label>
            <input
              type="text"
              value={tagsInput}
              onChange={(e) => setTagsInput(e.target.value)}
              placeholder="audit, security, refactor"
              className="w-full px-2.5 py-1.5 rounded-xl bg-surface2 border border-border text-[16px] sm:text-[12.5px] text-foreground placeholder:text-muted-foreground/50 outline-none focus:border-accent"
            />
          </div>
        </div>

        <div>
          <label className="block text-[10px] uppercase tracking-wide text-muted-foreground/70 mb-1">
            Markdown body <span className="text-red-400">*</span>
          </label>
          <textarea
            value={markdown}
            onChange={(e) => setMarkdown(e.target.value)}
            rows={12}
            placeholder="Write the prompt markdown here. Use {{variables}} for substitution, sections with ## headers, etc."
            className="w-full px-2.5 py-1.5 rounded-xl bg-surface2 border border-border text-[12px] text-foreground placeholder:text-muted-foreground/50 outline-none focus:border-accent font-mono leading-relaxed resize-y min-h-[200px]"
          />
          <div className="mt-1 text-[10px] text-muted-foreground/60">
            Preview:
          </div>
          <div className="mt-1 px-3 py-2 rounded-xl border border-border bg-surface2/50 max-h-[200px] overflow-y-auto">
            <Markdown text={markdown || "_Markdown preview will appear here._"} />
          </div>
        </div>

        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            checked={isPublic}
            onChange={(e) => setIsPublic(e.target.checked)}
            className="size-3.5 accent-accent"
          />
          <span className="text-[12px] text-foreground">
            Publish to the Explore feed (other users can heart + download)
          </span>
        </label>

        {err && (
          <div className="text-[11px] text-red-400 px-2 py-1 rounded bg-red-400/10 border border-red-400/30">
            {err}
          </div>
        )}
      </div>

      <div className="px-3 py-2 border-t border-border shrink-0 flex items-center gap-2 justify-end">
        <button
          onClick={onCancel}
          className="text-[11px] px-3 py-1.5 rounded-xl text-muted-foreground hover:text-foreground hover:bg-surface2 transition-colors"
        >
          Cancel
        </button>
        <button
          onClick={handleSave}
          disabled={saving}
          className="text-[11px] px-3 py-1.5 rounded-xl bg-accent text-white hover:bg-accent/90 transition-colors font-medium disabled:opacity-50"
        >
          {saving ? "Saving…" : existing ? "Save changes" : "Create template"}
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// EmptyState — shown when a list has no entries
// ---------------------------------------------------------------------------

function EmptyState({
  title,
  body,
  cta,
}: {
  title: string;
  body: string;
  cta: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center p-8 text-center">
      <svg
        width="32"
        height="32"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        className="text-muted-foreground/40 mb-3 empty-state-icon"
      >
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
        <path d="M14 2v6h6" />
      </svg>
      <div className="text-[13px] font-medium text-foreground mb-1">{title}</div>
      <div className="text-[11.5px] text-muted-foreground leading-relaxed max-w-sm">
        {body}
      </div>
      {cta && <div className="mt-3">{cta}</div>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// AiGenerateDialog — BATCH-2 Task 6.4
// Asks the user what kind of template they want, then drops the request
// into the chat input so the agent can create it.
// ---------------------------------------------------------------------------

function AiGenerateDialog({
  open,
  onClose,
  onSubmit,
}: {
  open: boolean;
  onClose: () => void;
  onSubmit: (kind: TemplateKind, description: string) => void;
}) {
  const [kind, setKind] = useState<TemplateKind>("websearch");
  const [description, setDescription] = useState("");

  // Reset on open.
  useEffect(() => {
    if (open) {
      setKind("websearch");
      setDescription("");
    }
  }, [open]);

  return (
    <AnimatePresence>
      {open && (
        <>
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.13 }}
            className="absolute inset-0 z-[70] bg-black/60"
            onClick={onClose}
            aria-hidden="true"
          />
          <motion.div
            initial={{ opacity: 0, scale: 0.96, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.96, y: 8 }}
            transition={{ duration: 0.15, ease: [0.19, 1, 0.22, 1] }}
            className="absolute inset-x-2 sm:inset-x-auto sm:left-1/2 sm:-translate-x-1/2 top-1/2 -translate-y-1/2 z-[70] w-auto sm:w-[440px] max-w-[calc(100vw-1rem)] flex flex-col rounded-2xl border border-border bg-surface shadow-2xl shadow-black/40 overflow-hidden"
            onClick={(e) => e.stopPropagation()}
            role="dialog"
            aria-modal="true"
            aria-label="Generate template with AI"
          >
            <div className="flex items-center gap-2 px-3.5 h-11 border-b border-border shrink-0">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-accent shrink-0">
                <path d="M12 2L3 14h9l-1 8 10-12h-9l1-8z" />
              </svg>
              <span className="text-[13px] font-semibold text-foreground shrink-0">
                Generate with AI
              </span>
              <button
                onClick={onClose}
                className="touch-target ml-auto p-1.5 rounded-xl text-muted-foreground hover:text-foreground hover:bg-surface2 transition-colors shrink-0"
                aria-label="Close"
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="18" y1="6" x2="6" y2="18" />
                  <line x1="6" y1="6" x2="18" y2="18" />
                </svg>
              </button>
            </div>
            <div className="flex-1 overflow-y-auto p-3.5 space-y-3">
              <div>
                <label className="block text-[10px] uppercase tracking-wide text-muted-foreground/70 mb-1">
                  Kind
                </label>
                <select
                  value={kind}
                  onChange={(e) => setKind(e.target.value as TemplateKind)}
                  className="w-full px-2.5 py-1.5 rounded-xl bg-surface2 border border-border text-[16px] sm:text-[12.5px] text-foreground outline-none focus:border-accent"
                >
                  {ALL_KINDS.map((k) => (
                    <option key={k} value={k}>
                      {KIND_LABELS[k]}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-[10px] uppercase tracking-wide text-muted-foreground/70 mb-1">
                  What should it do?
                </label>
                <textarea
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  rows={4}
                  placeholder="e.g. Audit a repo for security issues, then suggest fixes with code samples."
                  className="w-full px-2.5 py-1.5 rounded-xl bg-surface2 border border-border text-[16px] sm:text-[12.5px] text-foreground placeholder:text-muted-foreground/50 outline-none focus:border-accent resize-y min-h-[96px]"
                />
                <p className="text-[10px] text-muted-foreground/60 mt-1 leading-snug">
                  The agent will create a {KIND_LABELS[kind] || kind.toLowerCase()} template using
                  the roles.py system (planner, generator, critiquer, etc.) and drop it into your
                  chat — review + save it from there.
                </p>
              </div>
            </div>
            <div className="px-3.5 py-2.5 border-t border-border shrink-0 flex items-center gap-2 justify-end">
              <button
                onClick={onClose}
                className="touch-target text-[11.5px] px-3 py-1.5 rounded-xl text-muted-foreground hover:text-foreground hover:bg-surface2 transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={() => onSubmit(kind, description)}
                className="touch-target text-[11.5px] px-3 py-1.5 rounded-xl bg-accent text-white hover:bg-accent/90 transition-colors font-medium flex items-center gap-1.5"
              >
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M12 2L3 14h9l-1 8 10-12h-9l1-8z" />
                </svg>
                Send to agent
              </button>
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}

// ---------------------------------------------------------------------------
// MarkdownRaw — BATCH-2 Task 6.2
// Color-coded raw markdown view. Renders the raw source with syntax
// highlighting: headers in purple/pink, code in cyan/green, lists in soft
// gray, {{variables}} in amber, etc. CSS classes live in index.css (.md-raw).
// Reads as a "code editor" view of the prompt skeleton (role definitions,
// fanout config, shards).
// ---------------------------------------------------------------------------

function MarkdownRaw({
  text,
  className = "",
}: {
  text: string;
  className?: string;
}) {
  // Tokenize the markdown into spans with color classes. We use a simple
  // line-based tokenizer (no full markdown parser) — good enough for the
  // raw view, which is meant to look like a syntax-highlighted code editor.
  const lines = text.split("\n");
  const inFence = { current: false }; // mutates as we walk; tracks ``` blocks
  const out: React.ReactNode[] = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const trimmed = line.trim();
    // Fence toggle.
    if (/^```/.test(trimmed)) {
      inFence.current = !inFence.current;
      out.push(
        <div key={i} className="md-raw-fence-start">
          {line || "\u00a0"}
        </div>,
      );
      continue;
    }
    if (inFence.current) {
      // Inside a fence — color the whole line as code.
      out.push(
        <div key={i} className="md-raw-fence">
          {line || "\u00a0"}
        </div>,
      );
      continue;
    }
    // Headers.
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      const level = h[1].length;
      const cls = level <= 1 ? "md-raw-h1" : level === 2 ? "md-raw-h2" : level === 3 ? "md-raw-h3" : "md-raw-h4";
      out.push(
        <div key={i} className={cls}>
          {renderInlineSpans(h[2])}
        </div>,
      );
      continue;
    }
    // Horizontal rule.
    if (/^(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      out.push(
        <div key={i} className="md-raw-hr">
          {line || "\u00a0"}
        </div>,
      );
      continue;
    }
    // Blockquote.
    if (/^>\s?/.test(line)) {
      out.push(
        <div key={i} className="md-raw-bq">
          {renderInlineSpans(line.replace(/^>\s?/, ""))}
        </div>,
      );
      continue;
    }
    // Unordered list item.
    if (/^[-*+]\s+/.test(line)) {
      out.push(
        <div key={i}>
          <span className="md-raw-li">{"• "}</span>
          {renderInlineSpans(line.replace(/^[-*+]\s+/, ""))}
        </div>,
      );
      continue;
    }
    // Ordered list item.
    if (/^\d+\.\s+/.test(line)) {
      const m = /^(\d+\.)\s+(.*)$/.exec(line);
      out.push(
        <div key={i}>
          <span className="md-raw-ol">{m ? m[1] + " " : ""}</span>
          {renderInlineSpans(m ? m[2] : "")}
        </div>,
      );
      continue;
    }
    // Key: value (common in YAML frontmatter / config blocks).
    const kv = /^([A-Za-z_][A-Za-z0-9_]*):(.*)$/.exec(line);
    if (kv && !line.startsWith(" ")) {
      out.push(
        <div key={i}>
          <span className="md-raw-kv">{kv[1]}:</span>
          {renderInlineSpans(kv[2])}
        </div>,
      );
      continue;
    }
    // Comment.
    if (/^<!--.*-->\s*$/.test(line)) {
      out.push(
        <div key={i} className="md-raw-comment">
          {line || "\u00a0"}
        </div>,
      );
      continue;
    }
    // Empty line.
    if (!line.trim()) {
      out.push(<div key={i}>{"\u00a0"}</div>);
      continue;
    }
    // Default paragraph.
    out.push(<div key={i}>{renderInlineSpans(line)}</div>);
  }
  return <div className={`md-raw ${className}`}>{out}</div>;
}

/** Render inline markdown spans (bold, italic, code, links, vars) with
 *  appropriate color classes. Cheap regex tokenizer — good enough for
 *  the raw view. */
function renderInlineSpans(text: string): React.ReactNode[] {
  const nodes: React.ReactNode[] = [];
  // Pattern matches: **bold**, *italic*, `code`, [text](url), {{var}}, <!--comment-->
  const re = /(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`|\[[^\]]+\]\([^)]+\)|\{\{[^}]+\}\}|<!--[^>]+-->)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let key = 0;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) nodes.push(text.slice(last, m.index));
    const tok = m[0];
    if (tok.startsWith("**")) {
      nodes.push(<span key={key++} className="md-raw-strong">{tok.slice(2, -2)}</span>);
    } else if (tok.startsWith("*")) {
      nodes.push(<span key={key++} className="md-raw-em">{tok.slice(1, -1)}</span>);
    } else if (tok.startsWith("`")) {
      nodes.push(<span key={key++} className="md-raw-code">{tok.slice(1, -1)}</span>);
    } else if (tok.startsWith("[")) {
      const lm = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(tok);
      if (lm) {
        nodes.push(
          <span key={key++}>
            <span className="md-raw-link">{lm[1]}</span>
            <span className="md-raw-url"> ({lm[2]})</span>
          </span>,
        );
      } else {
        nodes.push(tok);
      }
    } else if (tok.startsWith("{{")) {
      nodes.push(<span key={key++} className="md-raw-var">{tok}</span>);
    } else if (tok.startsWith("<!--")) {
      nodes.push(<span key={key++} className="md-raw-comment">{tok}</span>);
    } else {
      nodes.push(tok);
    }
    last = m.index + tok.length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}
