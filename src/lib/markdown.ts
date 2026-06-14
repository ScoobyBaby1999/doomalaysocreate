import { marked } from "marked";
import hljs from "highlight.js";

// Render markdown to HTML with code highlighting. During streaming we call this on the
// growing text; marked is string-based (fast enough per flush). We disable raw HTML
// passthrough to prevent XSS via <script>, <img onerror>, etc. in model output or
// tool/web results.
marked.use({
  gfm: true,
  breaks: false,
  html: false,
});

export function renderMarkdown(src: string): string {
  const html = marked.parse(src ?? "", { async: false }) as string;
  return html;
}

export function highlightAll(root: HTMLElement) {
  root.querySelectorAll("pre code").forEach((el) => {
    if (!(el as HTMLElement).dataset.hl) {
      try {
        hljs.highlightElement(el as HTMLElement);
      } catch {
        /* unknown language */
      }
      (el as HTMLElement).dataset.hl = "1";
    }
  });
}
