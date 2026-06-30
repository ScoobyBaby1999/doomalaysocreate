import type { ChatSession } from "../api/agent";

export function SessionSidebar({
  open,
  sessions,
  activeId,
  onSelect,
  onDelete,
  onNew,
  onClose,
}: {
  open: boolean;
  sessions: ChatSession[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onNew: () => void;
  onClose: () => void;
}) {
  return (
    <>
      {open && (
        <div
          className="fixed inset-0 z-40 bg-black/30"
          onClick={onClose}
        />
      )}
      <div
        className={`fixed top-0 left-0 z-50 h-full w-72 bg-surface border-r border-border transform transition-transform duration-200 ${
          open ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <div className="flex items-center justify-between px-3 h-9 border-b border-border">
          <span className="text-[11px] font-medium text-accent">Sessions</span>
          <button
            onClick={onNew}
            className="text-[11px] px-2 py-0.5 rounded border border-border text-muted hover:border-accent hover:text-accent"
          >
            + New
          </button>
        </div>
        <div className="overflow-y-auto h-[calc(100%-36px)]">
          {sessions.length === 0 ? (
            <div className="p-4 text-[11px] text-muted text-center">No saved sessions</div>
          ) : (
            sessions.map((s) => (
              <div
                key={s.id}
                className={`flex items-center gap-1 px-3 py-2 text-[12px] cursor-pointer hover:bg-surface2 group ${
                  s.id === activeId ? "bg-surface2/80 text-accent" : "text-muted"
                }`}
                onClick={() => onSelect(s.id)}
              >
                <span className="truncate flex-1">{s.title}</span>
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    onDelete(s.id);
                  }}
                  className="text-[10px] opacity-0 group-hover:opacity-100 hover:text-rose-400 shrink-0"
                  title="Delete"
                >
                  ✕
                </button>
              </div>
            ))
          )}
        </div>
      </div>
    </>
  );
}
