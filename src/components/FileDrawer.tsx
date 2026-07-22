import type { AgentFile } from "../api/agent";
import { AgentClient } from "../api/agent";
import type { Settings } from "../api/panel";

export function FileDrawer({
  open,
  onClose,
  files,
  sessionId,
  settings,
  onRefresh,
}: {
  open: boolean;
  onClose: () => void;
  files: AgentFile[];
  sessionId: string | null;
  settings: Settings;
  onRefresh?: () => void;
}) {
  const client = new AgentClient(settings);

  return (
    <>
      {open && (
        <div
          className="fixed inset-0 z-[60] bg-black/30 backdrop-blur-sm"
          onClick={onClose}
        />
      )}
      <div
        className={`fixed top-0 right-0 z-[70] h-full w-[320px] max-w-[80vw] bg-bg border-l border-border shadow-2xl transition-transform duration-300 ${
          open ? "translate-x-0" : "translate-x-full"
        }`}
      >
        <div className="flex items-center justify-between px-3 h-10 border-b border-border">
          <span className="text-sm font-medium">Files</span>
          <div className="flex items-center gap-1">
            {onRefresh && (
              <button onClick={onRefresh} className="text-muted hover:text-accent text-sm px-1" title="Refresh file list">
                ↻
              </button>
            )}
            <button onClick={onClose} className="text-muted hover:text-accent text-sm px-1">
              ✕
            </button>
          </div>
        </div>
        <div className="overflow-y-auto h-[calc(100%-40px)]">
          {files.length === 0 ? (
            <div className="text-center text-muted text-[13px] mt-8 px-4">
              No files yet. The agent creates files in its workspace as it works.
            </div>
          ) : (
            <div className="p-2 space-y-1">
              {files.map((f) => (
                <div
                  key={f.path}
                  className="flex items-center gap-2 px-3 py-2 rounded-xl hover:bg-surface transition-colors"
                >
                  <span className="text-[11px] text-muted font-mono flex-1 truncate">
                    {f.path}
                  </span>
                  <span className="text-[10px] text-muted shrink-0">
                    {fmtSize(f.size)}
                  </span>
                  <button
                    onClick={() => {
                      if (sessionId) {
                        client.download(sessionId, f.path).catch(() => {});
                      }
                    }}
                    className="text-[11px] px-2 py-0.5 rounded border border-border hover:border-accent shrink-0"
                    disabled={!sessionId}
                  >
                    ↓
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </>
  );
}

function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}
