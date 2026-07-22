"""Template library — user-created reusable templates for websearch,
deepresearch, judge, chat, and custom workflows.

Provides the /api/templates* family of endpoints (replacing the old
orchestrator-schematic /api/templates, which has moved to
/api/orchestrator/templates):

  * GET    /api/templates                  — list the caller's templates
                                              (private + published).
  * GET    /api/templates/explore          — browse public templates with
                                              sort (hearts|recent|relevant),
                                              text search, kind filter.
  * GET    /api/templates/<id>             — fetch one template (full markdown).
  * POST   /api/templates                  — create a new template.
  * PATCH  /api/templates/<id>             — update (owner only).
  * DELETE /api/templates/<id>             — delete (owner only).
  * POST   /api/templates/<id>/heart       — toggle heart.
  * POST   /api/templates/<id>/download    — download (creates a local copy
                                              for the caller so they can
                                              edit their own version).
  * POST   /api/templates/<id>/publish     — publish to the global library.
  * POST   /api/templates/<id>/unpublish   — unpublish.

Storage: SQLite via the existing db._db() connection (same DB file as the
rest of the service). Schema is created idempotently on first request.

Auth: same bearer token gate as the rest of /api/agent. Identity (GitHub
user_id) is read from the X-JWT header via the request handler's
_require_user_from_jwt(). Anonymous users (no JWT) get user_id=None and
can browse/explore but cannot create/heart/download.

The default catalog (11 system templates: Breadth Search, Deep Dive,
Compare & Contrast, Fact Check, Default Deep Research, ReAct Loop,
Extended Thinking, Critique/Verify/Improve/Debate Panels) is seeded
idempotently on first use via seed_defaults().

Wired into critique_service.py via handle_request(method, path, body, handler).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any
from urllib.parse import parse_qs, urlsplit

import db as _dbmod
import favorites as _favs
import public_dataset as _pub

_write_lock = _dbmod._write_lock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gen_id() -> str:
    return uuid.uuid4().hex[:16]


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _log(event: str, **fields: Any) -> None:
    """Best-effort structured log via debug_log (never raises)."""
    try:
        import debug_log
        debug_log.log_event(event, **fields)
    except Exception:
        pass


VALID_KINDS = ("websearch", "deepresearch", "judge", "chat", "custom")
VALID_SORTS = ("hearts", "recent", "relevant")
VALID_FILTERS = ("favorites",)  # additive filter values for list endpoints

# ---------------------------------------------------------------------------
# Role-based template system (Task 4)
# ---------------------------------------------------------------------------
# A template is now a {role, instructions, output_rules, inputs, template,
# required_schema} object that gets filled into the role's skeleton via
# content.roles.make_role(). The legacy `markdown` field is the COMPILED
# prompt (the role skeleton with the parts substituted in) so existing
# consumers keep working.
#
# The legacy `kind` field (websearch/deepresearch/judge/chat/custom) is kept
# for backward compat with existing rows + the frontend's old filter UI, but
# the NEW primary axis is `role` (planner/generator/critiquer/verifier/
# transformer/parser/assembler/reviewer/extractor).
import sqlite3 as _sqlite3  # noqa: E402  (used by migrations above)
try:
    from content.roles import Roles as _Roles, make_role as _make_role
    VALID_ROLES = tuple(r.value for r in _Roles)
except Exception:  # pragma: no cover — roles.py is always present in this repo
    VALID_ROLES = ("planner", "parser", "critiquer", "verifier", "generator",
                   "transformer", "assembler", "reviewer", "extractor")
    _make_role = None


def _normalize_role(role: str | None) -> str | None:
    """Normalise + validate a role name. Returns the canonical role string
    (lowercase, in VALID_ROLES) or None if not recognised."""
    if not role:
        return None
    r = str(role).strip().lower()
    if r in VALID_ROLES:
        return r
    # Accept a few common aliases.
    aliases = {
        "review": "reviewer",
        "extract": "extractor",
        "transform": "transformer",
        "verify": "verifier",
        "critic": "critiquer",
        "critique": "critiquer",
        "plan": "planner",
        "parse": "parser",
        "assemble": "assembler",
        "generate": "generator",
    }
    return aliases.get(r)


def compile_template_markdown(*, role: str, instructions: str = "",
                              output_rules: str = "", inputs: str = "",
                              template: str = "", required_schema: str = "") -> str:
    """Compile a role-based template into its final prompt markdown.

    Calls content.roles.make_role(...) to substitute the parts into the
    role's skeleton (content/prompts/<role>.md). Falls back to a plain
    concatenation if the roles module is unavailable (defensive).
    """
    r = _normalize_role(role)
    if not r:
        # No role -> just return the instructions as-is (legacy plain-text
        # template). This preserves backward compat with old rows.
        return (instructions or "").strip()
    if _make_role is not None:
        try:
            return _make_role(r, instructions=instructions or "",
                              output_rules=output_rules or "",
                              inputs=inputs or "",
                              template=template or "",
                              required_schema=required_schema or "")
        except Exception:
            pass
    # Defensive fallback: plain concatenation.
    parts = [f"# {r.title()}",
             instructions or "",
             output_rules or "",
             inputs or "",
             template or "",
             required_schema or ""]
    return "\n\n".join(p for p in parts if p and p.strip())


def _role_skeleton(role: str) -> str:
    """Return the raw role skeleton markdown (content/prompts/<role>.md)
    so the frontend can render a 'raw' view with syntax highlighting.
    Returns '' if the role is unknown or the file is missing."""
    r = _normalize_role(role)
    if not r:
        return ""
    try:
        # get_role expects a Roles enum; we have a string. Use get_prompt
        # directly (it takes a bare name like "planner" / "critiquer").
        from content.roles import get_prompt
        return get_prompt(r)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Schema (idempotent — safe to call on every request)
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS templates (
    id           TEXT PRIMARY KEY,
    author_id    TEXT NOT NULL,
    author_name  TEXT,
    name         TEXT NOT NULL,
    description  TEXT,
    markdown     TEXT NOT NULL,
    kind         TEXT NOT NULL,
    tags         TEXT,
    is_public    INTEGER NOT NULL DEFAULT 0,
    hearts       INTEGER NOT NULL DEFAULT 0,
    downloads    INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_templates_author ON templates(author_id, created_at);
CREATE INDEX IF NOT EXISTS idx_templates_public ON templates(is_public, hearts DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_templates_kind ON templates(kind, is_public);

CREATE TABLE IF NOT EXISTS template_hearts (
    template_id  TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (template_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_template_hearts_user ON template_hearts(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS template_downloads (
    template_id  TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (template_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_template_downloads_user ON template_downloads(user_id, created_at DESC);
"""


def _ensure_schema() -> None:
    db = _dbmod._db()
    db.executescript(SCHEMA)
    # Role-based template system migration (Task 4): add columns for the
    # roles.py-based template structure. Each template now has a `role`
    # (planner|generator|critiquer|verifier|transformer|parser|assembler|
    # reviewer|extractor) plus the parts that fill the role's skeleton
    # (instructions, output_rules, inputs, template, required_schema).
    # The legacy `markdown` column is kept and populated with the COMPILED
    # prompt (make_role(...)) so existing consumers keep working.
    for col, decl in (
        ("role",            "TEXT"),
        ("instructions",    "TEXT"),
        ("output_rules",    "TEXT"),
        ("inputs",          "TEXT"),
        ("template_part",   "TEXT"),  # `template` is a SQL keyword — use template_part
        ("required_schema", "TEXT"),
        # Multi-stage template system (REAL template format from the
        # reference repo): a template is now a JSON object with task_type,
        # stages (array of stage objects), and output_rules (object). The
        # legacy single-role fields above are kept for backward compat;
        # the new fields are the source-of-truth for multi-stage templates.
        ("task_type",        "TEXT"),   # e.g. "research_paper", "code_review"
        ("task",             "TEXT"),   # short label
        ("stages_json",      "TEXT"),   # JSON array of stage objects
        ("output_rules_json","TEXT"),   # JSON object with format/min_words/etc.
    ):
        try:
            db.execute(f"ALTER TABLE templates ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass  # column already exists
    db.commit()


_schema_initialized = False
_schema_lock = threading.Lock()


def _ensure_schema_once() -> None:
    global _schema_initialized
    if _schema_initialized:
        return
    with _schema_lock:
        if not _schema_initialized:
            _ensure_schema()
            # Shared favorites/downloads schema (templates + workspaces).
            _favs._ensure_schema_once()
            # One-time best-effort migration of legacy per-template
            # hearts/downloads rows into the new generic tables.
            _migrate_legacy_favorites()
            seed_defaults()
            _schema_initialized = True


def _migrate_legacy_favorites() -> None:
    """One-time best-effort migration: copy rows from the legacy
    ``template_hearts`` / ``template_downloads`` tables into the new
    generic ``favorites`` / ``downloads`` tables managed by
    ``favorites.py``.

    Idempotent (uses INSERT OR IGNORE). Silently skips if the legacy
    tables don't exist or are empty. Never raises — a migration failure
    must not block first-request template initialization.
    """
    db = _dbmod._db()
    now = _iso_now()
    try:
        with _write_lock:
            # Legacy hearts -> generic favorites (item_type='template')
            try:
                rows = db.execute(
                    "SELECT template_id, user_id, created_at FROM template_hearts"
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            for r in rows:
                db.execute(
                    "INSERT OR IGNORE INTO favorites "
                    "(user_id, item_type, item_id, created_at) "
                    "VALUES (?, 'template', ?, ?)",
                    (r["user_id"], r["template_id"], r["created_at"] or now))
            # Legacy downloads -> generic downloads (item_type='template')
            try:
                rows = db.execute(
                    "SELECT template_id, user_id, created_at FROM template_downloads"
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            for r in rows:
                db.execute(
                    "INSERT OR IGNORE INTO downloads "
                    "(user_id, item_type, item_id, created_at) "
                    "VALUES (?, 'template', ?, ?)",
                    (r["user_id"], r["template_id"], r["created_at"] or now))
            db.commit()
    except Exception as exc:
        _log("template_legacy_fav_migration_failed", error=repr(exc)[:200])


# ---------------------------------------------------------------------------
# Multi-stage template helpers (REAL template format)
# ---------------------------------------------------------------------------
# A multi-stage template is a JSON object with:
#   task_type:   str           — e.g. "research_paper", "code_review"
#   task:        str           — short label
#   description: str           — human-readable description
#   stages:      list[stage]   — ordered pipeline of stages
#   output_rules: dict         — format/min_words/required_sections/tone/etc.
#
# Each stage is:
#   name:         str           — stage identifier (unique within the template)
#   role:         str           — planner|parser|critiquer|verifier|generator|
#                                 transformer|assembler|reviewer|extractor
#   instructions: str           — detailed prompt for this stage
#   inputs:       list[str]     — context paths this stage reads (e.g.
#                                 ["prompt", "outline.topics.{i}"])
#   fanout:       dict|None     — {"over": "outline.topics", "max_parallel": 3}
#   max_tokens:   int|None      — per-stage token budget
#
# The legacy single-role fields (role, instructions, output_rules, inputs,
# template, required_schema) are kept for backward compat. A template can be
# EITHER single-role (legacy) OR multi-stage (new). When both are present,
# the multi-stage fields take precedence.

def _normalize_stages(stages: Any) -> str:
    """Coerce a stages input (list of dicts) into a JSON array string for
    storage. Validates each stage has a name + role. Returns "[]" on bad input.
    """
    if not isinstance(stages, list):
        return "[]"
    out: list[dict] = []
    for st in stages:
        if not isinstance(st, dict):
            continue
        name = str(st.get("name", "")).strip()
        role = _normalize_role(st.get("role"))
        if not name or not role:
            continue  # skip invalid stages
        stage: dict[str, Any] = {
            "name": name,
            "role": role,
            "instructions": str(st.get("instructions", "") or "").strip(),
        }
        inputs = st.get("inputs")
        if isinstance(inputs, list):
            stage["inputs"] = [str(i).strip() for i in inputs if str(i).strip()]
        elif isinstance(inputs, str) and inputs.strip():
            stage["inputs"] = [inputs.strip()]
        else:
            stage["inputs"] = []
        fanout = st.get("fanout")
        if isinstance(fanout, dict) and fanout:
            fo: dict[str, Any] = {}
            over = fanout.get("over")
            if isinstance(over, str) and over.strip():
                fo["over"] = over.strip()
            mp = fanout.get("max_parallel")
            if isinstance(mp, int) and mp > 0:
                fo["max_parallel"] = mp
            if fo:
                stage["fanout"] = fo
        mt = st.get("max_tokens")
        if isinstance(mt, int) and mt > 0:
            stage["max_tokens"] = mt
        out.append(stage)
    return json.dumps(out, ensure_ascii=False)


def _normalize_output_rules(rules: Any) -> str:
    """Coerce an output_rules input (dict) into a JSON object string for
    storage. Returns "{}" on bad input."""
    if not isinstance(rules, dict):
        return "{}"
    out: dict[str, Any] = {}
    fmt = rules.get("format")
    if isinstance(fmt, str) and fmt.strip():
        out["format"] = fmt.strip()
    for k in ("min_words", "max_words"):
        v = rules.get(k)
        if isinstance(v, int) and v > 0:
            out[k] = v
    rs = rules.get("required_sections")
    if isinstance(rs, list):
        out["required_sections"] = [str(s).strip() for s in rs if str(s).strip()]
    bp = rules.get("banned_phrases")
    if isinstance(bp, list):
        out["banned_phrases"] = [str(s).strip() for s in bp if str(s).strip()]
    tone = rules.get("tone")
    if isinstance(tone, str) and tone.strip():
        out["tone"] = tone.strip()
    # Preserve any extra keys the caller supplied (forward-compat).
    for k, v in rules.items():
        if k not in out and v is not None:
            out[k] = v
    return json.dumps(out, ensure_ascii=False)


def _parse_json_field(raw: Any, default: Any) -> Any:
    """Parse a JSON string field back into a Python object. Returns ``default``
    on parse failure or if ``raw`` is already a Python object (passthrough)."""
    if raw is None:
        return default
    if isinstance(raw, (list, dict)):
        return raw
    if isinstance(raw, str):
        if not raw.strip():
            return default
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return default
    return default


def compile_stages_markdown(*, task_type: str = "", task: str = "",
                            description: str = "",
                            stages: list | None = None,
                            output_rules: dict | None = None) -> str:
    """Compile a multi-stage template into a human-readable markdown summary.

    This is the COMPILED prompt stored in the ``markdown`` column for backward
    compat with consumers that read markdown directly. It is NOT the actual
    pipeline runner — the runner executes each stage separately via the
    orchestrator. This markdown is a PREVIEW of what the template does.

    Format:
        # <task_type>: <task>

        <description>

        ## Stages
        1. **<name>** (<role>) — <instructions[:120]>...
           inputs: [a, b] | fanout: over=X, max_parallel=N | max_tokens: T
        2. ...

        ## Output Rules
        - format: markdown
        - min_words: 4000
        - required_sections: [References, ...]
        - banned_phrases: [clearly, ...]
        - tone: rigorous
    """
    parts: list[str] = []
    title_bits = []
    if task_type:
        title_bits.append(task_type)
    if task:
        title_bits.append(task)
    if title_bits:
        parts.append("# " + ": ".join(title_bits))
    if description:
        parts.append(description.strip())
    if stages:
        parts.append("## Stages")
        lines = []
        for i, st in enumerate(stages, 1):
            name = st.get("name", "?")
            role = st.get("role", "?")
            instr = (st.get("instructions", "") or "").strip()
            preview = (instr[:120] + "...") if len(instr) > 120 else instr
            line = f"{i}. **{name}** ({role})"
            if preview:
                line += f" — {preview}"
            lines.append(line)
            meta_bits = []
            inputs = st.get("inputs") or []
            if inputs:
                meta_bits.append(f"inputs: {inputs}")
            fanout = st.get("fanout") or {}
            if fanout:
                meta_bits.append(
                    f"fanout: over={fanout.get('over','?')}, "
                    f"max_parallel={fanout.get('max_parallel','?')}")
            mt = st.get("max_tokens")
            if mt:
                meta_bits.append(f"max_tokens: {mt}")
            if meta_bits:
                lines.append(f"   - " + " | ".join(meta_bits))
        parts.append("\n".join(lines))
    if output_rules:
        parts.append("## Output Rules")
        rule_lines = []
        if output_rules.get("format"):
            rule_lines.append(f"- format: {output_rules['format']}")
        if output_rules.get("min_words"):
            rule_lines.append(f"- min_words: {output_rules['min_words']}")
        if output_rules.get("max_words"):
            rule_lines.append(f"- max_words: {output_rules['max_words']}")
        if output_rules.get("required_sections"):
            rule_lines.append(
                f"- required_sections: {output_rules['required_sections']}")
        if output_rules.get("banned_phrases"):
            rule_lines.append(
                f"- banned_phrases: {output_rules['banned_phrases']}")
        if output_rules.get("tone"):
            rule_lines.append(f"- tone: {output_rules['tone']}")
        if rule_lines:
            parts.append("\n".join(rule_lines))
    return "\n\n".join(p for p in parts if p and p.strip())


# ---------------------------------------------------------------------------
# Row -> dict mapper
# ---------------------------------------------------------------------------

def _row_to_dict(row: sqlite3.Row | dict | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    # Parse tags JSON -> list
    raw_tags = d.get("tags")
    if isinstance(raw_tags, str) and raw_tags:
        try:
            d["tags"] = json.loads(raw_tags)
        except (json.JSONDecodeError, ValueError):
            d["tags"] = []
    elif raw_tags is None:
        d["tags"] = []
    # Booleans (SQLite stores as 0/1)
    d["is_public"] = bool(d.get("is_public", 0))
    # Role-based system (Task 4): surface the role + parts under their
    # canonical names. The DB column is `template_part` (because `template`
    # is a SQL keyword) but the API exposes it as `template` for clarity.
    if "template_part" in d:
        d["template"] = d.pop("template_part")
    # Default missing role fields to None (legacy rows have them as NULL).
    for k in ("role", "instructions", "output_rules", "inputs",
              "template", "required_schema"):
        d.setdefault(k, None)
    # Multi-stage template fields (REAL template format): parse the JSON
    # columns into Python objects so the frontend gets structured stages +
    # output_rules, not raw JSON strings.
    d["stages"] = _parse_json_field(d.get("stages_json"), [])
    d["output_rules_obj"] = _parse_json_field(d.get("output_rules_json"), {})
    # Expose task_type + task at the top level (the DB columns are already
    # named task_type / task — no aliasing needed).
    d.setdefault("task_type", None)
    d.setdefault("task", None)
    # If the template has a role but no compiled markdown (e.g. an old row
    # migrated to role-based), compile it on the fly so the frontend always
    # has a markdown preview.
    if d.get("role") and not (d.get("markdown") or "").strip():
        try:
            d["markdown"] = compile_template_markdown(
                role=d["role"],
                instructions=d.get("instructions") or "",
                output_rules=d.get("output_rules") or "",
                inputs=d.get("inputs") or "",
                template=d.get("template") or "",
                required_schema=d.get("required_schema") or "")
        except Exception:
            pass
    # Multi-stage templates: if stages are present but markdown is empty,
    # compile a preview markdown from the stages so legacy consumers still
    # get a non-empty markdown field.
    if d.get("stages") and not (d.get("markdown") or "").strip():
        try:
            d["markdown"] = compile_stages_markdown(
                task_type=d.get("task_type") or "",
                task=d.get("task") or "",
                description=d.get("description") or "",
                stages=d["stages"],
                output_rules=d["output_rules_obj"])
        except Exception:
            pass
    return d


def _normalize_tags(tags: Any) -> str:
    """Coerce tags input (list, comma-separated string, or None) into a JSON
    array string for storage. Strips empties + duplicates, lowercases."""
    if tags is None:
        return "[]"
    if isinstance(tags, str):
        # comma-separated
        parts = [t.strip() for t in tags.split(",") if t.strip()]
    elif isinstance(tags, list):
        parts = [str(t).strip() for t in tags if str(t).strip()]
    else:
        return "[]"
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        pl = p.lower()
        if pl not in seen:
            seen.add(pl)
            out.append(p)
    return json.dumps(out, ensure_ascii=False)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_template(*, user_id: str | None, name: str,
                    markdown: str | None = None, kind: str = "custom",
                    description: str | None = None,
                    tags: Any = None,
                    is_public: bool = False,
                    author_name: str | None = None,
                    role: str | None = None,
                    instructions: str | None = None,
                    output_rules: str | None = None,
                    inputs: str | None = None,
                    template: str | None = None,
                    required_schema: str | None = None,
                    task_type: str | None = None,
                    task: str | None = None,
                    stages: Any = None,
                    output_rules_obj: Any = None) -> dict:
    """Create a new template. Returns the template dict.

    Role-based system (Task 4): if ``role`` is provided, the template is
    compiled from the role + parts via :func:`compile_template_markdown`.
    The compiled prompt is stored in the ``markdown`` column (for backward
    compat with consumers that read markdown directly) AND the parts are
    stored in their own columns (so the frontend can show a 'raw' view
    with the role skeleton + parts separately).

    Legacy mode: if ``role`` is NOT provided, ``markdown`` must be a
    non-empty string (the old plain-text template format).

    Multi-stage system (REAL template format): if ``stages`` is provided
    (a list of stage dicts), the template is stored as a multi-stage
    pipeline. The ``markdown`` column is populated with a compiled preview
    (via :func:`compile_stages_markdown`) so legacy consumers keep working.
    The ``stages_json`` + ``output_rules_json`` columns store the structured
    pipeline (the source-of-truth for multi-stage templates). ``task_type``
    and ``task`` are short labels for the template type.
    """
    _ensure_schema_once()
    if not name or not name.strip():
        raise ValueError("'name' is required")
    norm_role = _normalize_role(role)
    # Normalize multi-stage fields (REAL template format).
    norm_task_type = (str(task_type).strip() if task_type else None)
    norm_task = (str(task).strip() if task else None)
    stages_json = _normalize_stages(stages) if stages is not None else "[]"
    output_rules_json = (_normalize_output_rules(output_rules_obj)
                         if output_rules_obj is not None else "{}")
    has_stages = stages_json not in ("[]", "")
    if has_stages:
        # Multi-stage: compile a preview markdown from the stages.
        parsed_stages = _parse_json_field(stages_json, [])
        parsed_rules = _parse_json_field(output_rules_json, {})
        compiled = compile_stages_markdown(
            task_type=norm_task_type or "",
            task=norm_task or "",
            description=(description or "").strip(),
            stages=parsed_stages,
            output_rules=parsed_rules)
        if not compiled or not compiled.strip():
            raise ValueError("could not compile multi-stage template markdown")
        markdown = compiled
    elif norm_role:
        # Role-based: compile the markdown from parts.
        compiled = compile_template_markdown(
            role=norm_role,
            instructions=instructions or "",
            output_rules=output_rules or "",
            inputs=inputs or "",
            template=template or "",
            required_schema=required_schema or "")
        if not compiled or not compiled.strip():
            raise ValueError("could not compile template from role + parts")
        markdown = compiled
    else:
        # Legacy plain-text: markdown is required.
        if not markdown or not markdown.strip():
            raise ValueError("'markdown' (or 'role' + parts or 'stages') is required")
    if kind not in VALID_KINDS:
        # Default to 'custom' for role-based templates that don't fit a kind.
        kind = "custom"
    db = _dbmod._db()
    tid = _gen_id()
    now = _iso_now()
    author_id = user_id or "anonymous"
    author = author_name or _author_label_for(user_id)
    with _write_lock:
        db.execute(
            "INSERT INTO templates (id, author_id, author_name, name, description, "
            "markdown, kind, tags, is_public, hearts, downloads, "
            "role, instructions, output_rules, inputs, template_part, required_schema, "
            "task_type, task, stages_json, output_rules_json, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, author_id, author, name.strip(),
             (description or "").strip() or None,
             markdown, kind, _normalize_tags(tags),
             1 if is_public else 0,
             norm_role,
             (instructions or "").strip() or None,
             (output_rules or "").strip() or None,
             (inputs or "").strip() or None,
             (template or "").strip() or None,
             (required_schema or "").strip() or None,
             norm_task_type,
             norm_task,
             stages_json,
             output_rules_json,
             now, now))
        db.commit()
    row = db.execute("SELECT * FROM templates WHERE id = ?", (tid,)).fetchone()
    return _row_to_dict(row) or {}


def list_my_templates(user_id: str | None, *, limit: int = 200,
                      filter: str | None = None) -> list[dict]:
    """List the caller's own templates (private + published), newest first.

    Each returned template dict includes per-user ``hearted`` and
    ``downloaded`` boolean flags (always False for anonymous callers) so the
    frontend can render the correct heart/download button state after a
    page reload — without these flags the UI couldn't tell which templates
    the current user has already hearted/downloaded (Bug 4).

    ``filter='favorites'`` returns the templates the caller has hearted
    (across ALL templates — not just their own), newest heart first.
    Anonymous callers get an empty list with this filter (no per-user
    state to filter on)."""
    _ensure_schema_once()
    db = _dbmod._db()
    filter = (filter or "").strip().lower() or None
    # Filter-by-favorites: return every template the user has hearted,
    # regardless of authorship (the user's "favorites" view).
    if filter == "favorites":
        if not user_id:
            return []
        fav_ids = _favs.list_favorites(user_id, "template", limit=limit)
        if not fav_ids:
            return []
        placeholders = ",".join("?" * len(fav_ids))
        rows = db.execute(
            "SELECT * FROM templates WHERE id IN (" + placeholders + ") "
            "ORDER BY created_at DESC LIMIT ?",
            [*fav_ids, limit]).fetchall()
        out = [_row_to_dict(r) for r in rows]
        _annotate_user_flags(out, user_id)
        # Enforce visibility: drop templates the caller can't see (private
        # templates authored by someone else — defensive; should not happen
        # because hearting requires visibility, but guard anyway).
        out = [t for t in out
               if t.get("is_public") or _is_owner(t, user_id)]
        return out
    if user_id:
        rows = db.execute(
            "SELECT * FROM templates WHERE author_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, limit)).fetchall()
    else:
        # Anonymous: only return anonymous-authored templates.
        rows = db.execute(
            "SELECT * FROM templates WHERE author_id = 'anonymous' "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,)).fetchall()
    out = [_row_to_dict(r) for r in rows]
    _annotate_user_flags(out, user_id)
    return out


def list_public_templates(*, sort: str = "hearts", query: str | None = None,
                          kind: str | None = None, role: str | None = None,
                          limit: int = 50, offset: int = 0,
                          user_id: str | None = None,
                          filter: str | None = None) -> tuple[list[dict], int]:
    """List public templates. Returns (templates, total). Sort by hearts
    (most hearted), recent (newest), or relevant (text-search relevance via
    LIKE on name+description+markdown+tags).

    Each returned template dict includes per-user ``hearted`` and
    ``downloaded`` boolean flags for ``user_id`` (always False when
    ``user_id`` is None) so the frontend can render the correct
    heart/download button state after a page reload (Bug 4).

    Source-of-truth: the public HF dataset (``public_dataset.py``).
    Falls back to local SQLite if the dataset is unreachable / empty
    (so the system default templates remain browsable offline).

    ``filter='favorites'`` returns only public templates the caller has
    hearted (Anonymous callers get an empty result with this filter)."""
    _ensure_schema_once()
    if sort not in VALID_SORTS:
        sort = "hearts"
    filter = (filter or "").strip().lower() or None
    page_limit = max(1, min(int(limit), 200))
    page_offset = max(0, int(offset))

    # ----- Try the public HF dataset first (source of truth) -----
    out: list[dict] = []
    total = 0
    try:
        pub_items, pub_total = _pub.list_public_templates(
            sort=sort, query=query, kind=kind, role=role,
            limit=page_limit, offset=page_offset)
    except Exception as exc:
        _log("template_public_list_failed", error=repr(exc)[:200])
        pub_items, pub_total = [], 0
    if pub_items:
        out = pub_items
        total = pub_total
        # Normalize: public-dataset records use list-typed tags + a bool
        # is_public; the rest of the code expects the same shape as a local
        # SQLite row (post _row_to_dict).
        for it in out:
            if it.get("tags") is None:
                it["tags"] = []
            if "is_public" not in it:
                it["is_public"] = True
        _annotate_user_flags(out, user_id)
        # Apply favorites filter post-hoc (only items the user has hearted).
        if filter == "favorites":
            if not user_id:
                return ([], 0)
            fav_ids = set(_favs.list_favorites(user_id, "template", limit=2000))
            out = [t for t in out if t.get("id") in fav_ids]
            total = len(out)
        return (out, total)

    # ----- Fallback: local SQLite (dataset unreachable / empty) -----
    db = _dbmod._db()
    where = "is_public = 1"
    params: list[Any] = []
    if kind and kind in VALID_KINDS:
        where += " AND kind = ?"
        params.append(kind)
    # Role filter (Task 4 role-based system).
    if role:
        norm_role = _normalize_role(role)
        if norm_role:
            where += " AND role = ?"
            params.append(norm_role)
    # Text search: LIKE on name + description + markdown + tags.
    has_query = bool(query and query.strip())
    if has_query:
        q = "%" + query.strip() + "%"
        where += " AND (name LIKE ? OR description LIKE ? OR markdown LIKE ? OR tags LIKE ?)"
        params.extend([q, q, q, q])
    # Favorites filter (local fallback): restrict to the user's hearted ids.
    if filter == "favorites":
        if not user_id:
            return ([], 0)
        fav_ids = _favs.list_favorites(user_id, "template", limit=2000)
        if not fav_ids:
            return ([], 0)
        placeholders = ",".join("?" * len(fav_ids))
        where += " AND id IN (" + placeholders + ")"
        params.extend(fav_ids)

    # Total count (for pagination).
    count_row = db.execute(
        f"SELECT COUNT(*) AS c FROM templates WHERE {where}", params).fetchone()
    total = int(count_row["c"]) if count_row else 0

    # Sort order.
    if sort == "hearts":
        order = "hearts DESC, downloads DESC, created_at DESC"
    elif sort == "recent":
        order = "created_at DESC"
    else:  # relevant — give exact-name matches a boost, then hearts.
        order = "CASE WHEN name LIKE ? THEN 0 ELSE 1 END, hearts DESC, created_at DESC"
        if has_query:
            # Insert the exact-name-match param at the front of the ORDER BY
            # params (after the WHERE params).
            params = list(params) + [query.strip() + "%"]
        else:
            # No query -> "relevant" degenerates to "hearts".
            order = "hearts DESC, downloads DESC, created_at DESC"

    rows = db.execute(
        f"SELECT * FROM templates WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
        params + [page_limit, page_offset]).fetchall()
    out = [_row_to_dict(r) for r in rows]
    _annotate_user_flags(out, user_id)
    return out, total


def _annotate_user_flags(templates: list[dict], user_id: str | None) -> None:
    """Annotate each template dict with per-user ``hearted`` and
    ``downloaded`` boolean flags (in-place). Anonymous callers (no
    user_id) get both flags set to False for every template.

    This is the fix for Bug 4 (heart/download don't persist): the
    list endpoints previously returned only the aggregate hearts/downloads
    counts, so the frontend couldn't tell which templates the current
    user had already hearted/downloaded — after a reload every heart
    button appeared unhearted even though the row was in
    ``template_hearts``. We do ONE bulk SELECT per table (keyed on the
    template ids in the page) so this stays O(page_size) not O(N²).

    Delegates to the shared ``favorites.annotate_user_flags`` so the same
    per-user flag logic is reused by templates AND workspaces (the user
    asked for these systems to share functionality)."""
    # The shared helper initialises both flags to False, handles the
    # anonymous case, and does the bulk SELECT. We keep this wrapper for
    # backward compat with the rest of this module.
    _favs.annotate_user_flags(user_id, "template", templates, id_key="id")


def get_template(template_id: str) -> dict | None:
    """Fetch one template by id (full markdown). Returns None if not found.

    Tries local SQLite first; if not found, falls back to the public HF
    dataset (for templates the caller has not downloaded/created locally
    but that have been published globally by another user)."""
    _ensure_schema_once()
    row = _dbmod._db().execute(
        "SELECT * FROM templates WHERE id = ?", (template_id,)).fetchone()
    if row:
        return _row_to_dict(row)
    # Fallback: public HF dataset (best-effort, never raises).
    try:
        pub = _pub.get_template(template_id)
        if pub:
            # Normalize: ensure is_public is set (everything in the public
            # dataset is public by definition) and tags is a list.
            pub = dict(pub)
            pub.setdefault("is_public", True)
            if pub.get("tags") is None:
                pub["tags"] = []
            return pub
    except Exception as exc:
        _log("template_public_get_failed",
             template_id=template_id, error=repr(exc)[:200])
    return None


def update_template(template_id: str, user_id: str | None, *,
                    name: str | None = None, description: str | None = None,
                    markdown: str | None = None, kind: str | None = None,
                    tags: Any = None, is_public: bool | None = None,
                    role: str | None = None,
                    instructions: str | None = None,
                    output_rules: str | None = None,
                    inputs: str | None = None,
                    template: str | None = None,
                    required_schema: str | None = None,
                    task_type: str | None = None,
                    task: str | None = None,
                    stages: Any = None,
                    output_rules_obj: Any = None) -> dict | None:
    """Update a template (owner only). Returns the updated template or None
    if not found / not owned by caller.

    Role-based system (Task 4): updating any of the role parts (role,
    instructions, output_rules, inputs, template, required_schema)
    triggers a re-compile of the ``markdown`` column so it stays in sync
    with the parts. The caller can still override markdown directly (for
    legacy plain-text templates).
    """
    _ensure_schema_once()
    db = _dbmod._db()
    existing = get_template(template_id)
    if not existing:
        return None
    if not _is_owner(existing, user_id):
        return None  # caller will surface a 403
    fields: list[str] = []
    params: list[Any] = []
    if name is not None and name.strip():
        fields.append("name = ?")
        params.append(name.strip())
    if description is not None:
        fields.append("description = ?")
        params.append(description.strip() or None)
    if markdown is not None and markdown.strip():
        fields.append("markdown = ?")
        params.append(markdown)
    if kind is not None and kind in VALID_KINDS:
        fields.append("kind = ?")
        params.append(kind)
    if tags is not None:
        fields.append("tags = ?")
        params.append(_normalize_tags(tags))
    if is_public is not None:
        fields.append("is_public = ?")
        params.append(1 if is_public else 0)
    # Role-based parts. We update each column individually if the caller
    # supplied it, then (if any role part changed) re-compile the markdown.
    norm_role = _normalize_role(role) if role is not None else None
    if role is not None:
        fields.append("role = ?")
        params.append(norm_role)
    if instructions is not None:
        fields.append("instructions = ?")
        params.append(instructions.strip() or None)
    if output_rules is not None:
        fields.append("output_rules = ?")
        params.append(output_rules.strip() or None)
    if inputs is not None:
        fields.append("inputs = ?")
        params.append(inputs.strip() or None)
    if template is not None:
        fields.append("template_part = ?")
        params.append(template.strip() or None)
    if required_schema is not None:
        fields.append("required_schema = ?")
        params.append(required_schema.strip() or None)
    # Multi-stage template fields (REAL template format).
    if task_type is not None:
        fields.append("task_type = ?")
        params.append(str(task_type).strip() or None)
    if task is not None:
        fields.append("task = ?")
        params.append(str(task).strip() or None)
    if stages is not None:
        stages_json = _normalize_stages(stages)
        fields.append("stages_json = ?")
        params.append(stages_json)
    if output_rules_obj is not None:
        output_rules_json = _normalize_output_rules(output_rules_obj)
        fields.append("output_rules_json = ?")
        params.append(output_rules_json)
    if not fields:
        return existing
    # Re-compile markdown if any role part OR multi-stage field changed.
    # We read the current parts from `existing` and override with the new
    # values. Multi-stage templates take precedence over single-role.
    changed_fields = {f.split(" = ")[0].strip() for f in fields}
    role_keys = ("role", "instructions", "output_rules", "inputs",
                 "template", "required_schema")
    multi_stage_changed = bool(changed_fields & {
        "task_type", "task", "stages_json", "output_rules_json"})
    role_changed = bool(changed_fields & {
        "role", "instructions", "output_rules", "inputs",
        "template_part", "required_schema"})
    if multi_stage_changed:
        # Re-compile the multi-stage markdown preview.
        merged_stages = (_parse_json_field(
            _normalize_stages(stages) if stages is not None
            else existing.get("stages_json"), [])
            if stages is not None
            else (existing.get("stages") or []))
        merged_rules = (_parse_json_field(
            _normalize_output_rules(output_rules_obj) if output_rules_obj is not None
            else existing.get("output_rules_json"), {})
            if output_rules_obj is not None
            else (existing.get("output_rules_obj") or {}))
        merged_task_type = (task_type if task_type is not None
                            else existing.get("task_type"))
        merged_task = (task if task is not None else existing.get("task"))
        if merged_stages:
            try:
                compiled = compile_stages_markdown(
                    task_type=merged_task_type or "",
                    task=merged_task or "",
                    description=(description if description is not None
                                 else existing.get("description")) or "",
                    stages=merged_stages,
                    output_rules=merged_rules)
                if compiled and compiled.strip():
                    fields.append("markdown = ?")
                    params.append(compiled)
            except Exception:
                pass
    elif role_changed:
        merged = {k: existing.get(k) for k in role_keys}
        if norm_role is not None:
            merged["role"] = norm_role
        if instructions is not None:
            merged["instructions"] = instructions
        if output_rules is not None:
            merged["output_rules"] = output_rules
        if inputs is not None:
            merged["inputs"] = inputs
        if template is not None:
            merged["template"] = template
        if required_schema is not None:
            merged["required_schema"] = required_schema
        if merged.get("role"):
            try:
                compiled = compile_template_markdown(
                    role=merged["role"],
                    instructions=merged.get("instructions") or "",
                    output_rules=merged.get("output_rules") or "",
                    inputs=merged.get("inputs") or "",
                    template=merged.get("template") or "",
                    required_schema=merged.get("required_schema") or "")
                if compiled and compiled.strip():
                    fields.append("markdown = ?")
                    params.append(compiled)
            except Exception:
                pass
    fields.append("updated_at = ?")
    params.append(_iso_now())
    params.append(template_id)
    with _write_lock:
        db.execute(
            f"UPDATE templates SET {', '.join(fields)} WHERE id = ?", params)
        db.commit()
    return get_template(template_id)


def delete_template(template_id: str, user_id: str | None) -> bool | None:
    """Delete a template (owner only). Returns True if deleted, False if not
    found, None if not owned by caller.

    Also removes the template from the public HF dataset (best-effort) and
    purges all per-user favorites/downloads rows via the shared
    ``favorites.purge_item`` helper."""
    _ensure_schema_once()
    db = _dbmod._db()
    existing = get_template(template_id)
    if not existing:
        return False
    if not _is_owner(existing, user_id):
        return None
    with _write_lock:
        db.execute("DELETE FROM templates WHERE id = ?", (template_id,))
        # Legacy tables (kept for backward compat with any older code paths).
        try:
            db.execute("DELETE FROM template_hearts WHERE template_id = ?", (template_id,))
        except sqlite3.OperationalError:
            pass
        try:
            db.execute("DELETE FROM template_downloads WHERE template_id = ?", (template_id,))
        except sqlite3.OperationalError:
            pass
        db.commit()
    # Shared generic tables (templates + workspaces).
    try:
        _favs.purge_item("template", template_id)
    except Exception as exc:
        _log("template_purge_favorites_failed",
             template_id=template_id, error=repr(exc)[:200])
    # Public HF dataset (best-effort — silent no-op if unreachable).
    try:
        _pub.unpublish_template(template_id)
    except Exception as exc:
        _log("template_unpublish_on_delete_failed",
             template_id=template_id, error=repr(exc)[:200])
    return True


def _ensure_published_for_counter(template: dict) -> None:
    """Ensure ``template`` is in the public HF dataset so its aggregate
    hearts/downloads counters can be tracked globally.

    System templates are seeded into local SQLite but NOT published to the
    HF dataset by default (seed_defaults only writes local rows). Without
    this guard, the first heart/download on a system template would silently
    no-op in _bump_counter (template not in the dataset → counter not bumped).
    We publish-on-first-heart/download so the global aggregate is always
    tracked, regardless of whether the template was explicitly published.

    Best-effort: silent no-op on any failure (network down, no HF_TOKEN,
    etc.). The local SQLite count still gets bumped by the caller, so the
    feature degrades gracefully when the dataset is unreachable.
    """
    if not template or not template.get("id"):
        return
    try:
        existing_pub = _pub.get_template(template["id"])
        if existing_pub:
            return  # already in the dataset — nothing to do
        _pub.publish_template(template)
    except Exception as exc:
        _log("template_ensure_published_failed",
             template_id=template.get("id"), error=repr(exc)[:200])


def heart_template(template_id: str, user_id: str | None) -> tuple[bool, int]:
    """Toggle heart on a template. Returns (hearted, hearts_count).

    Per-user state lives in the shared ``favorites`` table (so the same
    code path serves templates AND workspaces). The local SQLite
    ``templates.hearts`` column is bumped as a per-Space aggregate cache
    (used when the public HF dataset is unreachable). The GLOBAL aggregate
    count lives in the public HF dataset (``public_dataset.add_heart`` /
    ``remove_heart``) so hearts persist ACROSS users AND across Spaces.

    If the caller is anonymous (no user_id), the heart is not recorded
    but the count is still returned (no-op toggle)."""
    _ensure_schema_once()
    db = _dbmod._db()
    existing = get_template(template_id)
    if not existing:
        raise KeyError(template_id)
    if not user_id:
        # Anonymous can't heart — return current count, hearted=False.
        return (False, int(existing.get("hearts") or 0))
    # Per-user toggle (shared favorites table — same logic for workspaces).
    hearted, _ = _favs.heart(user_id, "template", template_id)
    # Bump the local SQLite aggregate count (per-Space cache).
    try:
        with _write_lock:
            if hearted:
                db.execute(
                    "UPDATE templates SET hearts = hearts + 1 WHERE id = ?",
                    (template_id,))
            else:
                db.execute(
                    "UPDATE templates SET hearts = MAX(hearts - 1, 0) WHERE id = ?",
                    (template_id,))
            db.commit()
            row = db.execute(
                "SELECT hearts FROM templates WHERE id = ?", (template_id,)).fetchone()
        count = int(row["hearts"]) if row else 0
    except Exception:
        count = int(existing.get("hearts") or 0)
    # Bump the GLOBAL aggregate count in the public HF dataset (best-effort).
    # We attempt this regardless of is_public — if the template isn't in
    # the public dataset yet, we publish it first (publish-on-first-heart)
    # so the global counter can be tracked. System templates are seeded
    # locally but NOT published by default, so without this guard their
    # hearts would never reach the metrics dataset.
    try:
        if hearted:
            _ensure_published_for_counter(existing)
            _pub.add_heart(template_id)
        else:
            _pub.remove_heart(template_id)
    except Exception as exc:
        _log("template_public_heart_failed",
             template_id=template_id, hearted=hearted, error=repr(exc)[:200])
    return (hearted, count)


def download_template(template_id: str, user_id: str | None) -> dict:
    """Download a template: increment its download count (idempotent per
    user) and create a LOCAL COPY for the caller so they have their own
    editable version. Returns the local copy.

    Per-user download tracking lives in the shared ``downloads`` table
    (``favorites.download``). The local SQLite ``templates.downloads``
    column is bumped as a per-Space aggregate cache. The GLOBAL aggregate
    count lives in the public HF dataset
    (``public_dataset.increment_downloads``) so the count persists
    across users AND across Spaces.

    Raises KeyError if the template doesn't exist.
    Raises PermissionError if the template is private and not owned by
    the caller (private templates are only downloadable by their owner)."""
    _ensure_schema_once()
    db = _dbmod._db()
    existing = get_template(template_id)
    if not existing:
        raise KeyError(template_id)
    # Private templates are only downloadable by their owner.
    if not existing.get("is_public") and not _is_owner(existing, user_id):
        raise PermissionError("template is private")
    # Record the download (idempotent per user) in the shared downloads
    # table. Returns True iff this is a NEW download (first time this user
    # has downloaded this item) — only then do we bump the aggregate
    # counts (local cache + global HF dataset).
    is_new_download = False
    if user_id:
        try:
            is_new_download = _favs.download(user_id, "template", template_id)
        except Exception as exc:
            _log("template_fav_download_failed",
                 template_id=template_id, error=repr(exc)[:200])
            is_new_download = False
    if is_new_download:
        # Bump local SQLite aggregate count (per-Space cache).
        try:
            with _write_lock:
                db.execute(
                    "UPDATE templates SET downloads = downloads + 1 WHERE id = ?",
                    (template_id,))
                db.commit()
        except Exception as exc:
            _log("template_local_download_bump_failed",
                 template_id=template_id, error=repr(exc)[:200])
        # Bump the GLOBAL aggregate count in the public HF dataset.
        # Publish-on-first-download: system templates aren't in the dataset
        # by default, so we publish them here so their download count is
        # tracked globally (same pattern as heart_template).
        try:
            _ensure_published_for_counter(existing)
            _pub.increment_downloads(template_id)
        except Exception as exc:
            _log("template_public_download_failed",
                 template_id=template_id, error=repr(exc)[:200])
    # Create a LOCAL COPY for the caller (so they have their own editable
    # version). The copy is private, attributed to the caller, and credits
    # the original via a "(copy of <id>)" suffix on the name + a tag.
    # tags is already a list (parsed by _row_to_dict), so pass it through
    # directly to create_template which re-normalizes it.
    tags_value = existing.get("tags") or []
    if isinstance(tags_value, str):
        try:
            tags_value = json.loads(tags_value)
        except (json.JSONDecodeError, ValueError):
            tags_value = []
    local = create_template(
        user_id=user_id,
        name=f"{existing['name']} (copy)",
        description=existing.get("description"),
        markdown=existing["markdown"],
        kind=existing["kind"],
        tags=tags_value,
        is_public=False,
        author_name=_author_label_for(user_id),
        # Preserve the multi-stage structure (REAL template format) so the
        # local copy is a fully editable pipeline, not just a markdown blob.
        task_type=existing.get("task_type"),
        task=existing.get("task"),
        stages=existing.get("stages") or [],
        output_rules_obj=existing.get("output_rules_obj") or {},
        # Preserve the legacy single-role fields too (for older templates).
        role=existing.get("role"),
        instructions=existing.get("instructions"),
        output_rules=existing.get("output_rules"),
        inputs=existing.get("inputs"),
        template=existing.get("template"),
        required_schema=existing.get("required_schema"))
    return local


def publish_template(template_id: str, user_id: str | None) -> dict | None:
    """Publish a template to the global library (owner only). Returns the
    updated template or None if not found / not owned.

    Pushes the template to the public HF dataset (best-effort — silent
    no-op if the dataset is unreachable, in which case the local SQLite
    row is still marked is_public=1 and will be served via the local
    fallback in ``list_public_templates``)."""
    tpl = update_template(template_id, user_id, is_public=True)
    if tpl is None:
        return None
    # Best-effort push to the public HF dataset. Failures are logged but
    # do not affect the local publish (the local SQLite row is already
    # is_public=1; the public dataset will catch up on the next publish
    # attempt or via a future sync job).
    try:
        _pub.publish_template(tpl)
    except Exception as exc:
        _log("template_publish_to_public_failed",
             template_id=template_id, error=repr(exc)[:200])
    return tpl


def unpublish_template(template_id: str, user_id: str | None) -> dict | None:
    """Unpublish a template from the global library (owner only).

    Removes the template from the public HF dataset (best-effort)."""
    tpl = update_template(template_id, user_id, is_public=False)
    if tpl is None:
        return None
    try:
        _pub.unpublish_template(template_id)
    except Exception as exc:
        _log("template_unpublish_from_public_failed",
             template_id=template_id, error=repr(exc)[:200])
    return tpl


def _is_owner(template: dict, user_id: str | None) -> bool:
    """True if the caller owns the template. System templates
    (author_id='system') are not owned by any user (only the system can
    modify them, which we don't expose via HTTP)."""
    if not user_id:
        return False
    return template.get("author_id") == user_id


def _author_label_for(user_id: str | None) -> str:
    """Best-effort display name for a user_id. Falls back to 'you' for
    authenticated users and 'anonymous' for unauthenticated."""
    if not user_id:
        return "anonymous"
    try:
        user = _dbmod.get_user(user_id)
        if user:
            uname = (user.get("github_username") or user.get("name")
                     or user.get("email") or "").strip()
            if uname:
                return uname
    except Exception:
        pass
    return "you"


# ---------------------------------------------------------------------------
# Default template catalog (seeded idempotently on first use)
# ---------------------------------------------------------------------------


DEFAULT_TEMPLATES: list[dict] = [
    # 1. research_paper — 10-stage research pipeline (from reference repo)
    {
        "name": "Research Paper",
        "description": "10-stage research pipeline: parse outline, discover peer-reviewed URLs, verify URLs, plan sub-questions, draft grounded sections with strict citation rules, intro, conclusion, references, uncertainties, assemble. Best for multi-section research papers with real source grounding.",
        "task_type": "research_paper",
        "task": "research paper",
        "kind": "deepresearch",
        "tags": ["research", "paper", "citations", "multi-stage"],
        "stages": [
            {"name": "outline", "role": "parser", "instructions": "From the user prompt, extract the numbered topic list as JSON with the EXACT shape {\"topics\": [{\"name\": str, \"scope\": str, \"target_words\": int}, ...]}. EVERY topic object MUST contain all three keys. If the prompt has no numbered list, infer 4-6 topics from the prompt's headings or central themes.", "inputs": ["prompt"], "max_tokens": 1000},
            {"name": "source_discovery", "role": "planner", "instructions": "For this topic, identify 1-10 specific URLs pointing to peer-reviewed articles, academic papers, or authoritative sources. Output JSON array: [{\"url\": str, \"title\": str, \"relevance\": str}]. DO NOT invent URLs. Prefer .edu, .gov, arxiv.org, nature.com, science.org. If uncertain, OMIT it.", "fanout": {"over": "outline.topics", "max_parallel": 3}, "inputs": ["outline.topics.{i}"], "max_tokens": 1200},
            {"name": "url_verification", "role": "parser", "instructions": "Verify and filter proposed URLs against fetched_sources. Output JSON: {\"verified\": [...], \"dropped\": [...]}. Reasons for dropping: not_fetched, empty_content, paywall_detected, unreachable.", "inputs": ["source_discovery.*", "fetched_sources"], "max_tokens": 1500},
            {"name": "research_plan", "role": "planner", "instructions": "For this single topic, list 3-5 specific sub-questions worth researching to write 600-800 words. Output JSON: [{\"question\": str, \"why\": str}]. Use VERIFIED sources to guide questions.", "fanout": {"over": "outline.topics", "max_parallel": 3}, "inputs": ["outline.topics.{i}", "url_verification.verified"], "max_tokens": 800},
            {"name": "section_draft", "role": "generator", "instructions": "Draft 600-800 words on the given topic. Ground every factual claim in fetched_sources. Cite sources by number: [1]. NEVER invent citations. Begin with '## <topic name>'. End with '### Open questions:'.", "fanout": {"over": "outline.topics", "max_parallel": 3}, "inputs": ["prompt", "fetched_sources", "url_verification.verified", "outline.topics.{i}", "research_plan.{i}"], "max_tokens": 2500},
            {"name": "intro", "role": "generator", "instructions": "Write a 200-250 word introduction. Frame the topic, motivate why it matters, end with a one-sentence summary. Begin with '# <derived title>'.", "inputs": ["outline.topics.*", "url_verification.verified", "prompt"], "max_tokens": 600},
            {"name": "conclusion", "role": "generator", "instructions": "Write a 150-200 word conclusion synthesizing findings. Identify 2-3 cross-cutting takeaways. Begin with '## Conclusion'. No new claims.", "inputs": ["outline.topics.*", "section_draft.*"], "max_tokens": 500},
            {"name": "references", "role": "generator", "instructions": "Build a '## References' section listing exactly the sources from url_verification.verified. Format: [N] Author. \"Title\". Year. URL. Do NOT add new sources.", "inputs": ["url_verification.verified"], "max_tokens": 1500},
            {"name": "uncertainties_synthesis", "role": "generator", "instructions": "Build a '## What I am not sure about' section listing 3-5 substantive open questions. Synthesize cross-cutting themes from section Open questions notes.", "inputs": ["outline.topics.*", "section_draft.*"], "max_tokens": 700},
            {"name": "assemble_body", "role": "assembler", "instructions": "Stitch together: intro, section_draft.*, conclusion, references, uncertainties_synthesis. Output the full paper.", "inputs": ["intro", "section_draft.*", "conclusion", "references", "uncertainties_synthesis"]},
        ],
        "output_rules_obj": {"format": "markdown", "min_words": 4000, "max_words": 12000, "required_sections": ["References", "What I am not sure about"], "banned_phrases": ["clearly", "obviously", "state-of-the-art", "industry standard", "everyone knows"], "tone": "rigorous, hedged, attribution-heavy"},
    },
    # 2. freeform — single-stage generator (from reference repo)
    {
        "name": "Freeform",
        "description": "Single-stage generator. Best for simple tasks: write an email, draft text, produce markdown from a prompt. One generator stage, no planning or review.",
        "task_type": "freeform",
        "task": "freeform write",
        "kind": "chat",
        "tags": ["freeform", "simple", "generator"],
        "stages": [
            {"name": "write", "role": "generator", "instructions": "Write the content the user asked for, exactly as requested. No preamble, no meta-commentary, no 'here is the content:' header. Begin with the content directly.", "inputs": ["prompt"], "max_tokens": 2000},
        ],
        "output_rules_obj": {"format": "markdown"},
    },
    # 3. structured_lesson — 5-stage lesson pipeline (from reference repo)
    {
        "name": "Structured Lesson",
        "description": "5-stage pipeline: extract params, generate Bloom's-taxonomy objectives, write full lesson with 6 required sections, quality critique, polish with transformer. Enforces exact time-budgeted procedure steps.",
        "task_type": "structured_lesson",
        "task": "structured lesson plan",
        "kind": "deepresearch",
        "tags": ["lesson", "education", "bloom", "multi-stage"],
        "stages": [
            {"name": "extract_params", "role": "parser", "instructions": "From the user prompt, extract JSON: topic (str), audience (str), duration_min (int, infer 30 if not stated), prerequisites (list of str). Emit ONLY the JSON object with these four keys.", "inputs": ["prompt"], "max_tokens": 400},
            {"name": "objectives", "role": "generator", "instructions": "Generate 3-5 measurable learning objectives. Each MUST start with a Bloom's taxonomy action verb (identify, explain, demonstrate, calculate, compare, construct). Output JSON array: [\"Objective 1\", ...].", "inputs": ["prompt", "extract_params"], "max_tokens": 400},
            {"name": "full_lesson", "role": "generator", "instructions": "Write a complete lesson plan with EXACT section headings: Learning Objectives, Prerequisites, Materials, Procedure (numbered steps with [N min] time allocations summing to duration_min), Assessment, Differentiation. Second-person imperative voice.", "inputs": ["prompt", "extract_params", "objectives"], "max_tokens": 4000},
            {"name": "quality_check", "role": "critiquer", "instructions": "Audit the full_lesson: all 6 sections present, objectives start with action verbs, materials are specific, procedure times sum to duration_min, steps are actionable, assessment is concrete. Output bullet list of issues or 'No issues found'.", "inputs": ["full_lesson", "extract_params"], "max_tokens": 800},
            {"name": "polish", "role": "transformer", "instructions": "Apply each fix from quality_check to the full_lesson. Return the FULL polished lesson plan with all sections intact.", "inputs": ["full_lesson", "quality_check", "extract_params"], "max_tokens": 4000},
        ],
        "output_rules_obj": {"format": "markdown", "min_words": 600, "max_words": 5000, "required_sections": ["Learning Objectives", "Prerequisites", "Materials", "Procedure", "Assessment", "Differentiation"], "tone": "instructive, supportive, second-person imperative"},
    },
    # 4. code_review — multi-stage code review pipeline
    {
        "name": "Code Review",
        "description": "5-stage code review: parse structure, identify issues per file (fanout), verify fixes, critique architecture, assemble report. Surfaces bugs, security issues, and improvement suggestions.",
        "task_type": "code_review",
        "task": "code review",
        "kind": "judge",
        "tags": ["code", "review", "bugs", "security"],
        "stages": [
            {"name": "parse_structure", "role": "parser", "instructions": "From the code input, extract a JSON inventory of files/modules: [{\"path\": str, \"language\": str, \"lines\": int, \"summary\": str}]. Identify the entry points and public APIs.", "inputs": ["prompt"], "max_tokens": 1000},
            {"name": "review_file", "role": "critiquer", "instructions": "Review this file for: bugs, security issues (injection, auth, crypto misuse), performance, readability. Output JSON: [{\"severity\": \"high|medium|low\", \"line\": int|null, \"issue\": str, \"fix\": str}]. Be specific — cite line numbers.", "fanout": {"over": "parse_structure.files", "max_parallel": 3}, "inputs": ["parse_structure.files.{i}", "prompt"], "max_tokens": 1500},
            {"name": "verify_fixes", "role": "verifier", "instructions": "For each suggested fix, verify it would actually resolve the issue without introducing regressions. Output JSON: [{\"issue_id\": str, \"verdict\": \"valid|invalid|risky\", \"reason\": str}].", "inputs": ["review_file.*"], "max_tokens": 800},
            {"name": "architecture_review", "role": "critiquer", "instructions": "Critique the overall architecture: coupling, cohesion, separation of concerns, error handling strategy, testability. Output 3-5 bullets, each with ONE issue + ONE concrete improvement.", "inputs": ["parse_structure", "prompt"], "max_tokens": 800},
            {"name": "assemble_report", "role": "assembler", "instructions": "Assemble the final code review report: Executive Summary, File-by-File Issues (sorted by severity), Architecture Notes, Recommended Actions (prioritized).", "inputs": ["parse_structure", "review_file.*", "verify_fixes", "architecture_review"]},
        ],
        "output_rules_obj": {"format": "markdown", "min_words": 400, "required_sections": ["Executive Summary", "File-by-File Issues", "Architecture Notes", "Recommended Actions"], "tone": "constructive, specific, actionable"},
    },
    # 5. brainstorm — idea generation + clustering
    {
        "name": "Brainstorm",
        "description": "3-stage brainstorm: generate diverse ideas, cluster into themes, rank by impact/effort. Best for product features, solutions, naming, content angles.",
        "task_type": "brainstorm",
        "task": "brainstorm",
        "kind": "chat",
        "tags": ["brainstorm", "ideas", "clustering"],
        "stages": [
            {"name": "generate_ideas", "role": "generator", "instructions": "Generate 15-25 diverse ideas for the prompt. Mix obvious and unconventional. Output JSON array: [{\"idea\": str, \"category\": str}]. No duplicates.", "inputs": ["prompt"], "max_tokens": 1500},
            {"name": "cluster", "role": "parser", "instructions": "Cluster the ideas into 3-6 themes. Output JSON: [{\"theme\": str, \"ideas\": [str, ...], \"description\": str}].", "inputs": ["generate_ideas"], "max_tokens": 800},
            {"name": "rank", "role": "critiquer", "instructions": "Rank the themes by impact (high/medium/low) and effort (high/medium/low). Output JSON: [{\"theme\": str, \"impact\": str, \"effort\": str, \"rationale\": str}]. Surface the top 3 quick wins (high impact, low effort).", "inputs": ["cluster"], "max_tokens": 800},
        ],
        "output_rules_obj": {"format": "markdown", "min_words": 200, "required_sections": ["Themes", "Quick Wins"], "tone": "creative, exploratory"},
    },
    # 6. summary — extract key points + write summary
    {
        "name": "Summary",
        "description": "3-stage summarizer: extract key claims, synthesize into a summary, verify no fabricated content. Best for long articles, transcripts, documents.",
        "task_type": "summary",
        "task": "summary",
        "kind": "chat",
        "tags": ["summary", "extract", "synthesize"],
        "stages": [
            {"name": "extract_claims", "role": "extractor", "instructions": "Extract every distinct factual claim from the source. Output JSON array: [{\"claim\": str, \"location\": str}]. Drop opinions.", "inputs": ["prompt"], "max_tokens": 1500},
            {"name": "synthesize", "role": "generator", "instructions": "Write a concise summary (target length: 1/5 of the source). Lead with the main thesis, then supporting points. Preserve every factual claim from extract_claims. Do not invent new content.", "inputs": ["prompt", "extract_claims"], "max_tokens": 1000},
            {"name": "verify", "role": "verifier", "instructions": "Verify each claim in the summary appears in extract_claims. Output JSON: {\"faithful\": bool, \"issues\": [str]}. If any claim is fabricated, list it.", "inputs": ["synthesize", "extract_claims"], "max_tokens": 400},
        ],
        "output_rules_obj": {"format": "markdown", "min_words": 100, "tone": "concise, faithful"},
    },
    # 7. translation — parse + translate + verify
    {
        "name": "Translation",
        "description": "4-stage translation: detect source language, translate preserving meaning/tone, verify key terms, polish for naturalness. Best for documents, UI strings, articles.",
        "task_type": "translation",
        "task": "translation",
        "kind": "chat",
        "tags": ["translation", "localization", "i18n"],
        "stages": [
            {"name": "detect_language", "role": "parser", "instructions": "Detect the source language and identify domain-specific terms (proper nouns, technical jargon, idioms). Output JSON: {\"source_language\": str, \"target_language\": str, \"key_terms\": [{\"source\": str, \"gloss\": str}]}.", "inputs": ["prompt"], "max_tokens": 400},
            {"name": "translate", "role": "generator", "instructions": "Translate the source text into the target language. Preserve meaning, tone, and formatting. Use the gloss from detect_language for key terms. Output the translation only — no commentary.", "inputs": ["prompt", "detect_language"], "max_tokens": 3000},
            {"name": "verify_terms", "role": "verifier", "instructions": "Verify every key term from detect_language was translated consistently. Output JSON: {\"consistent\": bool, \"inconsistencies\": [{\"term\": str, \"variants\": [str]}]}.", "inputs": ["translate", "detect_language"], "max_tokens": 400},
            {"name": "polish", "role": "transformer", "instructions": "Polish the translation for naturalness in the target language. Fix any inconsistencies from verify_terms. Return the FULL polished translation.", "inputs": ["translate", "verify_terms"], "max_tokens": 3000},
        ],
        "output_rules_obj": {"format": "markdown", "tone": "natural, fluent, faithful"},
    },
    # 8. creative_writing — outline + draft + polish
    {
        "name": "Creative Writing",
        "description": "4-stage creative pipeline: outline the arc, draft the piece, critique pacing/voice, polish. Best for short stories, scripts, poems, narrative content.",
        "task_type": "creative_writing",
        "task": "creative writing",
        "kind": "chat",
        "tags": ["creative", "writing", "story", "narrative"],
        "stages": [
            {"name": "outline", "role": "planner", "instructions": "Outline the creative piece: setup, inciting incident, rising action, climax, resolution. Output JSON: [{\"beat\": str, \"purpose\": str, \"target_words\": int}].", "inputs": ["prompt"], "max_tokens": 600},
            {"name": "draft", "role": "generator", "instructions": "Write the full creative piece following the outline. Lead with the strongest opening line. Maintain a consistent voice. No meta-commentary, no 'Here is your story:' preamble.", "inputs": ["prompt", "outline"], "max_tokens": 3000},
            {"name": "critique", "role": "critiquer", "instructions": "Critique the draft: pacing, voice consistency, show-don't-tell, dialogue naturalness, ending payoff. Output 3-5 bullets, each ONE issue + ONE specific fix.", "inputs": ["draft", "outline"], "max_tokens": 600},
            {"name": "polish", "role": "transformer", "instructions": "Apply the critique fixes to the draft. Return the FULL polished piece. Preserve every plot point; only improve prose, pacing, voice.", "inputs": ["draft", "critique"], "max_tokens": 3000},
        ],
        "output_rules_obj": {"format": "markdown", "min_words": 400, "tone": "vivid, voice-consistent, no meta-commentary"},
    },
    # 9. repo_audit — security audit pipeline
    {
        "name": "Repository Audit",
        "description": "5-stage security audit: inventory attack surface, audit each surface for vulnerabilities (fanout), verify findings, prioritize by risk, assemble report. Best for security reviews.",
        "task_type": "repo_audit",
        "task": "repository security audit",
        "kind": "judge",
        "tags": ["security", "audit", "vulnerabilities", "redteam"],
        "stages": [
            {"name": "inventory", "role": "parser", "instructions": "Inventory the attack surface: endpoints, auth flows, data stores, third-party deps, secrets handling. Output JSON: [{\"component\": str, \"type\": str, \"risk_factors\": [str]}].", "inputs": ["prompt"], "max_tokens": 1500},
            {"name": "audit_component", "role": "critiquer", "instructions": "Audit this component for OWASP Top 10 + common vulns (injection, auth bypass, SSRF, secrets-in-code, insecure deserialization). Output JSON: [{\"vuln\": str, \"severity\": \"critical|high|medium|low\", \"evidence\": str, \"fix\": str}]. Cite specific code.", "fanout": {"over": "inventory.components", "max_parallel": 3}, "inputs": ["inventory.components.{i}", "prompt"], "max_tokens": 1500},
            {"name": "verify_findings", "role": "verifier", "instructions": "Verify each finding is real (not a false positive) by checking the evidence. Output JSON: [{\"finding_id\": str, \"verdict\": \"confirmed|false_positive|needs_context\", \"reason\": str}].", "inputs": ["audit_component.*"], "max_tokens": 800},
            {"name": "prioritize", "role": "critiquer", "instructions": "Prioritize confirmed findings by exploitability x impact. Output JSON: [{\"finding_id\": str, \"priority\": \"P0|P1|P2|P3\", \"rationale\": str}].", "inputs": ["verify_findings", "audit_component.*"], "max_tokens": 600},
            {"name": "assemble_report", "role": "assembler", "instructions": "Assemble the security audit report: Executive Summary, Findings (sorted by priority), Remediation Plan (per-finding fix + owner suggestion), Risk Posture.", "inputs": ["inventory", "audit_component.*", "verify_findings", "prioritize"]},
        ],
        "output_rules_obj": {"format": "markdown", "min_words": 500, "required_sections": ["Executive Summary", "Findings", "Remediation Plan", "Risk Posture"], "banned_phrases": ["clearly", "obviously"], "tone": "objective, evidence-based, prioritized"},
    },
    # 10. design_doc — design document pipeline
    {
        "name": "Design Document",
        "description": "5-stage design doc: extract requirements, propose architecture, identify trade-offs, critique for gaps, assemble final design doc. Best for feature/system design.",
        "task_type": "design_doc",
        "task": "design document",
        "kind": "deepresearch",
        "tags": ["design", "architecture", "spec", "planning"],
        "stages": [
            {"name": "extract_requirements", "role": "parser", "instructions": "From the prompt, extract functional + non-functional requirements. Output JSON: {\"functional\": [{\"id\": str, \"requirement\": str, \"acceptance\": str}], \"non_functional\": [{\"category\": \"scalability|security|performance|reliability\", \"requirement\": str}].", "inputs": ["prompt"], "max_tokens": 1000},
            {"name": "architecture", "role": "planner", "instructions": "Propose an architecture: components, data flow, storage, APIs, deployment. Output JSON: {\"components\": [{\"name\": str, \"responsibility\": str, \"tech\": str}], \"data_flow\": str, \"storage\": str, \"apis\": [str]}.", "inputs": ["prompt", "extract_requirements"], "max_tokens": 1500},
            {"name": "tradeoffs", "role": "critiquer", "instructions": "Identify 3-5 key trade-offs in the architecture (build vs buy, consistency vs availability, latency vs cost). For each: the choice made, the alternative, and why this choice. Output JSON array.", "inputs": ["architecture", "extract_requirements"], "max_tokens": 800},
            {"name": "gap_review", "role": "critiquer", "instructions": "Review for gaps: missing error handling, no rollback plan, no observability, no capacity planning, security blind spots. Output 3-5 bullets, each ONE gap + ONE concrete addition.", "inputs": ["architecture", "tradeoffs", "extract_requirements"], "max_tokens": 800},
            {"name": "assemble_doc", "role": "assembler", "instructions": "Assemble the final design doc: Overview, Requirements, Architecture, Trade-offs, Open Questions, Appendix (data models, API contracts).", "inputs": ["extract_requirements", "architecture", "tradeoffs", "gap_review"]},
        ],
        "output_rules_obj": {"format": "markdown", "min_words": 800, "required_sections": ["Overview", "Requirements", "Architecture", "Trade-offs", "Open Questions"], "tone": "precise, decision-oriented, evidence-based"},
    },
    # 11. redteam — red team attack pipeline
    {
        "name": "Red Team",
        "description": "5-stage red team: identify attack surface, generate attacks per surface (fanout), verify attacks are feasible, prioritize by severity, assemble red team report. Best for offensive security testing.",
        "task_type": "redteam",
        "task": "red team assessment",
        "kind": "judge",
        "tags": ["redteam", "offensive-security", "attack", "testing"],
        "stages": [
            {"name": "attack_surface", "role": "parser", "instructions": "Identify the attack surface: entry points, trust boundaries, privileged operations, data flows. Output JSON: [{\"surface\": str, \"exposure\": \"public|internal|admin\", \"auth_required\": bool}].", "inputs": ["prompt"], "max_tokens": 1000},
            {"name": "generate_attacks", "role": "generator", "instructions": "For this attack surface, generate 3-5 plausible attacks (NOT exploit code — describe the attack vector). Output JSON: [{\"attack\": str, \"vector\": str, \"preconditions\": str, \"impact\": str}]. Be specific to the surface.", "fanout": {"over": "attack_surface.surfaces", "max_parallel": 3}, "inputs": ["attack_surface.surfaces.{i}", "prompt"], "max_tokens": 1200},
            {"name": "verify_attacks", "role": "verifier", "instructions": "For each attack, assess feasibility: are the preconditions realistic? Is the impact accurately characterized? Output JSON: [{\"attack_id\": str, \"feasible\": bool, \"confidence\": \"high|medium|low\", \"reason\": str}].", "inputs": ["generate_attacks.*"], "max_tokens": 800},
            {"name": "prioritize", "role": "critiquer", "instructions": "Rank feasible attacks by severity (CVSS-like: critical/high/medium/low). Output JSON: [{\"attack_id\": str, \"severity\": str, \"priority\": \"P0|P1|P2|P3\", \"rationale\": str}].", "inputs": ["verify_attacks", "generate_attacks.*"], "max_tokens": 600},
            {"name": "assemble_report", "role": "assembler", "instructions": "Assemble the red team report: Executive Summary, Attack Surface, Attacks (sorted by severity), Recommended Mitigations (per attack), Residual Risk.", "inputs": ["attack_surface", "generate_attacks.*", "verify_attacks", "prioritize"]},
        ],
        "output_rules_obj": {"format": "markdown", "min_words": 500, "required_sections": ["Executive Summary", "Attack Surface", "Attacks", "Recommended Mitigations", "Residual Risk"], "banned_phrases": ["clearly", "obviously", "trivially"], "tone": "adversarial, specific, evidence-based"},
    },
    # 12. panel_debate — multi-perspective debate
    {
        "name": "Panel Debate",
        "description": "4-stage debate: assign perspectives, generate opening arguments per perspective (fanout), generate rebuttals, synthesize. Best for exploring contentious topics from multiple angles.",
        "task_type": "panel_debate",
        "task": "panel debate",
        "kind": "judge",
        "tags": ["debate", "panel", "perspectives", "multi-view"],
        "stages": [
            {"name": "assign_perspectives", "role": "planner", "instructions": "Assign 3-4 distinct perspectives on the topic (e.g. pro/con/synthesist/skeptic, or domain-specific roles). Output JSON: [{\"perspective\": str, \"stance\": str, \"key_values\": [str]}].", "inputs": ["prompt"], "max_tokens": 600},
            {"name": "opening_arguments", "role": "generator", "instructions": "Write a 300-500 word opening argument from THIS perspective. Steelman the position. Cite evidence. No strawmen. Begin with '## <perspective>: Opening'.", "fanout": {"over": "assign_perspectives.perspectives", "max_parallel": 4}, "inputs": ["prompt", "assign_perspectives.perspectives.{i}"], "max_tokens": 1000},
            {"name": "rebuttals", "role": "critiquer", "instructions": "For each perspective, write a 150-200 word rebuttal to the OTHER perspectives' arguments. Address specific points, not strawmen. Begin with '## <perspective>: Rebuttal'.", "fanout": {"over": "assign_perspectives.perspectives", "max_parallel": 4}, "inputs": ["assign_perspectives.perspectives.{i}", "opening_arguments.*"], "max_tokens": 600},
            {"name": "synthesize", "role": "assembler", "instructions": "Synthesize the debate: where do perspectives agree? Where do they irreconcilably differ? What are the cruxes? Output a balanced synthesis that does NOT declare a winner but maps the disagreement.", "inputs": ["assign_perspectives", "opening_arguments.*", "rebuttals.*"], "max_tokens": 800},
        ],
        "output_rules_obj": {"format": "markdown", "min_words": 800, "required_sections": ["Opening Arguments", "Rebuttals", "Synthesis"], "banned_phrases": ["clearly", "obviously"], "tone": "balanced, steelman, no-winner-declared"},
    },
    # 13. fact_check — claim extraction + verification
    {
        "name": "Fact Check",
        "description": "4-stage fact checker: extract claims, verify each claim (fanout), assess source credibility, assemble verdict. Best for articles, social media posts, political claims.",
        "task_type": "fact_check",
        "task": "fact check",
        "kind": "judge",
        "tags": ["fact-check", "verify", "claims", "verification"],
        "stages": [
            {"name": "extract_claims", "role": "extractor", "instructions": "Extract every distinct factual claim from the source. Output JSON: [{\"claim\": str, \"location\": str, \"claim_type\": \"statistic|quote|causal|prediction|definition\"}]. Drop opinions.", "inputs": ["prompt"], "max_tokens": 1000},
            {"name": "verify_claim", "role": "verifier", "instructions": "Verify this single claim against fetched_sources. Output JSON: {\"claim\": str, \"verdict\": \"true|false|misleading|unverifiable\", \"evidence\": str, \"confidence\": \"high|medium|low\"}. Cite specific sources.", "fanout": {"over": "extract_claims.claims", "max_parallel": 4}, "inputs": ["extract_claims.claims.{i}", "fetched_sources"], "max_tokens": 500},
            {"name": "assess_credibility", "role": "critiquer", "instructions": "Assess the source's overall credibility: methodology, citation quality, potential bias. Output JSON: {\"credibility\": \"high|medium|low\", \"biases\": [str], \"missing_context\": [str]}.", "inputs": ["prompt", "extract_claims", "verify_claim.*"], "max_tokens": 600},
            {"name": "assemble_verdict", "role": "assembler", "instructions": "Assemble the fact-check report: Overall Verdict (true/mixed/false), Claim-by-Claim Verdicts, Source Credibility, Missing Context. Include a one-line summary at the top.", "inputs": ["extract_claims", "verify_claim.*", "assess_credibility"]},
        ],
        "output_rules_obj": {"format": "markdown", "min_words": 300, "required_sections": ["Overall Verdict", "Claim-by-Claim Verdicts", "Source Credibility"], "tone": "neutral, evidence-based, hedged appropriately"},
    },
    # 14. lesson_plan — simpler lesson plan (vs structured_lesson)
    {
        "name": "Lesson Plan",
        "description": "4-stage lesson planner: extract topic/audience, list learning objectives, write the lesson procedure, assemble. Lighter than Structured Lesson — no critique/polish loop.",
        "task_type": "lesson_plan",
        "task": "lesson plan",
        "kind": "deepresearch",
        "tags": ["lesson", "education", "teaching"],
        "stages": [
            {"name": "extract_topic", "role": "parser", "instructions": "From the prompt, extract: topic (str), audience (str), duration_min (int, default 30), prerequisites (list of str). Output JSON.", "inputs": ["prompt"], "max_tokens": 300},
            {"name": "objectives", "role": "generator", "instructions": "List 3-5 learning objectives starting with action verbs (explain, identify, demonstrate, calculate). Output JSON array of strings.", "inputs": ["prompt", "extract_topic"], "max_tokens": 300},
            {"name": "procedure", "role": "generator", "instructions": "Write the lesson procedure: hook, direct instruction, guided practice, independent practice, closure. Each step has a time allocation [N min] summing to duration_min. Include 2-3 assessment checks.", "inputs": ["prompt", "extract_topic", "objectives"], "max_tokens": 2000},
            {"name": "assemble", "role": "assembler", "instructions": "Assemble the lesson plan: Topic, Audience, Duration, Objectives, Materials (inferred), Procedure, Assessment. Output as a clean markdown document.", "inputs": ["extract_topic", "objectives", "procedure"]},
        ],
        "output_rules_obj": {"format": "markdown", "min_words": 300, "required_sections": ["Objectives", "Procedure", "Assessment"], "tone": "instructive, second-person imperative"},
    },
]




def seed_defaults() -> int:
    """Seed the default system templates if the templates table is empty.
    Idempotent: only inserts if the table is empty. Returns the number of
    templates inserted (0 if already seeded)."""
    db = _dbmod._db()
    # Check if ANY system template exists (idempotent re-seed guard).
    row = db.execute(
        "SELECT COUNT(*) AS c FROM templates WHERE author_id = 'system'").fetchone()
    if row and int(row["c"]) > 0:
        return 0
    now = _iso_now()
    inserted = 0
    with _write_lock:
        for tpl in DEFAULT_TEMPLATES:
            # Skip if a system template with the same name already exists
            # (defensive — handles partial seeds from a crashed earlier run).
            existing = db.execute(
                "SELECT 1 FROM templates WHERE author_id = 'system' AND name = ?",
                (tpl["name"],)).fetchone()
            if existing:
                continue
            tid = _gen_id()
            # Compile the markdown. Multi-stage templates (with `stages`)
            # use compile_stages_markdown; legacy single-role templates use
            # compile_template_markdown; legacy plain-text use the literal
            # markdown field.
            norm_role = _normalize_role(tpl.get("role"))
            stages_list = tpl.get("stages")
            output_rules_obj = tpl.get("output_rules_obj")
            has_stages = bool(stages_list)
            if has_stages:
                markdown = compile_stages_markdown(
                    task_type=tpl.get("task_type") or "",
                    task=tpl.get("task") or "",
                    description=tpl.get("description") or "",
                    stages=stages_list,
                    output_rules=output_rules_obj or {})
            elif norm_role:
                markdown = compile_template_markdown(
                    role=norm_role,
                    instructions=tpl.get("instructions") or "",
                    output_rules=tpl.get("output_rules") or "",
                    inputs=tpl.get("inputs") or "",
                    template=tpl.get("template") or "",
                    required_schema=tpl.get("required_schema") or "")
            else:
                markdown = tpl.get("markdown", "")
            # Normalize multi-stage fields for storage.
            stages_json = _normalize_stages(stages_list) if has_stages else "[]"
            out_rules_json = (_normalize_output_rules(output_rules_obj)
                              if output_rules_obj is not None else "{}")
            db.execute(
                "INSERT INTO templates (id, author_id, author_name, name, description, "
                "markdown, kind, tags, is_public, hearts, downloads, "
                "role, instructions, output_rules, inputs, template_part, required_schema, "
                "task_type, task, stages_json, output_rules_json, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (tid, "system", "doomalaysocreate", tpl["name"],
                 tpl.get("description"), markdown, tpl.get("kind", "custom"),
                 _normalize_tags(tpl.get("tags")),
                 norm_role,
                 (tpl.get("instructions") or "").strip() or None,
                 (tpl.get("output_rules") or "").strip() or None,
                 (tpl.get("inputs") or "").strip() or None,
                 (tpl.get("template") or "").strip() or None,
                 (tpl.get("required_schema") or "").strip() or None,
                 (tpl.get("task_type") or "").strip() or None,
                 (tpl.get("task") or "").strip() or None,
                 stages_json,
                 out_rules_json,
                 now, now))
            inserted += 1
        db.commit()
    return inserted


def list_default_templates(*, kind: str | None = None) -> list[dict]:
    """Return the system default templates, optionally filtered by kind.
    Used by /api/roster to populate the tool popovers in the frontend."""
    _ensure_schema_once()
    db = _dbmod._db()
    if kind and kind in VALID_KINDS:
        rows = db.execute(
            "SELECT * FROM templates WHERE author_id = 'system' AND is_public = 1 "
            "AND kind = ? ORDER BY name ASC",
            (kind,)).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM templates WHERE author_id = 'system' AND is_public = 1 "
            "ORDER BY kind ASC, name ASC").fetchall()
    return [_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# HTTP dispatch — called from critique_service.Handler
# ---------------------------------------------------------------------------

def _user_id_from_handler(handler) -> str | None:
    """Extract GitHub user_id from the request handler (X-JWT or fallback)."""
    try:
        return handler._require_user_from_jwt()
    except Exception:
        return None


def _json(handler, status: int, body: dict) -> None:
    handler._send_json(status, body)


def _parse_path(path: str) -> tuple[str, str, str]:
    """Parse a /api/templates* path into (route, template_id, sub_action).

    Examples:
      /api/templates            -> ("/api/templates", "", "")
      /api/templates/explore    -> ("/api/templates/explore", "", "")
      /api/templates/abc123     -> ("/api/templates/<id>", "abc123", "")
      /api/templates/abc123/heart -> ("/api/templates/<id>/<sub>", "abc123", "heart")
    """
    route = urlsplit(path).path.rstrip("/")
    if route == "/api/templates":
        return ("/api/templates", "", "")
    if route == "/api/templates/explore":
        return ("/api/templates/explore", "", "")
    if route.startswith("/api/templates/"):
        rest = route[len("/api/templates/"):]
        parts = rest.split("/")
        if len(parts) == 1:
            return ("/api/templates/<id>", parts[0], "")
        if len(parts) == 2:
            return ("/api/templates/<id>/<sub>", parts[0], parts[1])
    return ("", "", "")


def handle_request(method: str, path: str, body: dict, handler) -> bool:
    """Dispatch /api/templates* routes. Returns True if handled."""
    route, tid, sub = _parse_path(path)
    if not route:
        return False
    # Auth: same bearer gate as /api/agent.
    if not handler._auth_ok():
        _json(handler, 401, {"error": "missing or invalid bearer token"})
        return True
    user_id = _user_id_from_handler(handler)

    # -----------------------------------------------------------------
    # GET routes
    # -----------------------------------------------------------------
    if method == "GET":
        if route == "/api/templates":
            q = parse_qs(urlsplit(path).query)
            filter = (q.get("filter", [None])[0] or "").strip().lower() or None
            tpls = list_my_templates(user_id, filter=filter)
            _json(handler, 200, {"templates": tpls})
            return True
        if route == "/api/templates/explore":
            q = parse_qs(urlsplit(path).query)
            sort = (q.get("sort", ["hearts"])[0] or "hearts").strip().lower()
            query = (q.get("query", [None])[0] or "").strip() or None
            kind = (q.get("kind", [None])[0] or "").strip() or None
            role = (q.get("role", [None])[0] or "").strip().lower() or None
            filter = (q.get("filter", [None])[0] or "").strip().lower() or None
            try:
                limit = int(q.get("limit", ["50"])[0])
            except ValueError:
                limit = 50
            try:
                offset = int(q.get("offset", ["0"])[0])
            except ValueError:
                offset = 0
            tpls, total = list_public_templates(
                sort=sort, query=query, kind=kind, limit=limit, offset=offset,
                user_id=user_id, filter=filter, role=role)
            _json(handler, 200, {"templates": tpls, "total": total})
            return True
        if route == "/api/templates/<id>":
            tpl = get_template(tid)
            if tpl is None:
                _json(handler, 404, {"error": f"no such template {tid!r}"})
                return True
            # Visibility: private templates are only visible to their owner.
            if not tpl.get("is_public") and not _is_owner(tpl, user_id):
                _json(handler, 404, {"error": f"no such template {tid!r}"})
                return True
            # Annotate with per-user hearted/downloaded flags (Bug 4) so the
            # frontend can render the correct button state when a user opens
            # a template detail view directly via URL.
            _annotate_user_flags([tpl], user_id)
            # Raw view: ?view=raw returns the full template structure.
            # For MULTI-STAGE templates (the REAL template format from the
            # reference repo), this returns {task_type, task, description,
            # stages, output_rules} so the frontend can render the pipeline
            # with stage cards, fanout indicators, etc.
            # For LEGACY single-role templates, it returns the role skeleton
            # + the parts (backward compat with the Task-4 role-based raw view).
            q = parse_qs(urlsplit(path).query)
            view = (q.get("view", [None])[0] or "").strip().lower()
            if view == "raw":
                if tpl.get("stages"):
                    # Multi-stage template: return the full JSON pipeline.
                    _json(handler, 200, {
                        "template": tpl,
                        "raw": {
                            "task_type": tpl.get("task_type"),
                            "task": tpl.get("task"),
                            "description": tpl.get("description") or "",
                            "stages": tpl.get("stages") or [],
                            "output_rules": tpl.get("output_rules_obj") or {},
                        },
                    })
                    return True
                # Legacy single-role template: return the role skeleton + parts.
                skeleton = _role_skeleton(tpl.get("role") or "")
                _json(handler, 200, {
                    "template": tpl,
                    "raw": {
                        "role": tpl.get("role"),
                        "skeleton": skeleton,
                        "instructions": tpl.get("instructions") or "",
                        "output_rules": tpl.get("output_rules") or "",
                        "inputs": tpl.get("inputs") or "",
                        "template": tpl.get("template") or "",
                        "required_schema": tpl.get("required_schema") or "",
                    },
                })
                return True
            _json(handler, 200, {"template": tpl})
            return True
        _json(handler, 404, {"error": "unknown GET route"})
        return True

    # -----------------------------------------------------------------
    # POST routes
    # -----------------------------------------------------------------
    if method == "POST":
        if route == "/api/templates":
            name = str(body.get("name", "")).strip()
            if not name:
                _json(handler, 400, {"error": "'name' (non-empty string) is required"})
                return True
            # Role-based system (Task 4): accept role + parts as an
            # alternative to the legacy plain-text markdown. If `role` is
            # provided, the markdown is compiled from the parts.
            role = body.get("role")
            instructions = body.get("instructions")
            output_rules = body.get("output_rules")
            inputs = body.get("inputs")
            template = body.get("template")
            required_schema = body.get("required_schema")
            # Multi-stage template fields (REAL template format):
            # task_type, task, stages, output_rules_obj. If `stages` is
            # provided, the markdown is compiled from the stages.
            task_type = body.get("task_type")
            task = body.get("task")
            stages = body.get("stages")
            output_rules_obj = body.get("output_rules_obj") or body.get("output_rules_json")
            markdown = body.get("markdown")
            norm_role = _normalize_role(role) if role else None
            has_stages = isinstance(stages, list) and bool(stages)
            if not has_stages and not norm_role:
                # Legacy plain-text mode: markdown is required.
                if not isinstance(markdown, str) or not markdown.strip():
                    _json(handler, 400, {"error": "'markdown' (or 'role' + parts or 'stages') is required"})
                    return True
            kind = str(body.get("kind", "custom")).strip().lower()
            if kind not in VALID_KINDS:
                _json(handler, 400, {"error": f"'kind' must be one of {VALID_KINDS}"})
                return True
            description = body.get("description")
            if description is not None and not isinstance(description, str):
                _json(handler, 400, {"error": "'description' must be a string"})
                return True
            tags = body.get("tags")
            is_public = bool(body.get("is_public", False))
            try:
                tpl = create_template(
                    user_id=user_id, name=name, description=description,
                    markdown=markdown, kind=kind, tags=tags, is_public=is_public,
                    role=norm_role, instructions=instructions,
                    output_rules=output_rules, inputs=inputs,
                    template=template, required_schema=required_schema,
                    task_type=task_type, task=task, stages=stages,
                    output_rules_obj=output_rules_obj)
            except ValueError as e:
                _json(handler, 400, {"error": str(e)})
                return True
            _json(handler, 201, {"template": tpl})
            return True
        if route == "/api/templates/<id>/<sub>":
            if sub == "heart":
                try:
                    hearted, hearts = heart_template(tid, user_id)
                except KeyError:
                    _json(handler, 404, {"error": f"no such template {tid!r}"})
                    return True
                _json(handler, 200, {"hearted": hearted, "hearts": hearts})
                return True
            if sub == "download":
                # Fetch the template first to enforce visibility.
                existing = get_template(tid)
                if existing is None:
                    _json(handler, 404, {"error": f"no such template {tid!r}"})
                    return True
                if not existing.get("is_public") and not _is_owner(existing, user_id):
                    _json(handler, 404, {"error": f"no such template {tid!r}"})
                    return True
                try:
                    local_copy = download_template(tid, user_id)
                except KeyError:
                    _json(handler, 404, {"error": f"no such template {tid!r}"})
                    return True
                except PermissionError:
                    _json(handler, 403, {"error": "template is private"})
                    return True
                _json(handler, 200, {"template": local_copy})
                return True
            if sub == "publish":
                tpl = publish_template(tid, user_id)
                if tpl is None:
                    existing = get_template(tid)
                    if existing is None:
                        _json(handler, 404, {"error": f"no such template {tid!r}"})
                    else:
                        _json(handler, 403, {"error": "only the owner can publish"})
                    return True
                _json(handler, 200, {"template": tpl})
                return True
            if sub == "unpublish":
                tpl = unpublish_template(tid, user_id)
                if tpl is None:
                    existing = get_template(tid)
                    if existing is None:
                        _json(handler, 404, {"error": f"no such template {tid!r}"})
                    else:
                        _json(handler, 403, {"error": "only the owner can unpublish"})
                    return True
                _json(handler, 200, {"template": tpl})
                return True
            _json(handler, 404, {"error": f"unknown POST sub-route {sub!r}"})
            return True
        _json(handler, 404, {"error": "unknown POST route"})
        return True

    # -----------------------------------------------------------------
    # PATCH routes
    # -----------------------------------------------------------------
    if method == "PATCH":
        if route == "/api/templates/<id>":
            # Build the update kwargs from the body (only owner can update).
            kwargs: dict[str, Any] = {}
            for k in ("name", "description", "markdown", "kind", "tags"):
                if k in body:
                    kwargs[k] = body[k]
            if "is_public" in body:
                kwargs["is_public"] = bool(body["is_public"])
            # Role-based parts (legacy single-role templates).
            for k in ("role", "instructions", "output_rules", "inputs",
                      "template", "required_schema"):
                if k in body:
                    kwargs[k] = body[k]
            # Multi-stage template fields (REAL template format).
            for k in ("task_type", "task", "stages"):
                if k in body:
                    kwargs[k] = body[k]
            # output_rules_obj comes through as "output_rules_obj" or
            # "output_rules_json" in the request body.
            if "output_rules_obj" in body:
                kwargs["output_rules_obj"] = body["output_rules_obj"]
            elif "output_rules_json" in body:
                kwargs["output_rules_obj"] = body["output_rules_json"]
            if not kwargs:
                _json(handler, 400, {"error": "no updatable fields supplied"})
                return True
            try:
                tpl = update_template(tid, user_id, **kwargs)
            except ValueError as e:
                _json(handler, 400, {"error": str(e)})
                return True
            if tpl is None:
                existing = get_template(tid)
                if existing is None:
                    _json(handler, 404, {"error": f"no such template {tid!r}"})
                else:
                    _json(handler, 403, {"error": "only the owner can update"})
                return True
            _json(handler, 200, {"template": tpl})
            return True
        _json(handler, 404, {"error": "unknown PATCH route"})
        return True

    # -----------------------------------------------------------------
    # DELETE routes
    # -----------------------------------------------------------------
    if method == "DELETE":
        if route == "/api/templates/<id>":
            try:
                result = delete_template(tid, user_id)
            except KeyError:
                _json(handler, 404, {"error": f"no such template {tid!r}"})
                return True
            if result is None:
                _json(handler, 403, {"error": "only the owner can delete"})
                return True
            if not result:
                _json(handler, 404, {"error": f"no such template {tid!r}"})
                return True
            _json(handler, 200, {"ok": True})
            return True
        _json(handler, 404, {"error": "unknown DELETE route"})
        return True

    _json(handler, 405, {"error": f"method {method} not allowed"})
    return True
