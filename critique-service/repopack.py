from __future__ import annotations
import io
import tarfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from ssrfguard import BlockedAddress, assert_public_url

# Codebase ingestion: turn a {path: content} mapping (or a fetched repo tarball) into
# ONE annotated text blob a judge can reason over in a single pass. Validated live
# (2026-06): kimi/glm/nemotron genuinely reviewed this repo's full 110k-token pack.
#
# Design notes from the panel's critique of this very plan:
#   - token estimate: chars/3 for code (chars/4 underestimates code tokenization),
#     chars/4 for prose. documented, deliberately conservative.
#   - budget fitting drops vendored/generated/largest files first - NEVER tests/docs
#     first (they define intended behavior).
#   - every reduced pack reports coverage so partial-context reviews are honest.

#   prose extensions estimate at chars/4; everything else (code) at chars/3.
_PROSE_EXTS = (".md", ".txt", ".rst")
#   never reviewable: binaries and bulk artifacts.
_BINARY_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz",
                ".tar", ".whl", ".so", ".dylib", ".dll", ".exe", ".bin", ".pyc",
                ".woff", ".woff2", ".ttf", ".eot", ".mp3", ".mp4", ".webm", ".onnx",
                ".pt", ".safetensors", ".parquet", ".db", ".sqlite")
_LOCKFILES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
              "Pipfile.lock", "Cargo.lock", "go.sum", "composer.lock", "uv.lock")
_SKIP_DIR_PARTS = (".git/", "node_modules/", "__pycache__/", ".venv/", "venv/",
                   "dist/", "build/", ".mypy_cache/", ".pytest_cache/", "data/",
                   ".idea/", ".vscode/", "vendor/", "third_party/")
#   per-file char ceiling: a single enormous file shouldn't eat the whole budget.
MAX_FILE_CHARS = 200_000


def estimate_tokens(path: str, content: str) -> int:
    div = 4 if path.lower().endswith(_PROSE_EXTS) else 3
    return max(1, len(content) // div)


@dataclass
class Pack:
    files: dict[str, str]                       # included path -> content
    skipped: list[dict] = field(default_factory=list)  # [{path, reason}]
    @property
    def total_tokens(self) -> int:
        return sum(estimate_tokens(p, c) for p, c in self.files.items())
    @property
    def file_tokens(self) -> dict[str, int]:
        return {p: estimate_tokens(p, c) for p, c in self.files.items()}


def pack_files(files: dict[str, str], *, include_lockfiles: bool = False) -> Pack:
    #   filter a raw {path: content} mapping down to reviewable text files.
    keep: dict[str, str] = {}
    skipped: list[dict] = []
    for path, content in sorted(files.items()):
        p = path.replace("\\", "/").lstrip("/")
        if not isinstance(content, str):
            skipped.append({"path": p, "reason": "non-text content"})
            continue
        if any(part in f"{p}/" or f"/{part}" in p for part in _SKIP_DIR_PARTS):
            skipped.append({"path": p, "reason": "excluded directory"})
            continue
        if p.lower().endswith(_BINARY_EXTS):
            skipped.append({"path": p, "reason": "binary extension"})
            continue
        if "\x00" in content[:8192]:
            skipped.append({"path": p, "reason": "binary content (NUL byte)"})
            continue
        if not include_lockfiles and p.rsplit("/", 1)[-1] in _LOCKFILES:
            skipped.append({"path": p, "reason": "lockfile (set include_lockfiles)"})
            continue
        if len(content) > MAX_FILE_CHARS:
            content = content[:MAX_FILE_CHARS] + "\n... [truncated by repopack]\n"
        keep[p] = content
    return Pack(files=keep, skipped=skipped)


def _drop_score(path: str, tokens: int) -> tuple:
    #   budget-fitting drop order (highest score drops first). tests/docs are KEPT
    #   as long as possible (panel: they define intended behavior); generated/vendored
    #   and the largest deep-leaf files go first.
    p = path.lower()
    name = p.rsplit("/", 1)[-1]
    cls = 1  # default: ordinary source
    if "generated" in p or name.endswith((".min.js", ".min.css", ".lock")) or name.startswith("generated_"):
        cls = 3
    elif p.endswith((".csv", ".tsv", ".jsonl")) or name in ("changelog.md", "license", "license.md"):
        cls = 2
    elif "test" in p or "docs/" in p or p.endswith(_PROSE_EXTS):
        cls = 0  # dropped LAST
    depth = p.count("/")
    return (cls, tokens + depth * 50)


def fit_to_budget(pack: Pack, budget_tokens: int) -> tuple[Pack, list[str]]:
    #   reduce a pack to fit a judge's context budget. returns (reduced_pack,
    #   dropped_paths). drop order: generated/bulk first, then ordinary source by
    #   size; tests/docs last. deterministic.
    if pack.total_tokens <= budget_tokens:
        return pack, []
    ft = pack.file_tokens
    order = sorted(ft, key=lambda p: _drop_score(p, ft[p]), reverse=True)
    keep = dict(pack.files)
    dropped: list[str] = []
    total = pack.total_tokens
    for path in order:
        if total <= budget_tokens:
            break
        total -= ft[path]
        keep.pop(path)
        dropped.append(path)
    reduced = Pack(files=keep, skipped=list(pack.skipped))
    return reduced, dropped


def render(pack: Pack, *, dropped: list[str] | None = None) -> str:
    #   the blob a judge sees: a file-tree header (including what was omitted, so the
    #   model knows its blind spots), then each file delimited by an unambiguous marker.
    lines = ["# REPOSITORY PACK", "", "## File tree (included)"]
    lines += [f"- {p}  (~{t} tokens)" for p, t in sorted(pack.file_tokens.items())]
    if dropped:
        lines += ["", "## Omitted to fit your context budget (you have NOT seen these)"]
        lines += [f"- {p}" for p in sorted(dropped)]
    if pack.skipped:
        lines += ["", "## Skipped (binary/lockfile/excluded)"]
        lines += [f"- {s['path']}  ({s['reason']})" for s in pack.skipped[:50]]
    body = [f"\n===== FILE: {p} =====\n{c}" for p, c in sorted(pack.files.items())]
    return "\n".join(lines) + "\n" + "".join(body)


# --- repo_url fetching -------------------------------------------------------

REPO_FETCH_TIMEOUT_S = 60.0


class RepoFetchError(ValueError):
    """client-safe fetch failure (bad url, too big, not found)."""


def _codeload_url(repo_url: str, ref: str) -> str:
    #   accept https://github.com/owner/repo[.git] and convert to the codeload
    #   tarball endpoint. only github.com for now (no git binary in the container).
    u = repo_url.strip().rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    parts = u.split("github.com/")
    if len(parts) != 2 or parts[1].count("/") != 1:
        raise RepoFetchError("repo_url must look like https://github.com/<owner>/<repo>")
    return f"https://codeload.github.com/{parts[1]}/tar.gz/{ref}"


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    #   SSRF guard: validate every redirect target resolves to a PUBLIC address before
    #   following it (codeload legitimately redirects to a CDN, but a malicious redirect
    #   could point at an internal / cloud-metadata host).
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        assert_public_url(newurl)  # raises BlockedAddress -> aborts the fetch
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_repo_files(repo_url: str, *, ref: str = "HEAD", token: str = "",
                     max_bytes: int) -> dict[str, str]:
    #   fetch a repo tarball over HTTPS (stdlib only) and return {path: content} for
    #   its text files. token (optional) is forwarded as the auth header for private
    #   repos and is NEVER logged or persisted. size-capped before extraction.
    url = _codeload_url(repo_url, ref)
    try:
        assert_public_url(url)  # the initial host (defense-in-depth + DNS-trick catch)
    except BlockedAddress as e:
        raise RepoFetchError(f"repo fetch blocked: {e}") from e
    req = urllib.request.Request(url, headers={"User-Agent": "doomalaysocreate-panel"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    opener = urllib.request.build_opener(_GuardedRedirectHandler())
    try:
        with opener.open(req, timeout=REPO_FETCH_TIMEOUT_S) as resp:
            raw = resp.read(max_bytes + 1)
    except BlockedAddress as e:
        raise RepoFetchError(f"repo fetch blocked: redirect to non-public host ({e})") from e
    except urllib.error.HTTPError as e:
        raise RepoFetchError(f"repo fetch failed: HTTP {e.code} (check url/ref/token)") from e
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        #   a BlockedAddress raised inside the redirect handler surfaces wrapped in URLError.
        if isinstance(getattr(e, "reason", None), BlockedAddress):
            raise RepoFetchError(f"repo fetch blocked: {e.reason}") from e
        raise RepoFetchError(f"repo fetch failed: {type(e).__name__}") from e
    if len(raw) > max_bytes:
        raise RepoFetchError(f"repo tarball exceeds REPO_MAX_BYTES={max_bytes}")
    files: dict[str, str] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
            for m in tf.getmembers():
                if not m.isfile() or m.size > MAX_FILE_CHARS * 4:
                    continue
                #   strip the tarball's top-level "<repo>-<ref>/" directory.
                path = m.name.split("/", 1)[1] if "/" in m.name else m.name
                fh = tf.extractfile(m)
                if fh is None:
                    continue
                data = fh.read()
                try:
                    files[path] = data.decode("utf-8")
                except UnicodeDecodeError:
                    continue  # binary - pack_files would skip it anyway
    except tarfile.TarError as e:
        raise RepoFetchError(f"not a valid tar.gz: {type(e).__name__}") from e
    if not files:
        raise RepoFetchError("repo contained no decodable text files")
    return files
