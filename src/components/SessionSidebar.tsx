import { useState, useEffect, useRef, useMemo } from "react";
import type { ChatSession } from "../api/agent";

interface SessionSidebarProps {
  open: boolean;
  sessions: ChatSession[];
  activeId: string | null;
  isLoading: boolean;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onRename: (id: string, title: string) => void;
  onNew: () => void;
  onClose: () => void;
  /** Pinned session IDs — rendered in their own group at the top. */
  pinnedIds?: string[];
  /** Toggle the pinned state of a session. */
  onTogglePin?: (id: string) => void;
}

/** A date-bucket key for grouping conversations. */
type Bucket = "pinned" | "today" | "yesterday" | "week" | "earlier";

const BUCKET_ORDER: Bucket[] = ["pinned", "today", "yesterday", "week", "earlier"];
const BUCKET_LABEL: Record<Bucket, string> = {
  pinned: "Pinned",
  today: "Today",
  yesterday: "Yesterday",
  week: "This Week",
  earlier: "Earlier",
};

function bucketFor(session: ChatSession, pinned: boolean): Bucket {
  if (pinned) return "pinned";
  try {
    const d = new Date(session.updated_at || session.created_at);
    const now = new Date();
    const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    const ts = d.getTime();
    if (ts >= startOfToday) return "today";
    if (ts >= startOfToday - 86400000) return "yesterday";
    // This week = within the last 7 days.
    if (ts >= startOfToday - 6 * 86400000) return "week";
    return "earlier";
  } catch {
    return "earlier";
  }
}

export function SessionSidebar({
  open,
  sessions,
  activeId,
  isLoading,
  onSelect,
  onDelete,
  onRename,
  onNew,
  onClose,
  pinnedIds = [],
  onTogglePin,
}: SessionSidebarProps) {
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");
  const [collapsed, setCollapsed] = useState<Record<Bucket, boolean>>({
    pinned: false,
    today: false,
    yesterday: false,
    week: false,
    earlier: false,
  });
  const editInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editingId && editInputRef.current) {
      editInputRef.current.focus();
      editInputRef.current.select();
    }
  }, [editingId]);

  // Filter + bucket the sessions. Pinned sessions always show first (in their
  // own group) regardless of date.
  const grouped = useMemo(() => {
    const pinnedSet = new Set(pinnedIds);
    const filtered = searchQuery
      ? sessions.filter((s) =>
          (s.title || "Untitled").toLowerCase().includes(searchQuery.toLowerCase()),
        )
      : sessions;
    const map: Record<Bucket, ChatSession[]> = {
      pinned: [],
      today: [],
      yesterday: [],
      week: [],
      earlier: [],
    };
    for (const s of filtered) {
      map[bucketFor(s, pinnedSet.has(s.id))].push(s);
    }
    // Sort each bucket by updated_at desc.
    for (const k of BUCKET_ORDER) {
      map[k].sort((a, b) => {
        const ta = new Date(a.updated_at || a.created_at).getTime();
        const tb = new Date(b.updated_at || b.created_at).getTime();
        return tb - ta;
      });
    }
    return map;
  }, [sessions, searchQuery, pinnedIds]);

  const handleDelete = (id: string) => {
    setDeletingId(id);
    setTimeout(() => {
      onDelete(id);
      setDeletingId(null);
    }, 180);
  };

  const startEdit = (session: ChatSession) => {
    setEditingId(session.id);
    setEditValue(session.title || "");
  };

  const commitEdit = () => {
    if (editingId && editValue.trim()) {
      onRename(editingId, editValue.trim());
    }
    setEditingId(null);
    setEditValue("");
  };

  const cancelEdit = () => {
    setEditingId(null);
    setEditValue("");
  };

  const toggleBucket = (b: Bucket) =>
    setCollapsed((c) => ({ ...c, [b]: !c[b] }));

  return (
    <>
      {/* Backdrop */}
      {open && (
        <div
          className="fixed inset-0 z-40 bg-black/50 backdrop-blur-sm transition-opacity"
          onClick={onClose}
        />
      )}

      {/* Sidebar */}
      <div
        className={`fixed top-0 left-0 z-50 h-full w-[320px] bg-surface border-r border-border shadow-2xl transform transition-transform duration-200 ease-out flex flex-col ${
          open ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-4 h-14 border-b border-border shrink-0">
          <h2 className="text-[13px] font-semibold text-foreground tracking-wide uppercase">
            Workspace
          </h2>
          <div className="flex items-center gap-1.5">
            <button
              onClick={onNew}
              className="flex items-center gap-1 text-[11.5px] px-2.5 py-1.5 rounded-xl bg-accent text-white hover:bg-accent/90 transition-colors font-medium"
            >
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <line x1="12" y1="5" x2="12" y2="19" />
                <line x1="5" y1="12" x2="19" y2="12" />
              </svg>
              New
            </button>
            <button
              onClick={onClose}
              className="text-muted-foreground hover:text-foreground p-1.5 rounded-xl hover:bg-surface2 transition-colors"
              title="Close"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <line x1="18" y1="6" x2="6" y2="18" />
                <line x1="6" y1="6" x2="18" y2="18" />
              </svg>
            </button>
          </div>
        </div>

        {/* Search — always visible now (was hidden when sessions.length <= 5) */}
        <div className="px-3 py-2 border-b border-border shrink-0">
          <div className="relative">
            <svg
              className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground/70"
              width="13"
              height="13"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <circle cx="11" cy="11" r="8" />
              <line x1="21" y1="21" x2="16.65" y2="16.65" />
            </svg>
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search chats…"
              className="w-full pl-8 pr-3 py-1.5 text-[12px] bg-surface2 rounded-xl border border-border focus:border-accent focus:outline-none placeholder:text-muted-foreground/60"
            />
          </div>
        </div>

        {/* Session list — grouped by date bucket */}
        <div className="flex-1 overflow-y-auto py-1.5">
          {isLoading ? (
            <div className="flex items-center justify-center py-10">
              <div className="flex items-center gap-2 text-muted-foreground text-[11.5px]">
                <div className="w-3 h-3 border-2 border-accent border-t-transparent rounded-full animate-spin" />
                Loading chats…
              </div>
            </div>
          ) : BUCKET_ORDER.every((b) => grouped[b].length === 0) ? (
            <div className="flex flex-col items-center justify-center py-14 px-4 text-center">
              <svg
                className="text-muted-foreground/40 mb-3"
                width="32"
                height="32"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
              </svg>
              <p className="text-[11.5px] text-muted-foreground mb-3">
                {searchQuery ? "No chats match your search" : "No chats yet"}
              </p>
              {!searchQuery && (
                <button
                  onClick={onNew}
                  className="text-[11.5px] text-accent hover:underline"
                >
                  Start your first chat
                </button>
              )}
            </div>
          ) : (
            BUCKET_ORDER.map((b) => {
              const items = grouped[b];
              if (items.length === 0) return null;
              const isCollapsed = collapsed[b];
              return (
                <div key={b} className="mb-1">
                  <button
                    onClick={() => toggleBucket(b)}
                    className="w-full flex items-center gap-1.5 px-3 py-1 text-[9.5px] uppercase tracking-wide text-muted-foreground/60 hover:text-muted-foreground transition-colors"
                  >
                    <svg
                      width="8"
                      height="8"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="3"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      className={`transition-transform duration-150 ${isCollapsed ? "" : "rotate-90"}`}
                    >
                      <polyline points="9 18 15 12 9 6" />
                    </svg>
                    <span>{BUCKET_LABEL[b]}</span>
                    <span className="opacity-50">· {items.length}</span>
                  </button>
                  {!isCollapsed && (
                    <div className="pl-1.5">
                      {items.map((session) => (
                        <SessionRow
                          key={session.id}
                          session={session}
                          isActive={session.id === activeId}
                          isDeleting={session.id === deletingId}
                          isEditing={session.id === editingId}
                          isPinned={pinnedIds.includes(session.id)}
                          editValue={editValue}
                          editInputRef={editInputRef}
                          onSelect={onSelect}
                          onDelete={handleDelete}
                          onRename={startEdit}
                          onCommitEdit={commitEdit}
                          onCancelEdit={cancelEdit}
                          onEditValueChange={setEditValue}
                          onTogglePin={onTogglePin}
                        />
                      ))}
                    </div>
                  )}
                </div>
              );
            })
          )}
        </div>

        {/* Footer count */}
        {sessions.length > 0 && (
          <div className="px-4 py-2 border-t border-border shrink-0">
            <p className="text-[10px] text-muted-foreground text-center">
              {sessions.length} chat{sessions.length !== 1 ? "s" : ""} · double-click to rename
            </p>
          </div>
        )}
      </div>
    </>
  );
}

/** One row in the WorkspaceBrowser list. */
function SessionRow({
  session,
  isActive,
  isDeleting,
  isEditing,
  isPinned,
  editValue,
  editInputRef,
  onSelect,
  onDelete,
  onRename,
  onCommitEdit,
  onCancelEdit,
  onEditValueChange,
  onTogglePin,
}: {
  session: ChatSession;
  isActive: boolean;
  isDeleting: boolean;
  isEditing: boolean;
  isPinned: boolean;
  editValue: string;
  editInputRef: React.RefObject<HTMLInputElement>;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onRename: (session: ChatSession) => void;
  onCommitEdit: () => void;
  onCancelEdit: () => void;
  onEditValueChange: (v: string) => void;
  onTogglePin?: (id: string) => void;
}) {
  const date = session.updated_at ? formatRelative(session.updated_at) : "";
  const count = typeof session.message_count === "number" ? session.message_count : null;
  return (
    <div
      className={`group flex items-center gap-2 px-2.5 py-2 mx-2 rounded-lg cursor-pointer transition-all duration-150 ${
        isActive
          ? "bg-accent/10 border border-accent/30"
          : "hover:bg-surface2 border border-transparent"
      } ${isDeleting ? "opacity-40 scale-95" : ""}`}
      onClick={() => !isDeleting && !isEditing && onSelect(session.id)}
      onDoubleClick={() => !isDeleting && onRename(session)}
    >
      {/* Chat icon */}
      <div
        className={`shrink-0 w-7 h-7 rounded-xl flex items-center justify-center transition-colors ${
          isActive ? "bg-accent/20" : "bg-surface2"
        }`}
      >
        <svg
          width="13"
          height="13"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          className={isActive ? "text-accent" : "text-muted-foreground"}
        >
          <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
        </svg>
      </div>

      {/* Content */}
      <div className="flex-1 min-w-0">
        {isEditing ? (
          <input
            ref={editInputRef}
            value={editValue}
            onChange={(e) => onEditValueChange(e.target.value)}
            onClick={(e) => e.stopPropagation()}
            onBlur={onCommitEdit}
            onKeyDown={(e) => {
              if (e.key === "Enter") onCommitEdit();
              if (e.key === "Escape") onCancelEdit();
            }}
            className="w-full text-[12.5px] bg-surface2 border border-accent rounded px-1.5 py-0.5 outline-none text-foreground"
          />
        ) : (
          <>
            <div className="flex items-center gap-2">
              <span
                className={`text-[12.5px] truncate font-medium ${
                  isActive ? "text-accent" : "text-foreground"
                }`}
              >
                {session.title || "Untitled Chat"}
              </span>
              {isPinned && (
                <svg
                  width="9"
                  height="9"
                  viewBox="0 0 24 24"
                  fill="currentColor"
                  className="text-amber-400/80 shrink-0"
                  aria-label="Pinned"
                >
                  <path d="M12 2l3 7h7l-5.5 4 2 7L12 16l-6.5 4 2-7L2 9h7z" />
                </svg>
              )}
              {count != null && count > 0 && (
                <span
                  className="text-[9px] text-muted-foreground/70 bg-surface2 px-1.5 py-0.5 rounded-full shrink-0 tabular-nums"
                  title={`${count} message${count !== 1 ? "s" : ""}`}
                >
                  {count}
                </span>
              )}
            </div>
            <div className="flex items-center gap-2 mt-0.5">
              <span className="text-[10px] text-muted-foreground">{date}</span>
              {session.model && (
                <span className="text-[10px] text-muted-foreground/60 truncate max-w-[110px]">
                  {session.model.split("/").pop() || session.model}
                </span>
              )}
            </div>
          </>
        )}
      </div>

      {/* Action buttons (visible on hover or active) */}
      {!isEditing && (
        <div className="flex items-center gap-0.5 shrink-0">
          {onTogglePin && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                onTogglePin(session.id);
              }}
              className={`p-1 rounded-xl transition-all hover:bg-surface2 hover:text-foreground ${
                isPinned ? "text-amber-400/80" : "text-muted-foreground opacity-0 group-hover:opacity-100"
              } ${isActive ? "opacity-100" : ""}`}
              title={isPinned ? "Unpin" : "Pin to top"}
            >
              <svg width="11" height="11" viewBox="0 0 24 24" fill={isPinned ? "currentColor" : "none"} stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 2l3 7h7l-5.5 4 2 7L12 16l-6.5 4 2-7L2 9h7z" />
              </svg>
            </button>
          )}
          <button
            onClick={(e) => {
              e.stopPropagation();
              onRename(session);
            }}
            className={`p-1 rounded-xl transition-all hover:bg-surface2 hover:text-foreground ${
              isActive
                ? "text-muted-foreground opacity-100"
                : "text-muted-foreground opacity-0 group-hover:opacity-100"
            }`}
            title="Rename"
          >
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 20h9" /><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
            </svg>
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation();
              onDelete(session.id);
            }}
            className={`p-1 rounded-xl transition-all hover:bg-red-500/15 hover:text-red-300 ${
              isActive
                ? "text-muted-foreground opacity-100"
                : "text-muted-foreground opacity-0 group-hover:opacity-100"
            }`}
            title="Delete"
          >
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="3 6 5 6 21 6" />
              <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
            </svg>
          </button>
        </div>
      )}
    </div>
  );
}

/** Alias for callers that think of it as a "workspace browser". */
export const WorkspaceBrowser = SessionSidebar;

function formatRelative(iso: string): string {
  try {
    const d = new Date(iso);
    const now = new Date();
    const diffMs = now.getTime() - d.getTime();
    const diffMin = Math.floor(diffMs / 60000);
    const diffHr = Math.floor(diffMin / 60);
    const diffDay = Math.floor(diffHr / 24);

    if (diffMin < 1) return "just now";
    if (diffMin < 60) return `${diffMin}m ago`;
    if (diffHr < 24) return `${diffHr}h ago`;
    if (diffDay < 7) return `${diffDay}d ago`;
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  } catch {
    return "";
  }
}
