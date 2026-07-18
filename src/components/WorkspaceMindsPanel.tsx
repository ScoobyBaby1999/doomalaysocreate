/**
 * WorkspaceMindsPanel — per-workspace agent configuration panel.
 *
 * Wraps the existing ConsciousScreen (the multi-agent constellation UI) with
 * a back button so it can be opened from a workspace card. Each workspace
 * has its own "minds" config (orchestrator + sub-agents running their own
 * virtual PC harness). Multiple workspaces = multiple agents running
 * simultaneously through the orchestrator (agent_sessions.py on the backend).
 *
 * The back button is dynamic — it shows whenever the panel is open, and
 * takes the user back to the workspace grid.
 */
import { ConsciousScreen } from "../screens/ConsciousScreen";
import type { Settings } from "../api/panel";
import type { Workspace } from "../api/github";

interface Props {
  open: boolean;
  workspace: Workspace | null;
  settings: Settings;
  onBack: () => void;
}

export function WorkspaceMindsPanel({ open, workspace, settings, onBack }: Props) {
  if (!open || !workspace) return null;

  return (
    <div className="flex flex-col h-full">
      {/* header with dynamic back button */}
      <div className="flex items-center gap-2 px-3 h-11 border-b border-border bg-surface/60 backdrop-blur shrink-0">
        <button
          onClick={onBack}
          aria-label="Back to workspaces"
          className="touch-target h-9 px-3 rounded-lg bg-surface2 border border-border text-text hover:border-accent/60 flex items-center gap-1 text-[12px]"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/>
          </svg>
          Workspaces
        </button>
        <div className="flex items-center gap-1.5 min-w-0 flex-1">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#a855f7" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0">
            <path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/>
            <path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/>
          </svg>
          <span className="text-[12px] font-semibold text-text truncate">
            Minds · {workspace.title}
          </span>
        </div>
        <span className="text-[10px] text-muted truncate">
          {workspace.source_repo ? workspace.source_repo.replace("https://github.com/", "") : "no source"}
        </span>
      </div>

      {/* constellation body — reuses existing multi-agent UI */}
      <div className="flex-1 min-h-0 overflow-hidden">
        <ConsciousScreen settings={settings} workspaceId={workspace.id} />
      </div>
    </div>
  );
}
