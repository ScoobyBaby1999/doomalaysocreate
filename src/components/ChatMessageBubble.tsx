import { useState } from "react";
import { Markdown } from "./Markdown";
import { DiffView } from "./DiffView";
import type { ChatMessage } from "../state/chatStore";

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
  "Wrench";
  return "Wrench";
}

function isDiff(text: string): boolean {
  return text.includes("---") && text.includes("+++") && /^diff --git/.test(text.trim());
}

interface ChatMessageBubbleProps {
  message: ChatMessage;
}

export function ChatMessageBubble({ message }: ChatMessageBubbleProps) {
  // User message — right-aligned bubble
  if (message.role === "user") {
    return (
      <div className="flex justify-end px-4 py-2">
        <div className="max-w-[85%] rounded-2xl rounded-br-md bg-accent/10 px-4 py-2.5 text-[15px] leading-relaxed text-foreground">
          {message.content}
        </div>
      </div>
    );
  }

  // Assistant message — left-aligned with markdown
  if (message.role === "assistant") {
    return (
      <div className="flex justify-start px-4 py-2">
        <div className="max-w-[90%] text-[15px] leading-relaxed text-foreground">
          <Markdown text={message.content} />
          {message.isStreaming && (
            <span className="inline-block w-1.5 h-4 ml-0.5 bg-accent animate-pulse align-middle" />
          )}
        </div>
      </div>
    );
  }

  // Thinking message — collapsible
  if (message.role === "thinking") {
    return (
      <div className="px-4 py-1">
        <Collapsible label="Thinking">
          <div className="text-[13px] text-muted-foreground leading-relaxed">
            <Markdown text={message.content} />
            {message.isStreaming && (
              <span className="inline-block w-1.5 h-3 ml-0.5 bg-muted-foreground animate-pulse align-middle" />
            )}
          </div>
        </Collapsible>
      </div>
    );
  }

  // Tool use — compact indicator
  if (message.role === "tool") {
    if (message.toolName === "result") {
      // Tool result
      const isDiffContent = !message.isError && isDiff(message.content);
      if (isDiffContent) {
        return (
          <div className="px-4 py-1">
            <div className="rounded-lg border border-border overflow-hidden max-w-2xl">
              <div className="px-3 py-1.5 text-[11px] text-muted-foreground border-b border-border bg-muted/30 font-medium">
                Diff
              </div>
              <DiffView diff={message.content} />
            </div>
          </div>
        );
      }
      return (
        <div className="px-4 py-1">
          <Collapsible label={message.isError ? "Result (error)" : "Result"} error={message.isError}>
            <div className="text-[13px] overflow-x-auto">
              <Markdown text={message.content} />
            </div>
          </Collapsible>
        </div>
      );
    }

    // Tool use indicator
    return (
      <div className="px-4 py-1">
        <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
          <span className="flex items-center gap-1.5">
            <ToolIcon name={message.toolName || "tool"} />
            <span className="font-medium text-accent">{message.toolName}</span>
          </span>
          {message.toolSummary ? (
            <span className="font-mono text-[11px] truncate max-w-[300px] opacity-60">
              {message.toolSummary}
            </span>
          ) : null}
        </div>
      </div>
    );
  }

  // Status message
  if (message.role === "status") {
    if (message.isError) {
      return (
        <div className="px-4 py-2">
          <div className="max-w-2xl mx-auto rounded-xl border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
            {message.content}
          </div>
        </div>
      );
    }
    return (
      <div className="px-4 py-1 text-center">
        <span className="text-[11px] text-muted-foreground italic">
          {message.content}
        </span>
      </div>
    );
  }

  // Panel message
  if (message.role === "panel") {
    return (
      <div className="px-4 py-2">
        <div className="max-w-2xl mx-auto rounded-xl border border-border bg-muted/20 overflow-hidden">
          <div className="flex items-center gap-2 px-3 py-2 border-b border-border bg-muted/30">
            <span
              className={`h-2 w-2 rounded-full ${
                message.panelStatus === "starting"
                  ? "bg-muted-foreground"
                  : message.panelStatus === "running"
                  ? "bg-accent animate-pulse"
                  : "bg-emerald-400"
              }`}
            />
            <span className="text-sm font-medium">Panel: {message.content}</span>
            <span className="ml-auto text-[11px] text-muted-foreground capitalize">
              {message.panelStatus}
            </span>
          </div>
        </div>
      </div>
    );
  }

  return null;
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function Collapsible({
  label,
  children,
  error,
}: {
  label: string;
  children: React.ReactNode;
  error?: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div
      className={`rounded-lg border ${
        error ? "border-destructive/30" : "border-border"
      } max-w-2xl`}
    >
      <button
        onClick={() => setOpen((o) => !o)}
        className={`w-full text-left text-[11px] px-3 py-1.5 font-medium ${
          error ? "text-destructive" : "text-muted-foreground"
        }`}
      >
        <span className="inline-block w-3">
          {open ? "\u25BE" : "\u25B8"}
        </span>{" "}
        {label}
      </button>
      {open && <div className="px-3 pb-3 border-t border-border/50">{children}</div>}
    </div>
  );
}

function ToolIcon({ name }: { name: string }) {
  const iconName = toolIcon(name);
  // Simple SVG icons
  if (iconName === "Terminal") {
    return (
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <polyline points="4 17 10 11 4 5" /><line x1="12" y1="19" x2="20" y2="19" />
      </svg>
    );
  }
  if (iconName === "FileEdit") {
    return (
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 20h9" /><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
      </svg>
    );
  }
  if (iconName === "Eye") {
    return (
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" /><circle cx="12" cy="12" r="3" />
      </svg>
    );
  }
  if (iconName === "Search") {
    return (
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
      </svg>
    );
  }
  if (iconName === "Globe") {
    return (
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="10" /><line x1="2" y1="12" x2="22" y2="12" /><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
      </svg>
    );
  }
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
    </svg>
  );
}
