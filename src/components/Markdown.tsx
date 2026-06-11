import { useEffect, useRef } from "react";
import { renderMarkdown, highlightAll } from "../lib/markdown";

/** Renders markdown text and highlights code blocks after each update (streaming-safe). */
export function Markdown({ text }: { text: string }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (ref.current) highlightAll(ref.current);
  }, [text]);
  return (
    <div
      ref={ref}
      className="md text-[15px] text-text"
      dangerouslySetInnerHTML={{ __html: renderMarkdown(text) }}
    />
  );
}
