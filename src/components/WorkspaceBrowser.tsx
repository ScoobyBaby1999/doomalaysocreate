import { useCallback, useEffect, useState } from "react";
import type { AgentClient } from "../api/agent";

interface FileEntry {
  path: string;
  size: number;
  mtime: number;
}

interface TreeNode {
  name: string;
  path: string;
  children: TreeNode[];
  file?: FileEntry;
}

function buildTree(files: FileEntry[]): TreeNode[] {
  const root: TreeNode[] = [];
  const map = new Map<string, TreeNode>();

  const sorted = [...files].sort((a, b) => a.path.localeCompare(b.path));

  for (const f of sorted) {
    const parts = f.path.split("/");
    let path = "";
    let parent = root;
    for (let i = 0; i < parts.length; i++) {
      path = path ? `${path}/${parts[i]}` : parts[i];
      const isFile = i === parts.length - 1;
      let node = map.get(path);
      if (!node) {
        node = { name: parts[i], path, children: [], file: isFile ? f : undefined };
        map.set(path, node);
        parent.push(node);
      }
      parent = node.children;
    }
  }
  return root;
}

function TreeNodeRow({
  node,
  depth,
  onSelect,
}: {
  node: TreeNode;
  depth: number;
  onSelect: (path: string) => void;
}) {
  const [open, setOpen] = useState(depth < 1);

  if (node.file) {
    return (
      <button
        onClick={() => onSelect(node.path)}
        className="flex items-center gap-2 w-full text-left px-2 py-1 hover:bg-surface rounded text-[12px] font-mono text-muted hover:text-text transition-colors"
        style={{ paddingLeft: `${12 + depth * 16}px` }}
      >
        <span className="shrink-0 opacity-50">📄</span>
        <span className="truncate">{node.name}</span>
        <span className="ml-auto text-[10px] text-muted shrink-0">{fmtSize(node.file.size)}</span>
      </button>
    );
  }

  return (
    <div>
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-2 w-full text-left px-2 py-1 hover:bg-surface rounded text-[12px] font-mono text-muted transition-colors"
        style={{ paddingLeft: `${12 + depth * 16}px` }}
      >
        <span className="shrink-0">{open ? "📂" : "📁"}</span>
        <span className="truncate">{node.name}/</span>
      </button>
      {open && node.children.map((child) => (
        <TreeNodeRow key={child.path} node={child} depth={depth + 1} onSelect={onSelect} />
      ))}
    </div>
  );
}

export function WorkspaceBrowser({
  client,
  sessionId,
  onSelectFile,
}: {
  client: AgentClient;
  sessionId: string | null;
  onSelectFile?: (path: string) => void;
}) {
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    if (!sessionId) return;
    setLoading(true);
    setError("");
    try {
      const res = await client.files(sessionId);
      setFiles(res.files);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to load files");
    } finally {
      setLoading(false);
    }
  }, [client, sessionId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const tree = buildTree(files);

  if (!sessionId) {
    return <div className="text-muted text-xs p-3 italic">No active session</div>;
  }

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between px-3 py-1.5 border-b border-border">
        <span className="text-xs text-muted">{files.length} files</span>
        <button onClick={refresh} className="text-xs text-muted hover:text-accent px-1" disabled={loading}>
          {loading ? "…" : "↻"}
        </button>
      </div>
      {error && <div className="text-red-400 text-xs p-2">{error}</div>}
      <div className="flex-1 overflow-y-auto p-1">
        {tree.length === 0 && !loading && (
          <div className="text-muted text-xs italic p-3">No files yet</div>
        )}
        {tree.map((node) => (
          <TreeNodeRow
            key={node.path}
            node={node}
            depth={0}
            onSelect={(path) => onSelectFile?.(path)}
          />
        ))}
      </div>
    </div>
  );
}

function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}
