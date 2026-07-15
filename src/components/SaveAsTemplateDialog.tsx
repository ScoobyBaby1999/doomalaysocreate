/**
 * SaveAsTemplateDialog — lightweight "quick capture" flow that lets the user
 * save the current chat context as a reusable template.
 *
 * Triggered from the chat input toolbar's "Save as Template" button (next to
 * the tool icons). Simpler than the full TemplateLibrary editor — just:
 *   1. Pre-fills the markdown with the current conversation context (most
 *      recent user message + assistant reply, plus a leading system-prompt
 *      header so the template is self-describing).
 *   2. Lets the user name it and choose a kind (websearch/deepresearch/
 *      judge/chat).
 *   3. Saves it via the TemplateLibrary API (POST /api/templates).
 *
 * After saving, calls onSaved(template) so the caller can route the template
 * to the right tool slot (via applyTemplate) and toast the user.
 */

import { useEffect, useMemo, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { TemplateClient, type Template, type TemplateKind } from "../api/templates";
import type { Settings } from "../api/panel";
import type { ChatMessage } from "../state/chatStore";

const KIND_LABELS: Record<TemplateKind, string> = {
  websearch: "Web Search",
  deepresearch: "Deep Research",
  judge: "Judge",
  chat: "Chat",
  custom: "Custom",
};

const KIND_HINTS: Record<TemplateKind, string> = {
  websearch: "Use as a system prompt for web-search turns",
  deepresearch: "Use as a system prompt for deep-research turns",
  judge: "Use as the prompt for judge-panel fan-out",
  chat: "Use as a system prompt for plain chat",
  custom: "Free-form — won't be auto-applied",
};

const ALL_KINDS: TemplateKind[] = ["websearch", "deepresearch", "judge", "chat", "custom"];

interface SaveAsTemplateDialogProps {
  open: boolean;
  settings: Settings;
  /** Conversation context used to pre-fill the markdown body. */
  messages: ChatMessage[];
  /** Optional pre-fill for the name (e.g. last user message, truncated). */
  suggestedName?: string;
  /** Default kind when the dialog opens. */
  defaultKind?: TemplateKind;
  onClose: () => void;
  /** Fired after a successful save. Caller may applyTemplate to use it now. */
  onSaved: (template: Template) => void;
}

/**
 * Build the initial markdown body from the conversation context. We pull:
 *   - The most-recent user message (the "ask")
 *   - The most-recent assistant reply (the "answer")
 *   - Wrap them in a clear "system prompt + user example + assistant example"
 *     structure so the template is self-describing and reusable.
 *
 * If the conversation is empty, we drop in a starter skeleton.
 */
function buildInitialMarkdown(messages: ChatMessage[], kind: TemplateKind): string {
  // Find the last user message (skip errors / tool events).
  let lastUser: ChatMessage | null = null;
  let lastAssistant: ChatMessage | null = null;
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.role === "user" && !m.isError && !lastUser) {
      lastUser = m;
    } else if (m.role === "assistant" && !m.isError && !lastAssistant) {
      lastAssistant = m;
    }
    if (lastUser && lastAssistant) break;
  }

  const kindLabel = KIND_LABELS[kind] || "Custom";
  const header = `# ${kindLabel} Template\n\n> Captured from chat on ${new Date().toLocaleString()}.\n> Edit the sections below to customize behavior.\n\n## System Prompt\n\nYou are a helpful, expert assistant. Be precise and concise.\n`;

  if (!lastUser && !lastAssistant) {
    return `${header}\n## User\n\n<describe the task here>\n\n## Assistant\n\n<describe the expected output format here>\n`;
  }

  const userBlock = lastUser
    ? `## User\n\n${(lastUser.content || "").trim()}\n`
    : "";
  const assistantBlock = lastAssistant
    ? `\n## Assistant\n\n${(lastAssistant.content || "").trim()}\n`
    : "";

  return `${header}${userBlock ? "\n" + userBlock : ""}${assistantBlock}`;
}

export function SaveAsTemplateDialog({
  open,
  settings,
  messages,
  suggestedName,
  defaultKind = "chat",
  onClose,
  onSaved,
}: SaveAsTemplateDialogProps) {
  const client = useMemo(() => new TemplateClient(settings), [settings]);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [kind, setKind] = useState<TemplateKind>(defaultKind);
  const [markdown, setMarkdown] = useState("");
  const [isPublic, setIsPublic] = useState(false);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Reset + pre-fill when the dialog opens. The markdown is generated from
  // the current conversation context (most-recent user + assistant pair).
  useEffect(() => {
    if (!open) return;
    setName(suggestedName || "");
    setDescription("");
    setKind(defaultKind);
    setMarkdown(buildInitialMarkdown(messages, defaultKind));
    setIsPublic(false);
    setErr(null);
  }, [open, suggestedName, defaultKind, messages]);

  // Close on Escape.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

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
    try {
      const r = await client.create({
        name: name.trim(),
        description: description.trim() || undefined,
        markdown,
        kind,
        is_public: isPublic,
      });
      let result = r.template;
      // If they want to publish on creation, do it as a separate call
      // (the create endpoint may not honor is_public in v1).
      if (isPublic && !result.is_public) {
        try {
          const p = await client.publish(result.id);
          result = p.template;
        } catch {
          /* non-fatal */
        }
      }
      onSaved(result);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed to save template");
    } finally {
      setSaving(false);
    }
  };

  const field =
    "w-full bg-surface2 border border-border rounded-xl px-3 py-2 text-[16px] sm:text-[13px] outline-none focus:border-accent transition-colors";

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
            className="fixed inset-0 z-[70] bg-black/50 backdrop-blur-sm"
            onClick={onClose}
            aria-hidden="true"
          />
          {/* Sheet — centered modal on desktop, bottom sheet on mobile. */}
          <motion.div
            initial={{ opacity: 0, y: 40 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 40 }}
            transition={{ duration: 0.2, ease: [0.19, 1, 0.22, 1] }}
            className="fixed inset-0 z-[70] flex items-end sm:items-center justify-center p-0 sm:p-6 pointer-events-none"
          >
            <div
              className="pointer-events-auto w-full sm:max-w-lg flex flex-col rounded-t-3xl sm:rounded-2xl border border-border bg-surface shadow-2xl overflow-hidden"
              style={{ maxHeight: "92dvh" }}
              onClick={(e) => e.stopPropagation()}
              role="dialog"
              aria-modal="true"
              aria-label="Save chat as template"
            >
              {/* Drag handle (mobile only) */}
              <div className="sm:hidden flex justify-center pt-2 pb-1 shrink-0">
                <span className="block w-10 h-1 rounded-full bg-border" />
              </div>

              {/* Header */}
              <div className="flex items-center justify-between gap-3 px-4 h-11 border-b border-border shrink-0">
                <div className="flex items-center gap-2 min-w-0">
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
                    <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z" />
                    <polyline points="17 21 17 13 7 13 7 21" />
                    <polyline points="7 3 7 8 15 8" />
                  </svg>
                  <span className="text-[13px] font-semibold text-foreground truncate">
                    Save as Template
                  </span>
                </div>
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

              {/* Body — scrollable. */}
              <div className="flex-1 overflow-y-auto p-4 space-y-3">
                {/* Name */}
                <div>
                  <label className="block text-[10px] uppercase tracking-wide text-muted-foreground/70 mb-1">
                    Name <span className="text-red-400">*</span>
                  </label>
                  <input
                    type="text"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="e.g. Repo Audit Prompt"
                    autoFocus
                    className={field}
                  />
                </div>

                {/* Kind */}
                <div>
                  <label className="block text-[10px] uppercase tracking-wide text-muted-foreground/70 mb-1">
                    Kind
                  </label>
                  <div className="grid grid-cols-3 sm:grid-cols-5 gap-1.5">
                    {ALL_KINDS.map((k) => (
                      <button
                        key={k}
                        onClick={() => setKind(k)}
                        className={`touch-target text-[11px] px-2 py-1.5 rounded-xl border transition-colors ${
                          kind === k
                            ? "border-accent text-accent bg-accent/10"
                            : "border-border text-muted-foreground hover:text-foreground hover:border-accent/40"
                        }`}
                        title={KIND_HINTS[k]}
                      >
                        {KIND_LABELS[k]}
                      </button>
                    ))}
                  </div>
                  <p className="text-[10px] text-muted-foreground/60 mt-1 leading-snug">
                    {KIND_HINTS[kind]}
                  </p>
                </div>

                {/* Description */}
                <div>
                  <label className="block text-[10px] uppercase tracking-wide text-muted-foreground/70 mb-1">
                    Description <span className="text-muted-foreground/40 normal-case tracking-normal">(optional)</span>
                  </label>
                  <input
                    type="text"
                    value={description}
                    onChange={(e) => setDescription(e.target.value)}
                    placeholder="Short one-line summary"
                    className={field}
                  />
                </div>

                {/* Markdown body — pre-filled from the conversation context.
                    Editable so the user can trim/adjust before saving. */}
                <div>
                  <label className="block text-[10px] uppercase tracking-wide text-muted-foreground/70 mb-1">
                    Markdown body <span className="text-red-400">*</span>
                  </label>
                  <textarea
                    value={markdown}
                    onChange={(e) => setMarkdown(e.target.value)}
                    rows={10}
                    spellCheck={false}
                    className="w-full bg-surface2 border border-border rounded-xl px-3 py-2 text-[12px] sm:text-[12.5px] font-mono leading-relaxed outline-none focus:border-accent transition-colors resize-y min-h-[200px] max-h-[40dvh] text-foreground"
                  />
                  <p className="text-[10px] text-muted-foreground/60 mt-1 leading-snug">
                    Pre-filled from the latest user + assistant messages in this chat. Edit freely.
                  </p>
                </div>

                {/* Publish toggle */}
                <label className="flex items-start gap-2 cursor-pointer pt-1">
                  <input
                    type="checkbox"
                    checked={isPublic}
                    onChange={(e) => setIsPublic(e.target.checked)}
                    className="size-3.5 accent-accent mt-0.5 shrink-0"
                  />
                  <span className="text-[12px] text-foreground leading-snug">
                    Publish to the Explore feed (other users can heart + download)
                  </span>
                </label>

                {err && (
                  <div className="text-[11px] text-red-400 px-3 py-2 rounded-xl bg-red-400/10 border border-red-400/30">
                    {err}
                  </div>
                )}
              </div>

              {/* Footer — actions. */}
              <div className="px-4 py-3 border-t border-border shrink-0 flex items-center gap-2 justify-end safe-bottom">
                <button
                  onClick={onClose}
                  className="touch-target text-[12px] px-3 py-2 rounded-xl text-muted-foreground hover:text-foreground hover:bg-surface2 transition-colors"
                >
                  Cancel
                </button>
                <button
                  onClick={handleSave}
                  disabled={saving}
                  className="touch-target text-[12px] px-4 py-2 rounded-xl bg-accent text-white hover:bg-accent/90 transition-colors font-medium disabled:opacity-50 flex items-center gap-1.5"
                >
                  {saving ? (
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className="animate-spin">
                      <path d="M21 12a9 9 0 1 1-6.219-8.56" />
                    </svg>
                  ) : (
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z" />
                      <polyline points="17 21 17 13 7 13 7 21" />
                    </svg>
                  )}
                  {saving ? "Saving…" : "Save template"}
                </button>
              </div>
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}
