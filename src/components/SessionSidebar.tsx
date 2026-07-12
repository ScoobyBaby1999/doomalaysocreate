import { useState, useEffect, useRef } from "react";
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
}: SessionSidebarProps) {
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");
  const editInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editingId && editInputRef.current) {
      editInputRef.current.focus();
      editInputRef.current.select();
    }
  }, [editingId]);

  const filteredSessions = searchQuery
    ? sessions.filter((s) =>
        (s.title || "Untitled").toLowerCase().includes(searchQuery.toLowerCase()),
      )
    : sessions;

  const handleDelete = (id: string) => {
    setDeletingId(id);
    // Optimistic delete is handled by the store — we just show the fade-out
    // animation here, then trigger the actual delete.
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
        className={`fixed top-0 left-0 z-50 h-full w-[300px] bg-surface border-r border-border shadow-2xl transform transition-transform duration-200 ease-out flex flex-col ${
          open ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-4 h-14 border-b border-border shrink-0">
          <h2 className="text-[13px] font-semibold text-foreground tracking-wide uppercase">
            Chats
          </h2>
          <div className="flex items-center gap-1.5">
            <button
              onClick={onNew}
              className="flex items-center gap-1 text-[11.5px] px-2.5 py-1.5 rounded-md bg-accent text-white hover:bg-accent/90 transition-colors font-medium"
            >
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <line x1="12" y1="5" x2="12" y2="19" />
                <line x1="5" y1="12" x2="19" y2="12" />
              </svg>
              New
            </button>
            <button
              onClick={onClose}
              className="text-muted-foreground hover:text-foreground p-1.5 rounded-md hover:bg-surface2 transition-colors"
              title="Close"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <line x1="18" y1="6" x2="6" y2="18" />
                <line x1="6" y1="6" x2="18" y2="18" />
              </svg>
            </button>
          </div>
        </div>

        {/* Search */}
        {sessions.length > 5 && (
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
                className="w-full pl-8 pr-3 py-1.5 text-[12px] bg-surface2 rounded-md border border-border focus:border-accent focus:outline-none placeholder:text-muted-foreground/60"
              />
            </div>
          </div>
        )}

        {/* Session list */}
        <div className="flex-1 overflow-y-auto py-1.5">
          {isLoading ? (
            <div className="flex items-center justify-center py-10">
              <div className="flex items-center gap-2 text-muted-foreground text-[11.5px]">
                <div className="w-3 h-3 border-2 border-accent border-t-transparent rounded-full animate-spin" />
                Loading chats…
              </div>
            </div>
          ) : filteredSessions.length === 0 ? (
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
            filteredSessions.map((session) => {
              const isActive = session.id === activeId;
              const isDeleting = session.id === deletingId;
              const isEditing = session.id === editingId;
              const date = session.updated_at
                ? formatRelative(session.updated_at)
                : "";

              return (
                <div
                  key={session.id}
                  className={`group flex items-center gap-2 px-2.5 py-2 mx-2 rounded-lg cursor-pointer transition-all duration-150 ${
                    isActive
                      ? "bg-accent/10 border border-accent/30"
                      : "hover:bg-surface2 border border-transparent"
                  } ${isDeleting ? "opacity-40 scale-95" : ""}`}
                  onClick={() => !isDeleting && !isEditing && onSelect(session.id)}
                  onDoubleClick={() => !isDeleting && startEdit(session)}
                >
                  {/* Chat icon */}
                  <div
                    className={`shrink-0 w-7 h-7 rounded-md flex items-center justify-center transition-colors ${
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
                        onChange={(e) => setEditValue(e.target.value)}
                        onClick={(e) => e.stopPropagation()}
                        onBlur={commitEdit}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") commitEdit();
                          if (e.key === "Escape") cancelEdit();
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
                        </div>
                        <div className="flex items-center gap-2 mt-0.5">
                          <span className="text-[10px] text-muted-foreground">
                            {date}
                          </span>
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
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          startEdit(session);
                        }}
                        className={`p-1 rounded-md transition-all hover:bg-surface2 hover:text-foreground ${
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
                          handleDelete(session.id);
                        }}
                        className={`p-1 rounded-md transition-all hover:bg-red-500/15 hover:text-red-300 ${
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
