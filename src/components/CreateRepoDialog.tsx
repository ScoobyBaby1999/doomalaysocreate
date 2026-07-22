/**
 * CreateRepoDialog — full-screen modal (bottom-sheet on mobile) that lets
 * the user create a brand-new GitHub repo through our app, then clones it
 * as a workspace. Mirrors GitHub's "Create repository" form.
 *
 * Backend wiring:
 *   - POST /api/github/repos/create { name, description, private, auto_init,
 *     gitignore_template, license_template, owner } — proxies to GitHub's
 *     POST /user/repos (or /orgs/:org/repos when owner is an org) using the
 *     user's stored OAuth token.
 *   - GET /api/github/gitignore/templates — list of ~100 .gitignore templates.
 *   - GET /api/github/licenses — list of 14 license templates.
 *   - GET /api/github/orgs — orgs the user can create repos in.
 *
 * If the backend hasn't implemented these routes yet, we fall back to a
 * curated static list (still 14 licenses, top ~80 gitignores) so the form
 * is always usable. The actual create call surfaces the backend's error.
 */
import { useEffect, useMemo, useState } from "react";
import { GitHubClient, type Repo } from "../api/github";
import type { Workspace } from "../api/github";

// --- Static fallbacks (used if the backend proxy isn't wired yet) ---------
// 14 licenses — matches the task spec exactly.
const STATIC_LICENSES: { key: string; name: string; spdx_id?: string | null }[] = [
  { key: "", name: "None", spdx_id: null },
  { key: "mit", name: "MIT License", spdx_id: "MIT" },
  { key: "apache-2.0", name: "Apache License 2.0", spdx_id: "Apache-2.0" },
  { key: "gpl-3.0", name: "GNU General Public License v3.0", spdx_id: "GPL-3.0" },
  { key: "bsd-2-clause", name: "BSD 2-Clause \"Simplified\" License", spdx_id: "BSD-2-Clause" },
  { key: "bsd-3-clause", name: "BSD 3-Clause \"New\" or \"Revised\" License", spdx_id: "BSD-3-Clause" },
  { key: "lgpl-3.0", name: "GNU Lesser General Public License v3.0", spdx_id: "LGPL-3.0" },
  { key: "mpl-2.0", name: "Mozilla Public License 2.0", spdx_id: "MPL-2.0" },
  { key: "cddl-1.0", name: "Common Development and Distribution License 1.0", spdx_id: "CDDL-1.0" },
  { key: "epl-2.0", name: "Eclipse Public License 2.0", spdx_id: "EPL-2.0" },
  { key: "unlicense", name: "The Unlicense", spdx_id: "Unlicense" },
  { key: "gpl-2.0", name: "GNU General Public License v2.0", spdx_id: "GPL-2.0" },
  { key: "agpl-3.0", name: "GNU Affero General Public License v3.0", spdx_id: "AGPL-3.0" },
  { key: "0bsd", name: "BSD Zero Clause License", spdx_id: "0BSD" },
];

// Top ~80 .gitignore templates (GitHub's full list is ~100). Empty = "None".
const STATIC_GITIGNORES: string[] = [
  "", "Node", "Python", "Rust", "Go", "Java", "Ruby", "C", "C++", "C#", "Swift",
  "Kotlin", "Scala", "Haskell", "Elixir", "Erlang", "Clojure", "Perl", "PHP",
  "Lua", "R", "Julia", "Dart", "Flutter", "Zig", "Nim", "Crystal", "OCaml",
  "F#", "CommonLisp", "Scheme", "D", "Objective-C", "Gradle", "Maven", "Sbt",
  "React", "Next.js", "NuxtJS", "Vue", "Angular", "Svelte", "Gatsby", "Docusaurus",
  "Jekyll", "Hugo", "Hexo", "Rails", "Django", "Flask", "FastAPI", "Laravel",
  "Symfony", "SpringBoot", "Quarkus", "DotnetCore", "Unity", "UnrealEngine",
  "Godot", "Android", "iOS", "macOS", "Windows", "Linux", "VisualStudio",
  "VSCode", "IntelliJ", "Eclipse", "Xcode", "JetBrains", "SublimeText", "Vim",
  "Emacs", "Nano", "Vagrant", "Docker", "Terraform", "Ansible", "Chef", "Puppet",
];

interface Props {
  open: boolean;
  client: GitHubClient;
  /** Default owner (the user's GitHub login). */
  defaultOwner?: string;
  onClose: () => void;
  /** Called after the GitHub repo is created — should create the workspace. */
  onRepoCreated: (repo: Repo) => Promise<Workspace>;
  /** Called after the workspace is created from the new repo. */
  onWorkspaceReady: (ws: Workspace) => void;
}

export function CreateRepoDialog({
  open,
  client,
  defaultOwner,
  onClose,
  onRepoCreated,
  onWorkspaceReady,
}: Props) {
  // --- form state ---
  const [owner, setOwner] = useState(defaultOwner || "");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [isPrivate, setIsPrivate] = useState(false);
  const [addReadme, setAddReadme] = useState(true);
  const [gitignore, setGitignore] = useState("");
  const [license, setLicense] = useState("");

  // --- option lists (live-fetched with static fallback) ---
  const [owners, setOwners] = useState<{ login: string; avatar_url?: string }[]>([]);
  const [licenses, setLicenses] = useState(STATIC_LICENSES);
  const [gitignores, setGitignores] = useState<string[]>(STATIC_GITIGNORES);
  const [loadingOpts, setLoadingOpts] = useState(false);

  // --- submission state ---
  const [submitting, setSubmitting] = useState(false);
  const [stage, setStage] = useState<"idle" | "creating-repo" | "cloning-workspace" | "done">("idle");
  const [error, setError] = useState("");

  // --- validation ---
  const nameError = useMemo(() => {
    if (!name) return "";
    if (name.length > 100) return "Max 100 characters";
    if (!/^[a-z0-9_.-]+$/.test(name)) return "Lowercase letters, digits, -, _, . only";
    if (name.startsWith(".") || name.startsWith("-")) return "Can't start with . or -";
    return "";
  }, [name]);

  const descCount = description.length;
  const DESC_MAX = 350;
  const canSubmit = !!name && !nameError && !submitting;

  // Load dropdown options on open (best-effort — fall back to static lists).
  useEffect(() => {
    if (!open) return;
    let alive = true;
    setLoadingOpts(true);
    Promise.allSettled([
      client.orgs(),
      client.gitignoreTemplates(),
      client.licenseTemplates(),
    ]).then((results) => {
      if (!alive) return;
      // owners
      if (results[0].status === "fulfilled") {
        const orgs = results[0].value.orgs || [];
        if (orgs.length) {
          const me = defaultOwner ? [{ login: defaultOwner, avatar_url: undefined }, ...orgs] : orgs;
          setOwners(me);
          if (!owner) setOwner(me[0]?.login || "");
        }
      } else if (defaultOwner && !owner) {
        setOwner(defaultOwner);
      }
      // gitignores
      if (results[1].status === "fulfilled") {
        const v = results[1].value as any;
        const arr: { name?: string }[] = Array.isArray(v) ? v : (v?.templates || []);
        const names = arr.map((t) => (typeof t === "string" ? t : t?.name || "")).filter(Boolean);
        if (names.length) setGitignores(["", ...names]);
      }
      // licenses
      if (results[2].status === "fulfilled") {
        const v = results[2].value as any;
        const arr: { key?: string; name?: string; spdx_id?: string | null }[] = Array.isArray(v) ? v : (v?.licenses || []);
        const mapped = arr
          .filter((l) => l && l.key)
          .map((l) => ({ key: l.key!, name: l.name || l.key!, spdx_id: l.spdx_id }));
        if (mapped.length) {
          // Prepend a "None" entry so users can opt out.
          setLicenses([{ key: "", name: "None", spdx_id: null }, ...mapped]);
        }
      }
    }).finally(() => {
      if (alive) setLoadingOpts(false);
    });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Reset state when reopened.
  useEffect(() => {
    if (open) {
      setStage("idle");
      setError("");
      setSubmitting(false);
    }
  }, [open]);

  if (!open) return null;

  async function handleSubmit() {
    if (!canSubmit) return;
    setSubmitting(true);
    setError("");
    try {
      // 1. Create the GitHub repo.
      setStage("creating-repo");
      const repo = await client.createRepo({
        name: name.trim(),
        description: description.trim() || undefined,
        private: isPrivate,
        auto_init: addReadme,
        gitignore_template: gitignore || undefined,
        license_template: license || undefined,
        owner: owner && owner !== defaultOwner ? owner : undefined,
      });

      // 2. Clone it as a workspace.
      setStage("cloning-workspace");
      const ws = await onRepoCreated(repo);

      setStage("done");
      // Brief success flash, then close.
      setTimeout(() => {
        onWorkspaceReady(ws);
      }, 300);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setStage("idle");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="fixed inset-0 z-[80] flex flex-col">
      {/* backdrop */}
      <div
        className="absolute inset-0 bg-black/70 backdrop-blur-sm"
        onClick={submitting ? undefined : onClose}
      />
      {/* sheet — full-screen on mobile, modal on desktop */}
      <div
        className="relative mt-auto sm:mx-auto sm:my-auto sm:max-w-lg w-full bg-bg sm:rounded-2xl rounded-t-2xl border border-border shadow-2xl flex flex-col max-h-[92vh] sm:max-h-[88vh] slide-up"
        style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
      >
        {/* drag handle (mobile) */}
        <div className="sm:hidden flex justify-center pt-2 pb-1">
          <div className="w-10 h-1 rounded-full bg-border" />
        </div>

        {/* header */}
        <div className="flex items-center justify-between px-4 h-12 border-b border-border shrink-0">
          <div className="flex items-center gap-2 min-w-0">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#a855f7" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0">
              <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
            </svg>
            <span className="text-sm font-semibold text-text truncate">Create New Workspace</span>
          </div>
          <button
            onClick={onClose}
            disabled={submitting}
            aria-label="Close"
            className="touch-target w-9 h-9 rounded-xl bg-surface2 flex items-center justify-center text-muted hover:text-text disabled:opacity-40"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>

        {/* scrollable body */}
        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          {error && (
            <div className="text-sm text-rose-300 bg-rose-500/10 border border-rose-500/40 rounded-xl px-3 py-2">
              {error}
            </div>
          )}

          {stage === "done" && (
            <div className="text-sm text-emerald-300 bg-emerald-500/10 border border-emerald-500/40 rounded-xl px-3 py-2">
              ✓ Workspace ready — opening…
            </div>
          )}

          {/* Owner */}
          <label className="block space-y-1">
            <span className="text-[11px] uppercase tracking-wide text-muted-foreground font-medium">Owner</span>
            <select
              value={owner}
              onChange={(e) => setOwner(e.target.value)}
              disabled={loadingOpts && owners.length === 0}
              className="w-full bg-surface border border-border rounded-xl px-3 py-2.5 text-sm outline-none focus:border-accent"
            >
              {owners.length === 0 && defaultOwner ? (
                <option value={defaultOwner}>{defaultOwner}</option>
              ) : (
                owners.map((o) => (
                  <option key={o.login} value={o.login}>{o.login}</option>
                ))
              )}
            </select>
          </label>

          {/* Name */}
          <label className="block space-y-1">
            <span className="text-[11px] uppercase tracking-wide text-muted-foreground font-medium">
              Repository name <span className="text-rose-400">*</span>
            </span>
            <div className="flex items-center gap-1 text-sm text-muted">
              <span className="shrink-0 px-2 py-2.5 rounded-l-xl bg-surface2 border border-border border-r-0">{owner || "you"}/</span>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="my-awesome-project"
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
                className="flex-1 bg-surface border border-border rounded-r-xl px-3 py-2.5 text-sm outline-none focus:border-accent"
              />
            </div>
            {nameError && <span className="text-[11px] text-rose-400">{nameError}</span>}
            {!nameError && name && (
              <span className="text-[11px] text-muted/70">Will be created at <span className="text-accent">{owner || "you"}/{name}</span></span>
            )}
          </label>

          {/* Description */}
          <label className="block space-y-1">
            <span className="text-[11px] uppercase tracking-wide text-muted-foreground font-medium">
              Description <span className="text-muted/60 normal-case">({descCount}/{DESC_MAX})</span>
            </span>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value.slice(0, DESC_MAX))}
              placeholder="What is this project about?"
              rows={3}
              className="w-full bg-surface border border-border rounded-xl px-3 py-2.5 text-sm outline-none focus:border-accent resize-none"
            />
          </label>

          {/* Visibility */}
          <div className="space-y-1.5">
            <span className="text-[11px] uppercase tracking-wide text-muted-foreground font-medium">Visibility</span>
            <div className="grid grid-cols-2 gap-2">
              <button
                type="button"
                onClick={() => setIsPrivate(false)}
                className={`flex items-center gap-2 px-3 py-2.5 rounded-xl border text-sm transition-colors ${
                  !isPrivate ? "border-accent bg-accent/10 text-accent" : "border-border text-muted hover:border-accent/50"
                }`}
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/>
                  <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>
                </svg>
                Public
              </button>
              <button
                type="button"
                onClick={() => setIsPrivate(true)}
                className={`flex items-center gap-2 px-3 py-2.5 rounded-xl border text-sm transition-colors ${
                  isPrivate ? "border-accent bg-accent/10 text-accent" : "border-border text-muted hover:border-accent/50"
                }`}
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>
                  <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
                </svg>
                Private
              </button>
            </div>
          </div>

          {/* README */}
          <label className="flex items-center gap-3 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={addReadme}
              onChange={(e) => setAddReadme(e.target.checked)}
              className="w-4 h-4 accent-accent"
            />
            <span className="text-sm text-text">Add a README file</span>
          </label>

          {/* .gitignore */}
          <label className="block space-y-1">
            <span className="text-[11px] uppercase tracking-wide text-muted-foreground font-medium">Add .gitignore</span>
            <select
              value={gitignore}
              onChange={(e) => setGitignore(e.target.value)}
              className="w-full bg-surface border border-border rounded-xl px-3 py-2.5 text-sm outline-none focus:border-accent"
            >
              {gitignores.map((g) => (
                <option key={g || "none"} value={g}>{g || "None"}</option>
              ))}
            </select>
          </label>

          {/* License */}
          <label className="block space-y-1">
            <span className="text-[11px] uppercase tracking-wide text-muted-foreground font-medium">Add license</span>
            <select
              value={license}
              onChange={(e) => setLicense(e.target.value)}
              className="w-full bg-surface border border-border rounded-xl px-3 py-2.5 text-sm outline-none focus:border-accent"
            >
              {licenses.map((l) => (
                <option key={l.key || "none"} value={l.key}>{l.name}{l.spdx_id ? ` (${l.spdx_id})` : ""}</option>
              ))}
            </select>
          </label>
        </div>

        {/* footer actions */}
        <div className="shrink-0 border-t border-border p-3 flex gap-2 items-center bg-surface/40">
          {stage === "creating-repo" && (
            <span className="text-[11px] text-muted-foreground mr-auto">Creating repo on GitHub…</span>
          )}
          {stage === "cloning-workspace" && (
            <span className="text-[11px] text-muted-foreground mr-auto">Cloning into workspace…</span>
          )}
          <button
            onClick={onClose}
            disabled={submitting}
            className="px-4 h-10 rounded-xl border border-border text-sm text-muted hover:text-text disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={!canSubmit}
            className="px-5 h-10 rounded-xl bg-accent text-white text-sm font-medium disabled:opacity-40 flex items-center gap-2"
          >
            {submitting ? (
              <>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="animate-spin">
                  <line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/>
                  <line x1="4.93" y1="4.93" x2="7.76" y2="7.76"/><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"/>
                  <line x1="2" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="22" y2="12"/>
                </svg>
                Working…
              </>
            ) : (
              "Create Workspace"
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
