import { useState, memo } from "react";
import { Markdown } from "./Markdown";
import { DiffView } from "./DiffView";
import type { ChatMessage, ChatSource } from "../state/chatStore";

const TOOL_ICONS: Record<string, string> = {
  bash: "Terminal",
  shell: "Terminal",
  write: "FileEdit",
  edit: "FileEdit",
  str_replace: "FileEdit",
  fileeditor: "FileEdit",
  read: "Eye",
  view: "Eye",
  glob: "Search",
  grep: "Search",
  search: "Search",
  web: "Globe",
  fetch: "Globe",
  http_request: "Globe",
};

function toolIcon(name: string): string {
  const k = name.toLowerCase();
  for (const key of Object.keys(TOOL_ICONS)) {
    if (k.includes(key)) return TOOL_ICONS[key];
  }
  return "Wrench";
}

function isDiff(text: string): boolean {
  return text.includes("---") && text.includes("+++") && /^diff --git/.test(text.trim());
}

/** Map a backend stage hint to a human label. */
function stageLabel(stage: string): string {
  if (stage === "searching") return "Searching the web…";
  if (stage === "synthesizing") return "Synthesizing…";
  if (stage.startsWith("reading:")) {
    const n = stage.slice("reading:".length);
    return `Reading ${n} source${n === "1" ? "" : "s"}…`;
  }
  if (stage.startsWith("judge:")) return "Judge panel running…";
  // Fall back to whatever the backend sent, humanized.
  return stage.charAt(0).toUpperCase() + stage.slice(1) + "…";
}

interface ChatMessageBubbleProps {
  message: ChatMessage;
  /** Optional retry callback for the last assistant message. */
  onRetry?: () => void;
}

export const ChatMessageBubble = memo(function ChatMessageBubble({
  message,
  onRetry,
}: ChatMessageBubbleProps) {
  // -- User message: right-aligned bubble with pending indicator ----------
  if (message.role === "user") {
    return (
      <div className="flex justify-end px-4 py-1.5 group fade-in">
        <div className="flex flex-col items-end gap-0.5 max-w-[85%]">
          <div
            className={`rounded-2xl rounded-br-md px-4 py-2.5 text-[14.5px] leading-relaxed whitespace-pre-wrap break-words ${
              message.pending
                ? "bg-accent/10 text-foreground/70 italic"
                : "bg-accent/15 text-foreground"
            }`}
          >
            {message.content}
          </div>
          {message.pending && (
            <span className="text-[10px] text-muted-foreground/70 pr-1">
              sending…
            </span>
          )}
        </div>
      </div>
    );
  }

  // -- Assistant message: left-aligned with avatar + markdown -------------
  if (message.role === "assistant") {
    const hasMeta =
      message.tokensIn != null ||
      message.tokensOut != null ||
      message.tokensReasoning != null ||
      typeof message.costUsd === "number";
    return (
      <div className="flex justify-start px-4 py-1.5 group">
        <div className="flex gap-2.5 max-w-[92%] fade-in">
          <Avatar kind="assistant" />
          <div className="flex-1 min-w-0 pt-0.5">
            {/* Status indicator — mid-turn stage hint */}
            {message.isStreaming && message.stage && (
              <div className="flex items-center gap-1.5 text-[10.5px] text-accent/80 mb-1 fade-in">
                <span className="flex gap-0.5">
                  <span className="w-1 h-1 rounded-full bg-accent animate-bounce" style={{ animationDelay: "0ms" }} />
                  <span className="w-1 h-1 rounded-full bg-accent animate-bounce" style={{ animationDelay: "120ms" }} />
                  <span className="w-1 h-1 rounded-full bg-accent animate-bounce" style={{ animationDelay: "240ms" }} />
                </span>
                <span>{stageLabel(message.stage)}</span>
              </div>
            )}
            <div className="text-[14.5px] leading-relaxed text-foreground">
              <Markdown text={message.content} />
              {message.isStreaming && (
                <span className="inline-block w-[6px] h-[15px] ml-0.5 bg-accent animate-pulse align-middle rounded-sm" />
              )}
            </div>
            {/* Sources panel — collapsible list of cited URLs */}
            {message.sources && message.sources.length > 0 && (
              <SourcesPanel sources={message.sources} />
            )}
            {/* Meta + actions row — visible on hover */}
            {!message.isStreaming && message.content.trim() && (
              <div className="flex items-center gap-2 mt-1 opacity-0 group-hover:opacity-100 transition-opacity flex-wrap">
                <CopyButton text={message.content} />
                {onRetry && <RetryButton onClick={onRetry} />}
                {hasMeta && <MessageMeta message={message} />}
              </div>
            )}
            {/* Even when streaming, show meta on hover for live cost tracking */}
            {message.isStreaming && hasMeta && (
              <div className="flex items-center gap-2 mt-1 opacity-0 group-hover:opacity-100 transition-opacity">
                <MessageMeta message={message} />
              </div>
            )}
          </div>
        </div>
      </div>
    );
  }

  // -- Thinking: collapsible card -----------------------------------------
  if (message.role === "thinking") {
    const preview = message.content.slice(0, 80).replace(/\n/g, " ");
    return (
      <div className="px-4 py-1 fade-in">
        <Collapsible
          label={
            message.isStreaming
              ? `Thinking… ${preview}${message.content.length > 80 ? "…" : ""}`
              : "Thinking"
          }
          kind="thinking"
          defaultOpen={!!message.isStreaming}
        >
          <div className="text-[12.5px] text-muted-foreground leading-relaxed">
            <Markdown text={message.content} />
            {message.isStreaming && (
              <span className="inline-block w-[6px] h-[12px] ml-0.5 bg-accent/60 animate-pulse align-middle rounded-sm" />
            )}
          </div>
        </Collapsible>
      </div>
    );
  }

  // -- Tool message: compact indicator or collapsible result --------------
  if (message.role === "tool") {
    if (message.toolName === "result") {
      const isDiffContent = !message.isError && isDiff(message.content);
      if (isDiffContent) {
        return (
          <div className="px-4 py-1 fade-in">
            <div className="rounded-lg border border-border overflow-hidden max-w-2xl">
              <div className="px-3 py-1.5 text-[11px] text-muted-foreground border-b border-border bg-surface/50 font-medium">
                Diff
              </div>
              <DiffView diff={message.content} />
            </div>
          </div>
        );
      }
      return (
        <div className="px-4 py-1 fade-in">
          <Collapsible
            label={message.isError ? "Result (error)" : "Result"}
            kind={message.isError ? "error" : "result"}
          >
            <div className="text-[12.5px] overflow-x-auto">
              <Markdown text={message.content} />
            </div>
          </Collapsible>
        </div>
      );
    }

    // Tool use indicator (no result yet)
    return (
      <div className="px-4 py-0.5">
        <div className="flex items-center gap-2 text-[11.5px] text-muted-foreground">
          <span className="flex items-center gap-1.5">
            <ToolIcon name={message.toolName || "tool"} />
            <span className="font-medium text-accent/90">{message.toolName}</span>
          </span>
          {message.toolSummary ? (
            <span className="font-mono text-[10.5px] truncate max-w-[300px] opacity-70">
              {message.toolSummary}
            </span>
          ) : null}
        </div>
      </div>
    );
  }

  // -- Status message -----------------------------------------------------
  if (message.role === "status") {
    if (message.isError) {
      return (
        <div className="px-4 py-1.5">
          <div className="max-w-2xl mx-auto rounded-xl border border-red-500/40 bg-red-500/10 px-4 py-2.5 text-[13px] text-red-300 flex items-start gap-2">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="mt-0.5 shrink-0">
              <circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" />
            </svg>
            <span className="flex-1">{message.content}</span>
          </div>
        </div>
      );
    }
    return (
      <div className="px-4 py-1 text-center">
        <span className="text-[10.5px] text-muted-foreground/70 italic">
          {message.content}
        </span>
      </div>
    );
  }

  // -- Panel message ------------------------------------------------------
  if (message.role === "panel") {
    return (
      <div className="px-4 py-1.5">
        <div className="max-w-2xl mx-auto rounded-xl border border-border bg-surface/40 overflow-hidden">
          <div className="flex items-center gap-2 px-3 py-2 border-b border-border bg-surface/60">
            <span
              className={`h-2 w-2 rounded-full ${
                message.panelStatus === "starting"
                  ? "bg-muted-foreground"
                  : message.panelStatus === "running"
                  ? "bg-accent animate-pulse"
                  : "bg-emerald-400"
              }`}
            />
            <span className="text-[12.5px] font-medium">Panel: {message.content}</span>
            <span className="ml-auto text-[10.5px] text-muted-foreground capitalize">
              {message.panelStatus}
            </span>
          </div>
        </div>
      </div>
    );
  }

  return null;
});

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function Avatar({ kind }: { kind: "assistant" | "user" }) {
  if (kind === "assistant") {
    return (
      <div className="shrink-0 w-7 h-7 rounded-md bg-accent/15 flex items-center justify-center">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#5b8cff" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 2a10 10 0 0 1 10 10c0 5.523-4.477 10-10 10S2 17.523 2 12 6.477 2 12 2z" />
          <path d="M12 16v-4" />
          <path d="M12 8h.01" />
        </svg>
      </div>
    );
  }
  return null;
}

function Collapsible({
  label,
  children,
  kind,
  defaultOpen,
}: {
  label: string;
  children: React.ReactNode;
  kind?: "thinking" | "result" | "error";
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(kind === "error" || !!defaultOpen);
  const borderClass =
    kind === "error"
      ? "border-red-500/30"
      : kind === "thinking"
      ? "border-border bg-surface/20"
      : "border-border";
  const labelClass =
    kind === "error" ? "text-red-300" : "text-muted-foreground";
  return (
    <div className={`rounded-lg border ${borderClass} max-w-2xl overflow-hidden`}>
      <button
        onClick={() => setOpen((o) => !o)}
        className={`w-full text-left text-[11px] px-3 py-1.5 font-medium ${labelClass} hover:bg-surface/40 transition-colors flex items-center gap-2`}
      >
        <svg
          width="9"
          height="9"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="3"
          strokeLinecap="round"
          strokeLinejoin="round"
          className={`transition-transform duration-150 shrink-0 ${open ? "rotate-90" : ""}`}
        >
          <polyline points="9 18 15 12 9 6" />
        </svg>
        <span className="truncate flex-1">{label}</span>
      </button>
      {open && (
        <div className="px-3 pb-3 pt-1 border-t border-border/50 max-h-[400px] overflow-y-auto">{children}</div>
      )}
    </div>
  );
}

function ToolIcon({ name }: { name: string }) {
  const iconName = toolIcon(name);
  const props = {
    width: 12,
    height: 12,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 2,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
  };
  if (iconName === "Terminal") {
    return (
      <svg {...props}>
        <polyline points="4 17 10 11 4 5" />
        <line x1="12" y1="19" x2="20" y2="19" />
      </svg>
    );
  }
  if (iconName === "FileEdit") {
    return (
      <svg {...props}>
        <path d="M12 20h9" />
        <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
      </svg>
    );
  }
  if (iconName === "Eye") {
    return (
      <svg {...props}>
        <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
        <circle cx="12" cy="12" r="3" />
      </svg>
    );
  }
  if (iconName === "Search") {
    return (
      <svg {...props}>
        <circle cx="11" cy="11" r="8" />
        <line x1="21" y1="21" x2="16.65" y2="16.65" />
      </svg>
    );
  }
  if (iconName === "Globe") {
    return (
      <svg {...props}>
        <circle cx="12" cy="12" r="10" />
        <line x1="2" y1="12" x2="22" y2="12" />
        <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
      </svg>
    );
  }
  return (
    <svg {...props}>
      <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
    </svg>
  );
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={() => {
        navigator.clipboard.writeText(text);
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      }}
      className="text-[9px] px-1.5 py-0.5 rounded text-muted-foreground/60 hover:text-foreground hover:bg-surface2 transition-colors flex items-center gap-1"
      title="Copy to clipboard"
    >
      {copied ? (
        <>
          <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="20 6 9 17 4 12" />
          </svg>
          Copied
        </>
      ) : (
        <>
          <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
            <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
          </svg>
          Copy
        </>
      )}
    </button>
  );
}

function RetryButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="text-[9px] px-1.5 py-0.5 rounded text-muted-foreground/60 hover:text-foreground hover:bg-surface2 transition-colors flex items-center gap-1"
      title="Retry this turn"
    >
      <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <polyline points="23 4 23 10 17 10" />
        <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
      </svg>
      Retry
    </button>
  );
}

/** Per-message metadata row: tokens (in↓ out↑ reasoning🧠) + cost ($ or free). */
function MessageMeta({ message }: { message: ChatMessage }) {
  const parts: React.ReactNode[] = [];
  if (message.tokensIn != null) {
    parts.push(
      <span key="in" className="flex items-center gap-0.5" title="Input tokens">
        <span>↓</span>
        <span>{message.tokensIn.toLocaleString()}</span>
      </span>,
    );
  }
  if (message.tokensOut != null) {
    parts.push(
      <span key="out" className="flex items-center gap-0.5" title="Output tokens">
        <span>↑</span>
        <span>{message.tokensOut.toLocaleString()}</span>
      </span>,
    );
  }
  if (message.tokensReasoning != null && message.tokensReasoning > 0) {
    parts.push(
      <span key="reason" className="flex items-center gap-0.5" title="Reasoning tokens">
        <span>🧠</span>
        <span>{message.tokensReasoning.toLocaleString()}</span>
      </span>,
    );
  }
  if (typeof message.costUsd === "number") {
    if (message.costUsd === 0) {
      parts.push(
        <span key="cost" className="text-emerald-400/80" title="Free model">
          free
        </span>,
      );
    } else {
      const v = message.costUsd;
      const s = v < 0.01 ? `$${v.toFixed(4)}` : `$${v.toFixed(3)}`;
      parts.push(
        <span key="cost" className="text-amber-400/80" title="Cost (USD)">
          {s}
        </span>,
      );
    }
  }
  if (parts.length === 0) return null;
  return (
    <div className="flex items-center gap-2 text-[9px] text-muted-foreground/60 font-mono tabular-nums">
      {parts}
    </div>
  );
}

/** Collapsible "N sources" panel with citation links. */
function SourcesPanel({ sources }: { sources: ChatSource[] }) {
  const [open, setOpen] = useState(false);
  if (sources.length === 0) return null;
  return (
    <div className="mt-1.5 max-w-2xl">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1.5 text-[10px] text-muted-foreground hover:text-foreground transition-colors"
      >
        <svg
          width="9"
          height="9"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="3"
          strokeLinecap="round"
          strokeLinejoin="round"
          className={`transition-transform duration-150 ${open ? "rotate-90" : ""}`}
        >
          <polyline points="9 18 15 12 9 6" />
        </svg>
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
          <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
        </svg>
        <span>
          {sources.length} source{sources.length !== 1 ? "s" : ""}
        </span>
      </button>
      {open && (
        <div className="mt-1 rounded-lg border border-border bg-surface/30 overflow-hidden">
          {sources.map((s, i) => {
            const host = (() => {
              try {
                return new URL(s.url).hostname.replace(/^www\./, "");
              } catch {
                return s.url;
              }
            })();
            return (
              <a
                key={i}
                href={s.url}
                target="_blank"
                rel="noopener noreferrer"
                className="block px-3 py-2 hover:bg-surface2 transition-colors border-b border-border/50 last:border-b-0 group"
                title={s.url}
              >
                <div className="flex items-center gap-1.5">
                  <span className="text-[10px] text-muted-foreground/60 font-mono shrink-0">
                    {i + 1}.
                  </span>
                  <span className="text-[11px] text-accent font-medium truncate flex-1">
                    {s.name || host}
                  </span>
                  <svg
                    width="9"
                    height="9"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    className="text-muted-foreground/60 opacity-0 group-hover:opacity-100 transition-opacity shrink-0"
                  >
                    <path d="M7 7h10v10" />
                    <path d="M7 17 17 7" />
                  </svg>
                </div>
                <div className="text-[9px] text-muted-foreground/50 ml-4 truncate">{host}</div>
                {s.snippet && (
                  <div className="text-[10px] text-muted-foreground/70 mt-1 ml-4 line-clamp-2">
                    {s.snippet}
                  </div>
                )}
              </a>
            );
          })}
        </div>
      )}
    </div>
  );
}
