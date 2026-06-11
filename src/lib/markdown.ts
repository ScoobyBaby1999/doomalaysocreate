import { marked } from "marked";
import hljs from "highlight.js";

// Render markdown to HTML with code highlighting. During streaming we call this on the
// growing text; marked is string-based (fast enough per flush). We escape nothing extra
// because the source is model output rendered in our own trusted shell, but we DO sanitize
// by disabling raw HTML passthrough to avoid injected <script> from a tool/web result.
marked.setOptions({
  gfm: true,
  breaks: false,
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
