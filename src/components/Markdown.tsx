import { useEffect, useRef, useMemo } from "react";
import { renderMarkdown, highlightAll } from "../lib/markdown";

/** Renders markdown text and highlights code blocks.
 *  PERFORMANCE: memoizes the rendered HTML by text so streaming deltas only
 *  re-parse the changed message, not every visible message. The highlight
 *  effect only runs when the HTML actually changes. */
export const Markdown = ({ text }: { text: string }) => {
  const ref = useRef<HTMLDivElement>(null);
  // Memoize the HTML so identical text doesn't re-parse. During streaming,
  // only the message whose text changed re-renders (parent uses React.memo).
  const html = useMemo(() => renderMarkdown(text), [text]);
  useEffect(() => {
    if (ref.current) highlightAll(ref.current);
  }, [html]);
  return (
    <div
      ref={ref}
      className="md text-[15px] text-text"
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
};
