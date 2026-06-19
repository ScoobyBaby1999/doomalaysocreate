// Workspace screen: list workspaces, create new, view details with file browser
// and git operations.  The main GitHub integration UI.

import { useEffect, useMemo, useRef, useState } from "react";
import { GitHubClient, type Workspace, type Repo, type Branch, type PushLog, type RegistryEntry } from "../api/github";
import { GithubConnect } from "../components/GithubConnect";
import type { Settings } from "../api/panel";

type View = "list" | "create" | "detail" | "registry";

export function WorkspaceScreen({
  settings,
  onChange,
}: {
  settings: Settings;
  onChange: (s: Settings) => void;
}) {
  // Debounced logout guard: a single 401 used to wipe githubSessionId on the
  // spot, which logged the user out every time they opened the panel — even
  // right after connecting — because the FIRST request (listWorkspaces) would
  // race the JWT reconstruction on a freshly-restarted Space. Now we verify
  // with /api/auth/status before clearing. If status says still-authenticated,
  // the 401 was transient (DB warm-up, audience skew) and we keep the session.
  const clearingRef = useRef(false);
  const handleUnauthorized = () => {
    if (clearingRef.current) return;          // already verifying — don't stack
    clearingRef.current = true;
    const probe = new GitHubClient(settings);
    probe.status()
      .then((s) => {
        if (s.authenticated) {
          // Session is actually valid — the 401 was transient. Don't log out.
          return;
        }
        onChange({ ...settings, githubSessionId: "", githubUsername: "" });
        setView("list");
      })
      .catch(() => {
        // Status check itself failed — clear to be safe, but this is rare.
        onChange({ ...settings, githubSessionId: "", githubUsername: "" });
        setView("list");
      })
      .finally(() => { clearingRef.current = false; });
  };
  const client = useMemo(
    () => new GitHubClient(settings, handleUnauthorized),
    // handleUnauthorized is stable enough — it only closes over settings/onChange
    // which change together. Recreating the client on settings change is correct.
    [settings, onChange],
  );
  const [view, setView] = useState<View>("list");
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selected, setSelected] = useState<Workspace | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const refreshRef = useRef(0);

  const connected = !!settings.githubSessionId;

  function switchView(v: View) {
    setError("");
    setView(v);
  }

  // Load workspaces on mount
  useEffect(() => {
    if (!connected) {
      setLoading(false);
      return;
    }
    let alive = true;
    client
      .listWorkspaces()
      .then((r) => {
        if (alive) setWorkspaces(r.workspaces);
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connected]);

  function refresh() {
    setLoading(true);
    setError("");
    const reqId = ++refreshRef.current;
    client
      .listWorkspaces()
      .then((r) => {
        if (reqId === refreshRef.current) setWorkspaces(r.workspaces);
      })
      .catch((e) => {
        if (reqId === refreshRef.current) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (reqId === refreshRef.current) setLoading(false);
      });
  }

  function openDetail(ws: Workspace) {
    setSelected(ws);
    switchView("detail");
  }

  // --- Not connected view ---
  if (!connected) {
    return (
      <div className="flex flex-col items-center justify-center h-full p-6 text-center space-y-4">
        <p className="text-sm font-medium">Connect GitHub to start</p>
        <p className="text-sm text-muted max-w-xs">
          Link your GitHub account to clone repos, work on code, and push changes.
        </p>
        <GithubConnect
          settings={settings}
          onConnected={(id, username) => {
            onChange({ ...settings, githubSessionId: id, githubUsername: username });
          }}
        />
      </div>
    );
  }

  // --- List view ---
  if (view === "list") {
    return (
      <div className="flex flex-col h-full">
        <div className="flex items-center gap-2 px-3 h-9 border-b border-border text-[11px] text-muted">
          <GithubConnect
            settings={settings}
            onConnected={(id, username) => {
              onChange({ ...settings, githubSessionId: id, githubUsername: username });
            }}
          />
          <button
            onClick={() => switchView("create")}
            className="ml-auto px-2 py-1 rounded-lg border border-border hover:border-accent text-accent"
          >
            + New
          </button>
          <button onClick={() => switchView("registry")} className="underline">
            browse
          </button>
          <button onClick={refresh} className="underline">
            refresh
          </button>
          {settings.githubSessionId && (
            <button
              onClick={() => onChange({ ...settings, githubSessionId: "", githubUsername: "" })}
              className="px-2 py-1 rounded border border-rose-500/40 text-rose-300 hover:bg-rose-500/10"
              title="Clear session (fixes stale 401 errors)"
            >
              clear
            </button>
          )}
        </div>

        {error && (
          <div className="px-3 py-2 text-sm text-rose-300 border-b border-rose-500/40 bg-rose-500/10">
            {error}
          </div>
        )}

        <div className="flex-1 overflow-y-auto">
          {loading && workspaces.length === 0 ? (
            <div className="text-center text-muted text-sm mt-12">loading…</div>
          ) : workspaces.length === 0 ? (
            <div className="text-center text-muted text-sm mt-12 px-6 space-y-2">
              <p>No workspaces yet.</p>
              <p>Create one to clone a repo and start working on code with the agent.</p>
            </div>
          ) : (
            <div className="divide-y divide-border">
              {workspaces.map((ws) => (
                <button
                  key={ws.id}
                  onClick={() => openDetail(ws)}
                  className="w-full text-left px-3 py-3 hover:bg-surface transition-colors"
                >
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-text">{ws.title}</span>
                    {ws.visibility === "public" && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-accent/20 text-accent">
                        public
                      </span>
                    )}
                  </div>
                  {ws.source_repo && (
                    <div className="text-[11px] text-muted mt-0.5">
                      {ws.source_repo.replace("https://github.com/", "")} · {ws.current_branch}
                      {ws.source_branches && ws.source_branches.length > 1 && (
                        <span className="text-[10px] text-muted/60 ml-1">
                          (+{ws.source_branches.length - 1} more)
                        </span>
                      )}
                    </div>
                  )}
                  <div className="text-[10px] text-muted mt-0.5">
                    updated {new Date(ws.last_modified).toLocaleDateString()}
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    );
  }

  // --- Create view ---
  if (view === "create") {
    return (
      <CreateWorkspace
        client={client}
        onCreated={(ws) => {
          setWorkspaces((prev) => [ws, ...prev]);
          setSelected(ws);
          switchView("detail");
        }}
        onCancel={() => switchView("list")}
      />
    );
  }

  // --- Detail view ---
  if (view === "detail" && selected) {
    return (
      <WorkspaceDetail
        client={client}
        workspace={selected}
        onBack={() => switchView("list")}
        onUpdated={(ws) => {
          setSelected(ws);
          setWorkspaces((prev) => prev.map((w) => (w.id === ws.id ? ws : w)));
        }}
        onDeleted={(id) => {
          setWorkspaces((prev) => prev.filter((w) => w.id !== id));
          switchView("list");
        }}
      />
    );
  }

  // --- Registry view ---
  if (view === "registry") {
    return (
      <RegistryBrowser
        client={client}
        onBack={() => switchView("list")}
      />
    );
  }

  return null;
}

// --- Create Workspace Form -------------------------------------------------

function CreateWorkspace({
  client,
  onCreated,
  onCancel,
}: {
  client: GitHubClient;
  onCreated: (ws: Workspace) => void;
  onCancel: () => void;
}) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [repos, setRepos] = useState<Repo[]>([]);
  const [selectedRepo, setSelectedRepo] = useState<string>("");
  const [branches, setBranches] = useState<Branch[]>([]);
  const [selectedBranch, setSelectedBranch] = useState<string>("");
  const [selectedBranches, setSelectedBranches] = useState<Set<string>>(new Set());
  const [branchMode, setBranchMode] = useState<"single" | "all" | "select">("single");
  const [loadingRepos, setLoadingRepos] = useState(true);
  const [loadingBranches, setLoadingBranches] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const repoSelectRef = useRef(0);
  const titleEdited = useRef(false);
  const descriptionEdited = useRef(false);

  useEffect(() => {
    let alive = true;
    client
      .repos(1, 100)
      .then((r) => {
        if (alive) setRepos(r.repos);
      })
      .catch(() => {
        if (alive) setError("Failed to load repos");
      })
      .finally(() => {
        if (alive) setLoadingRepos(false);
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleRepoSelect(fullName: string) {
    setSelectedRepo(fullName);
    setSelectedBranch("");
    setSelectedBranches(new Set());
    if (!fullName) {
      setBranches([]);
      // Auto-clear title/description if they were auto-filled
      if (!titleEdited.current) setTitle("");
      if (!descriptionEdited.current) setDescription("");
      return;
    }
    setLoadingBranches(true);
    const reqId = ++repoSelectRef.current;
    try {
      const [owner, repo] = fullName.split("/");
      const branchesResult = await client.branches(owner, repo);
      if (reqId !== repoSelectRef.current) return;
      setBranches(branchesResult.branches);
      const def = branchesResult.branches.find((b) => b.name === "main") || branchesResult.branches[0];
      if (def) setSelectedBranch(def.name);
      // Auto-fill title/description if not manually edited
      const repoInfo = repos.find((r) => r.full_name === fullName);
      if (repoInfo && !titleEdited.current) {
        setTitle(repoInfo.name);
      }
      if (repoInfo && !descriptionEdited.current) {
        setDescription(repoInfo.description || "");
      }
    } catch (e) {
      if (reqId === repoSelectRef.current) setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (reqId === repoSelectRef.current) setLoadingBranches(false);
    }
  }

  async function handleCreate() {
    if (!title.trim()) {
      setError("Title is required");
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      const opts: Parameters<GitHubClient["createWorkspace"]>[0] = {
        title: title.trim(),
        description: description.trim(),
      };
      if (selectedRepo) {
        opts.source_repo = `https://github.com/${selectedRepo}.git`;
        if (branchMode === "single") {
          opts.source_branch = selectedBranch || undefined;
        } else if (branchMode === "select") {
          opts.source_branches = Array.from(selectedBranches);
          opts.source_branch = opts.source_branches[0] || undefined;
        }
        // branchMode === "all": omit source_branch — backend clones full repo
      }
      const ws = await client.createWorkspace(opts);
      onCreated(ws);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-2 px-3 h-9 border-b border-border text-[11px] text-muted">
        <button onClick={onCancel} className="underline">
          ← back
        </button>
        <span className="font-medium text-text">New Workspace</span>
      </div>

      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {error && (
          <div className="text-sm text-rose-300 bg-rose-500/10 rounded-lg px-3 py-2">
            {error}
          </div>
        )}

        <label className="block">
          <span className="text-[11px] text-muted">Title</span>
          <input
            value={title}
            onChange={(e) => { titleEdited.current = true; setTitle(e.target.value); }}
            placeholder="my-project"
            className="mt-1 w-full bg-surface border border-border rounded-lg px-3 py-2 text-sm outline-none focus:border-accent"
          />
        </label>

        <label className="block">
          <span className="text-[11px] text-muted">Description (optional)</span>
          <input
            value={description}
            onChange={(e) => { descriptionEdited.current = true; setDescription(e.target.value); }}
            placeholder="What this workspace is for"
            className="mt-1 w-full bg-surface border border-border rounded-lg px-3 py-2 text-sm outline-none focus:border-accent"
          />
        </label>

        <label className="block">
          <span className="text-[11px] text-muted">Clone from repo (optional)</span>
          <select
            value={selectedRepo}
            onChange={(e) => handleRepoSelect(e.target.value)}
            disabled={loadingRepos}
            className="mt-1 w-full bg-surface border border-border rounded-lg px-3 py-2 text-sm outline-none focus:border-accent"
          >
            <option value="">{loadingRepos ? "loading repos…" : "— none (empty workspace) —"}</option>
            {repos.map((r) => (
              <option key={r.full_name} value={r.full_name}>
                {r.full_name} {r.private ? "🔒" : ""}
              </option>
            ))}
          </select>
        </label>

        {selectedRepo && (
          <>
            <label className="block">
              <span className="text-[11px] text-muted">Branch selection</span>
              <div className="mt-1 flex gap-2 text-sm">
                {(["single", "all", "select"] as const).map((mode) => (
                  <button
                    key={mode}
                    onClick={() => setBranchMode(mode)}
                    className={`px-3 py-1.5 rounded-lg border text-[11px] ${
                      branchMode === mode
                        ? "border-accent bg-accent/10 text-accent"
                        : "border-border text-muted hover:border-accent"
                    }`}
                  >
                    {mode === "single" ? "Single" : mode === "all" ? "All" : "Select"}
                  </button>
                ))}
              </div>
            </label>

            {branchMode === "single" && (
              <label className="block">
                <span className="text-[11px] text-muted">Branch</span>
                <select
                  value={selectedBranch}
                  onChange={(e) => setSelectedBranch(e.target.value)}
                  disabled={loadingBranches}
                  className="mt-1 w-full bg-surface border border-border rounded-lg px-3 py-2 text-sm outline-none focus:border-accent"
                >
                  {loadingBranches ? (
                    <option>loading…</option>
                  ) : (
                    branches.map((b) => (
                      <option key={b.name} value={b.name}>
                        {b.name}
                      </option>
                    ))
                  )}
                </select>
              </label>
            )}

            {branchMode === "select" && (
              <label className="block">
                <span className="text-[11px] text-muted">Branches to clone</span>
                <div className="mt-1 max-h-40 overflow-y-auto border border-border rounded-lg p-2 space-y-1">
                  {loadingBranches ? (
                    <div className="text-[11px] text-muted px-2 py-1">loading…</div>
                  ) : (
                    branches.map((b) => (
                      <label key={b.name} className="flex items-center gap-2 px-2 py-1 rounded hover:bg-surface cursor-pointer text-sm">
                        <input
                          type="checkbox"
                          checked={selectedBranches.has(b.name)}
                          onChange={(e) => {
                            const next = new Set(selectedBranches);
                            if (e.target.checked) next.add(b.name);
                            else next.delete(b.name);
                            setSelectedBranches(next);
                          }}
                          className="accent-accent"
                        />
                        {b.name}
                      </label>
                    ))
                  )}
                </div>
              </label>
            )}

            {branchMode === "all" && (
              <div className="text-[11px] text-muted bg-surface border border-border rounded-lg px-3 py-2">
                All branches will be cloned (shallow).
              </div>
            )}
          </>
        )}

        <button
          onClick={handleCreate}
          disabled={submitting || !title.trim()}
          className="w-full py-2 rounded-lg bg-accent text-white font-medium disabled:opacity-40"
        >
          {submitting ? "Creating…" : "Create Workspace"}
        </button>
      </div>
    </div>
  );
}

// --- Workspace Detail View -------------------------------------------------

function WorkspaceDetail({
  client,
  workspace: initial,
  onBack,
  onUpdated,
  onDeleted,
}: {
  client: GitHubClient;
  workspace: Workspace;
  onBack: () => void;
  onUpdated: (ws: Workspace) => void;
  onDeleted: (id: string) => void;
}) {
  const [ws, setWs] = useState(initial);
  const [logs, setLogs] = useState<PushLog[]>([]);
  const [commitMsg, setCommitMsg] = useState("");
  const [committing, setCommitting] = useState(false);
  const [pushing, setPushing] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [pendingApproval, setPendingApproval] = useState<{
    requestId: string;
    status: string;
    branch?: string;
    title?: string;
  } | null>(null);
  const [approving, setApproving] = useState(false);
  const approvingRef = useRef(false);
  const [publishing, setPublishing] = useState(false);

  // Load push logs
  useEffect(() => {
    let alive = true;
    client
      .pushLogs(ws.id, 10)
      .then((r) => {
        if (alive) setLogs(r.logs);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ws.id]);

  // Poll pending push approval
  useEffect(() => {
    if (!pendingApproval || pendingApproval.status !== "pending") return;
    let alive = true;
    const iv = setInterval(async () => {
      try {
        const result = await client.pushRequestStatus(pendingApproval.requestId);
        if (!alive) return;
        if (result.status === "approved" || result.status === "rejected" || result.status === "error") {
          clearInterval(iv);
          setPendingApproval(null);
          if (result.status === "approved") {
            setSuccess("Push approved! Reloading…");
            // Refresh workspace state
            const fresh = await client.getWorkspace(ws.id);
            if (alive) {
              setWs(fresh);
              onUpdated(fresh);
              const logsR = await client.pushLogs(ws.id, 10);
              if (alive) setLogs(logsR.logs);
            }
          } else if (result.status === "rejected") {
            setError("Push rejected.");
          } else {
            setError("Push request failed.");
          }
        }
        // If still pending, do nothing — interval continues
      } catch {
        // Transient error — keep polling
      }
    }, 3000);
    return () => {
      alive = false;
      clearInterval(iv);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingApproval?.requestId, pendingApproval?.status]);

  async function handleCommit() {
    if (!commitMsg.trim()) return;
    setCommitting(true);
    setError("");
    setSuccess("");
    try {
      const r = await client.commit(ws.id, commitMsg.trim());
      setSuccess(`Committed: ${r.commit_sha.slice(0, 7)}`);
      setCommitMsg("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCommitting(false);
    }
  }

  async function handlePush() {
    setPushing(true);
    setError("");
    setSuccess("");
    try {
      const result = await client.push(ws.id, { branch: ws.current_branch });
      if ("request_id" in result) {
        setPendingApproval({
          requestId: result.request_id,
          status: "pending",
          branch: result.branch,
          title: result.title,
        });
        setSuccess("Push pending approval — waiting for review");
      } else {
        setSuccess(`Pushed: ${(result as { commit_sha: string }).commit_sha.slice(0, 7)}`);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setPushing(false);
    }
  }

  async function handleApprovePush(approved: boolean) {
    if (!pendingApproval || approvingRef.current) return;
    approvingRef.current = true;
    setApproving(true);
    try {
      await client.resolvePushRequest(pendingApproval.requestId, approved);
      // The polling effect will pick up the status change
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      approvingRef.current = false;
      setApproving(false);
    }
  }

  async function handleDelete() {
    if (!confirm(`Delete workspace "${ws.title}"? This cannot be undone.`)) return;
    try {
      await client.deleteWorkspace(ws.id);
      onDeleted(ws.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function handlePublish() {
    setPublishing(true);
    setError("");
    try {
      await client.publish(ws.id);
      setSuccess("Published to registry");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setPublishing(false);
    }
  }

  async function handleUnpublish() {
    setPublishing(true);
    setError("");
    try {
      await client.unpublish(ws.id);
      setSuccess("Removed from registry");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setPublishing(false);
    }
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-2 px-3 h-9 border-b border-border text-[11px] text-muted">
        <button onClick={onBack} className="underline">
          ← back
        </button>
        <span className="font-medium text-text">{ws.title}</span>
        <span className="px-1.5 py-0.5 rounded bg-surface2 text-[10px]">
          {ws.current_branch}
        </span>
        {ws.source_repo && (
          <a
            href={ws.source_repo}
            target="_blank"
            rel="noreferrer noopener"
            className="text-accent underline ml-auto"
          >
            repo ↗
          </a>
        )}
      </div>

      {error && (
        <div className="px-3 py-2 text-sm text-rose-300 border-b border-rose-500/40 bg-rose-500/10">
          {error}
        </div>
      )}
      {success && (
        <div className="px-3 py-2 text-sm text-green-300 border-b border-green-500/40 bg-green-500/10">
          {success}
        </div>
      )}

      {/* Pending Push Approval */}
      {pendingApproval && pendingApproval.status === "pending" && (
        <div className="px-3 py-3 border-b border-border bg-amber-500/5">
          <div className="flex items-center gap-2 mb-2">
            <span className="inline-block w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
            <span className="text-sm text-amber-200">Push pending approval</span>
          </div>
          {pendingApproval.branch && (
            <div className="text-[11px] text-muted mb-2">
              Branch: <span className="text-text">{pendingApproval.branch}</span>
            </div>
          )}
          <div className="flex gap-2">
            <button
              onClick={() => handleApprovePush(true)}
              disabled={approving}
              className="flex-1 py-1.5 rounded-lg bg-green-600 text-white text-sm disabled:opacity-40"
            >
              {approving ? "…" : "Approve"}
            </button>
            <button
              onClick={() => handleApprovePush(false)}
              disabled={approving}
              className="flex-1 py-1.5 rounded-lg border border-rose-500/40 text-rose-300 text-sm hover:bg-rose-500/10 disabled:opacity-40"
            >
              {approving ? "…" : "Reject"}
            </button>
          </div>
        </div>
      )}

      {/* Commit + Push */}
      <div className="border-b border-border p-3 space-y-2">
        <textarea
          value={commitMsg}
          onChange={(e) => setCommitMsg(e.target.value)}
          placeholder="Commit message…"
          rows={2}
          className="w-full bg-surface border border-border rounded-lg px-3 py-2 text-sm outline-none focus:border-accent resize-none"
        />
        <div className="flex gap-2">
          <button
            onClick={handleCommit}
            disabled={committing || !commitMsg.trim()}
            className="flex-1 py-1.5 rounded-lg border border-border text-sm hover:border-accent disabled:opacity-40"
          >
            {committing ? "committing…" : "Commit"}
          </button>
          <button
            onClick={handlePush}
            disabled={pushing}
            className="flex-1 py-1.5 rounded-lg bg-accent text-white text-sm disabled:opacity-40"
          >
            {pushing ? "pushing…" : "Push"}
          </button>
        </div>
      </div>

      {/* Push History */}
      <div className="flex-1 overflow-y-auto">
        <div className="px-3 py-2 text-[11px] text-muted font-medium border-b border-border">
          push history
        </div>
        {logs.length === 0 ? (
          <div className="text-center text-muted text-sm mt-8">No pushes yet</div>
        ) : (
          <div className="divide-y divide-border">
            {logs.map((log) => (
              <div key={log.id} className="px-3 py-2 text-[11px]">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-text">{log.commit_sha.slice(0, 7)}</span>
                  <span className="text-muted">{log.push_type}</span>
                  {log.pr_url && (
                    <a
                      href={log.pr_url}
                      target="_blank"
                      rel="noreferrer noopener"
                      className="text-accent underline"
                    >
                      PR #{log.pr_number}
                    </a>
                  )}
                </div>
                {log.commit_message && (
                  <div className="text-muted mt-0.5">{log.commit_message}</div>
                )}
                <div className="text-muted mt-0.5">
                  → {log.target_repo.replace("https://github.com/", "")}/{log.target_branch} ·{" "}
                  {new Date(log.created_at).toLocaleString()}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Danger zone */}
      <div className="border-t border-border p-3 space-y-2">
        <button
          onClick={handlePublish}
          disabled={publishing}
          className="w-full py-1.5 rounded-lg border border-border text-[11px] hover:border-accent disabled:opacity-40"
        >
          {publishing ? "…" : "Publish to Registry"}
        </button>
        <button
          onClick={handleUnpublish}
          disabled={publishing}
          className="w-full py-1.5 rounded-lg border border-border text-[11px] hover:border-accent disabled:opacity-40"
        >
          {publishing ? "…" : "Unpublish from Registry"}
        </button>
        <button
          onClick={handleDelete}
          className="w-full py-1.5 rounded-lg border border-rose-500/40 text-rose-300 text-[11px] hover:bg-rose-500/10"
        >
          Delete Workspace
        </button>
      </div>
    </div>
  );
}

// --- Registry Browser ------------------------------------------------------

function RegistryBrowser({
  client,
  onBack,
}: {
  client: GitHubClient;
  onBack: () => void;
}) {
  const [entries, setEntries] = useState<RegistryEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [error, setError] = useState("");
  const searchRef = useRef(0);

  useEffect(() => {
    let alive = true;
    client
      .listRegistry({ per_page: 50 })
      .then((r) => {
        if (alive) setEntries(r.items);
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleSearch() {
    setLoading(true);
    setError("");
    const reqId = ++searchRef.current;
    try {
      const r = await client.listRegistry({ search: search.trim() || undefined, per_page: 50 });
      if (reqId === searchRef.current) setEntries(r.items);
    } catch (e) {
      if (reqId === searchRef.current) setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (reqId === searchRef.current) setLoading(false);
    }
  }

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-2 px-3 h-9 border-b border-border text-[11px] text-muted">
        <button onClick={onBack} className="underline">
          ← back
        </button>
        <span className="font-medium text-text">Public Workspaces</span>
      </div>

      <div className="px-3 py-2 border-b border-border flex gap-2">
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleSearch()}
          placeholder="Search…"
          className="flex-1 bg-surface border border-border rounded-lg px-3 py-1.5 text-sm outline-none focus:border-accent"
        />
        <button
          onClick={handleSearch}
          className="px-3 py-1.5 rounded-lg border border-border text-sm hover:border-accent"
        >
          Search
        </button>
      </div>

      {error && (
        <div className="px-3 py-2 text-sm text-rose-300 border-b border-rose-500/40 bg-rose-500/10">
          {error}
        </div>
      )}

      <div className="flex-1 overflow-y-auto">
        {loading ? (
          <div className="text-center text-muted text-sm mt-12">loading…</div>
        ) : entries.length === 0 ? (
          <div className="text-center text-muted text-sm mt-12 px-6">
            {search ? "No results found." : "No public workspaces yet."}
          </div>
        ) : (
          <div className="divide-y divide-border">
            {entries.map((entry) => (
              <div key={entry.workspace_id} className="px-3 py-3">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-text">{entry.title}</span>
                  <span className="text-[11px] text-muted">by {entry.owner_username}</span>
                </div>
                {entry.description && (
                  <div className="text-[11px] text-muted mt-0.5">{entry.description}</div>
                )}
                {entry.source_repo && (
                  <a
                    href={entry.source_repo}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="text-[11px] text-accent underline mt-0.5 inline-block"
                  >
                    {entry.source_repo.replace("https://github.com/", "")} ↗
                  </a>
                )}
                {entry.hf_space_url && (
                  <a
                    href={entry.hf_space_url}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="text-[11px] text-accent underline mt-0.5 inline-block ml-2"
                  >
                    Space ↗
                  </a>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
