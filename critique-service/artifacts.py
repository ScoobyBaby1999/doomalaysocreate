from __future__ import annotations
import re
import secrets

# Multi-file artifact protocol: marker blocks + per-request nonce.
#
# A model emits each file between sentinel lines carrying a nonce + path:
#
#   ===== BEGIN FILE [<nonce>] path=src/app.py =====
#   <raw file content, verbatim — may itself contain ``` or markdown>
#   ===== END FILE [<nonce>] path=src/app.py =====
#
# We parse by the SENTINEL lines (not by code fences), so nested ``` blocks
# survive. The nonce is generated per request and injected into the prompt; the
# parser only accepts markers carrying it, so file content can never accidentally
# forge a marker. Recovery: a missing END is closed by the next BEGIN or EOF and
# the file is flagged truncated. Lossless fallback: zero valid markers -> the whole
# reply is returned as one output.md. Paths are sanitized (no abs / no ..).


def make_nonce() -> str:
    return secrets.token_hex(3)  # 6 hex chars, e.g. "a7f3c1"


def _begin_re(nonce: str) -> re.Pattern:
    return re.compile(
        r"^=+\s*BEGIN FILE\s*\[" + re.escape(nonce) + r"\]\s*path=(.+?)\s*=*\s*$"
    )


def _end_re(nonce: str) -> re.Pattern:
    return re.compile(r"^=+\s*END FILE\s*\[" + re.escape(nonce) + r"\]")


def artifact_instructions(nonce: str) -> str:
    #   spliced into a file-producing stage's directive so the model emits files
    #   in the marker format. Kept terse and in-distribution.
    return (
        "\n\n## Output format: FILES\n"
        "Emit your output as one or more complete files. Wrap EVERY file exactly like this:\n\n"
        f"===== BEGIN FILE [{nonce}] path=relative/path.ext =====\n"
        "<the full, final file content — verbatim, no truncation>\n"
        f"===== END FILE [{nonce}] path=relative/path.ext =====\n\n"
        "Rules:\n"
        f"- Use the token [{nonce}] on every marker line, exactly as shown.\n"
        "- Use relative paths only (no leading '/', no '..'). Group related files in folders.\n"
        "- Put NOTHING between an END marker and the next BEGIN marker.\n"
        "- Output the complete content of each file; do not abbreviate or use placeholders.\n"
        "- A short prose note before the first file is fine, but all deliverables must be in files."
    )


def safe_path(path: str) -> str | None:
    #   normalize + reject unsafe paths. returns a clean relative path or None.
    p = (path or "").strip().strip('"').strip("'").replace("\\", "/")
    if not p:
        return None
    p = p.lstrip("/")
    parts = [seg for seg in p.split("/") if seg not in ("", ".")]
    if any(seg == ".." for seg in parts):
        return None
    cleaned = "/".join(parts)
    return cleaned or None


def parse_artifacts(text: str, nonce: str) -> list[dict]:
    #   parse marker-delimited files out of `text`. returns a list of
    #   {path, content, truncated}. zero markers -> a single output.md fallback.
    if not nonce:
        return [{"path": "output.md", "content": text, "truncated": False}]
    begin_re, end_re = _begin_re(nonce), _end_re(nonce)
    lines = text.splitlines()
    files: list[dict] = []
    cur_path: str | None = None
    cur_buf: list[str] = []

    def _flush(truncated: bool) -> None:
        nonlocal cur_path, cur_buf
        if cur_path is None:
            return
        sp = safe_path(cur_path)
        if sp is not None:
            files.append({
                "path": sp,
                "content": "\n".join(cur_buf).strip("\n") + "\n",
                "truncated": truncated,
            })
        cur_path, cur_buf = None, []

    for line in lines:
        bm = begin_re.match(line)
        if bm:
            #   a new BEGIN closes any open block (missing END -> truncated).
            _flush(truncated=True)
            cur_path = bm.group(1)
            cur_buf = []
            continue
        if cur_path is not None and end_re.match(line):
            _flush(truncated=False)
            continue
        if cur_path is not None:
            cur_buf.append(line)
    #   trailing unterminated block (token-limit truncation).
    _flush(truncated=True)

    if not files:
        #   lossless fallback — never drop the model's output.
        return [{"path": "output.md", "content": text, "truncated": False}]
    return files


def files_to_body(files: list[dict]) -> str:
    #   path-labelled concatenation so the text-based judge rules still apply
    #   to file output. one block per file.
    chunks = []
    for f in files:
        flag = "  (truncated)" if f.get("truncated") else ""
        chunks.append(f"### FILE: {f['path']}{flag}\n\n{f['content']}")
    return "\n\n".join(chunks)
