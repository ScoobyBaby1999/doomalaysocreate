import { html } from "diff2html";
import { useEffect, useRef } from "react";

export function DiffView({ diff, filename }: { diff: string; filename?: string }) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current || !diff.trim()) return;
    try {
      const output = html(diff, {
        drawFileList: true,
        matching: "lines",
        outputFormat: "line-by-line",
        highlight: false,
      });
      ref.current.innerHTML = typeof output === "string" ? output : "";
    } catch {
      ref.current.innerHTML = `<pre class="text-[11px] font-mono whitespace-pre-wrap p-2 text-red-400">failed to render diff</pre>`;
    }
  }, [diff]);

  if (!diff.trim()) {
    return <div className="text-muted text-xs italic p-2">No changes</div>;
  }

  return (
    <div className="diff-view overflow-x-auto rounded border border-border bg-black/20">
      {filename && (
        <div className="px-3 py-1 text-[11px] font-mono text-muted border-b border-border">
          {filename}
        </div>
      )}
      <div ref={ref} className="min-h-[40px]" />
    </div>
  );
}
