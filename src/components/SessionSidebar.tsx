import { useState } from "react";
import type { ChatSession } from "../api/agent";

interface SessionSidebarProps {
  open: boolean;
  sessions: ChatSession[];
  activeId: string | null;
  isLoading: boolean;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
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
  onNew,
  onClose,
}: SessionSidebarProps) {
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");

  const filteredSessions = searchQuery
    ? sessions.filter((s) =>
        s.title.toLowerCase().includes(searchQuery.toLowerCase())
      )
    : sessions;

  const handleDelete = (id: string) => {
    setDeletingId(id);
    // Small delay to show the deleting state
    setTimeout(() => {
      onDelete(id);
      setDeletingId(null);
    }, 200);
  };

  return (
    <>
      {/* Backdrop */}
      {open && (
        <div
          className="fixed inset-0 z-40 bg-black/40 backdrop-blur-sm transition-opacity"
          onClick={onClose}
        />
      )}

      {/* Sidebar */}
      <div
        className={`fixed top-0 left-0 z-50 h-full w-80 bg-card border-r border-border shadow-xl transform transition-transform duration-200 ease-out flex flex-col ${
          open ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-4 h-14 border-b border-border shrink-0">
          <h2 className="text-sm font-semibold text-foreground">Chat Sessions</h2>
          <div className="flex items-center gap-2">
            <button
              onClick={onNew}
              className="flex items-center gap-1 text-xs px-3 py-1.5 rounded-md bg-accent text-accent-foreground hover:bg-accent/90 transition-colors font-medium"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <line x1="12" y1="5" x2="12" y2="19" />
                <line x1="5" y1="12" x2="19" y2="12" />
              </svg>
              New
            </button>
            <button
              onClick={onClose}
              className="text-muted-foreground hover:text-foreground p-1 rounded-md transition-colors"
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <line x1="18" y1="6" x2="6" y2="18" />
                <line x1="6" y1="6" x2="18" y2="18" />
              </svg>
            </button>
          </div>
        </div>

        {/* Search */}
        {sessions.length > 5 && (
          <div className="px-4 py-2 border-b border-border shrink-0">
            <div className="relative">
              <svg
                className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground"
                width="14"
                height="14"
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
                placeholder="Search sessions..."
                className="w-full pl-8 pr-3 py-1.5 text-xs bg-muted rounded-md border border-border focus:border-accent focus:outline-none"
              />
            </div>
          </div>
        )}

        {/* Session list */}
        <div className="flex-1 overflow-y-auto">
          {isLoading ? (
            <div className="flex items-center justify-center py-8">
              <div className="flex items-center gap-2 text-muted-foreground text-xs">
                <div className="w-3 h-3 border-2 border-accent border-t-transparent rounded-full animate-spin" />
                Loading sessions...
              </div>
            </div>
          ) : filteredSessions.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 px-4 text-center">
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
              <p className="text-xs text-muted-foreground">
                {searchQuery
                  ? "No sessions match your search"
                  : "No chat sessions yet"}
              </p>
              {!searchQuery && (
                <button
                  onClick={onNew}
                  className="mt-3 text-xs text-accent hover:underline"
                >
                  Start your first chat
                </button>
              )}
            </div>
          ) : (
            <div className="py-1">
              {filteredSessions.map((session) => {
                const isActive = session.id === activeId;
                const isDeleting = session.id === deletingId;
                const date = session.updated_at
                  ? new Date(session.updated_at).toLocaleDateString(undefined, {
                      month: "short",
                      day: "numeric",
                    })
                  : "";

                return (
                  <div
                    key={session.id}
                    className={`group flex items-center gap-2 px-3 py-2.5 mx-2 rounded-lg cursor-pointer transition-all duration-150 ${
                      isActive
                        ? "bg-accent/10 border border-accent/30"
                        : "hover:bg-muted border border-transparent"
                    } ${isDeleting ? "opacity-40" : ""}`}
                    onClick={() => !isDeleting && onSelect(session.id)}
                  >
                    {/* Chat icon */}
                    <div
                      className={`shrink-0 w-7 h-7 rounded-md flex items-center justify-center ${
                        isActive ? "bg-accent/20" : "bg-muted"
                      }`}
                    >
                      <svg
                        width="14"
                        height="14"
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
                      <div className="flex items-center gap-2">
                        <span
                          className={`text-[13px] truncate font-medium ${
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
                          <span className="text-[10px] text-muted-foreground/60 truncate max-w-[100px]">
                            {session.model.split("/").pop() || session.model}
                          </span>
                        )}
                      </div>
                    </div>

                    {/* Delete button */}
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        handleDelete(session.id);
                      }}
                      className={`shrink-0 p-1 rounded-md transition-all ${
                        isActive
                          ? "text-muted-foreground hover:text-destructive hover:bg-destructive/10 opacity-100"
                          : "text-muted-foreground hover:text-destructive hover:bg-destructive/10 opacity-0 group-hover:opacity-100"
                      }`}
                      title="Delete session"
                    >
                      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <polyline points="3 6 5 6 21 6" />
                        <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                      </svg>
                    </button>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* Footer count */}
        {sessions.length > 0 && (
          <div className="px-4 py-2 border-t border-border shrink-0">
            <p className="text-[10px] text-muted-foreground text-center">
              {sessions.length} session{sessions.length !== 1 ? "s" : ""}
            </p>
          </div>
        )}
      </div>
    </>
  );
}
