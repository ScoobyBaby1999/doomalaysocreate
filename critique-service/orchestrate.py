from __future__ import annotations
import dataclasses
import json
import os
import re
import threading
from pathlib import Path

import artifacts
from oplog import log_event
from orchestrator.schematic import (
    SchemaValidationError,
    TaskSchematic,
    from_json_text,
    parse_and_validate,
)

# Service-side glue for the multi-stage orchestrator:
#   - TemplateStore: built-in templates (read-only) + user-authored templates
#     (validated, persisted to a private HF Dataset so they survive the ephemeral
#     Space FS, like metrics).
#   - resolve_schematic(): turn a request into a validated TaskSchematic (inline
#     schematic > stored template > planner "auto" > freeform fallback).
#   - serialize_run_result(): shape a RunResult for the HTTP layer.

HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = Path(os.environ.get("TEMPLATES_DIR", HERE / "orchestrator" / "templates"))
USER_TEMPLATES_DIR = Path(os.environ.get("USER_TEMPLATES_DIR", HERE / "data" / "templates"))

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class OrchestrateError(ValueError):
    """invalid /api/run or /api/templates request -> 400."""


def _load_commented(text: str) -> dict:
    no_comments = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("//"))
    return json.loads(no_comments)


def _schematic_summary(tid: str, schem: TaskSchematic, source: str) -> dict:
    jc = schem.judge_config
    return {
        "id": tid, "source": source, "task_type": schem.task_type,
        "stages": [{"name": s.name, "role": s.role.value} for s in schem.stages],
        "output_format": schem.output_rules.get("format", "markdown"),
        "judge": {"rules": len(jc.get("rules", [])), "plugins": jc.get("plugins", []),
                  "llm_judges": len(jc.get("llm_judges", []))},
        "max_rounds": schem.max_rounds,
    }


class TemplateStore:
    """built-in (read-only) + user-authored (persisted) template registry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._user: dict[str, dict] = {}        # id -> schematic dict
        self._hf = _TemplateHFSink()
        try:
            USER_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self._load()

    # --- built-ins ----------------------------------------------------------
    @staticmethod
    def _builtin_paths() -> dict[str, Path]:
        out: dict[str, Path] = {}
        if TEMPLATES_DIR.is_dir():
            for p in sorted(TEMPLATES_DIR.glob("*.json")):
                out[p.stem] = p
        return out

    # --- load/persist user templates ---------------------------------------
    def _load(self) -> None:
        self._hf.download_all(USER_TEMPLATES_DIR)
        loaded = 0
        for f in sorted(USER_TEMPLATES_DIR.glob("*.json")):
            try:
                data = _load_commented(f.read_text(encoding="utf-8"))
                parse_and_validate(data)  # skip invalid on boot
                self._user[f.stem] = data
                loaded += 1
            except (OSError, ValueError):
                continue
        if loaded:
            log_event("templates_loaded", user_templates=loaded)

    # --- public API ---------------------------------------------------------
    def get(self, template_id: str) -> TaskSchematic:
        with self._lock:
            if template_id in self._user:
                return parse_and_validate(self._user[template_id])
        paths = self._builtin_paths()
        if template_id in paths:
            return from_json_text(paths[template_id].read_text(encoding="utf-8"))
        raise OrchestrateError(
            f"unknown template {template_id!r}; available: {sorted(self.ids())}")

    def get_dict(self, template_id: str) -> dict | None:
        with self._lock:
            if template_id in self._user:
                return dict(self._user[template_id])
        paths = self._builtin_paths()
        if template_id in paths:
            return _load_commented(paths[template_id].read_text(encoding="utf-8"))
        return None

    def ids(self) -> list[str]:
        with self._lock:
            return sorted(set(self._builtin_paths()) | set(self._user))

    def save(self, template_id: str, schematic_obj: dict) -> dict:
        if not _ID_RE.match(template_id or ""):
            raise OrchestrateError("template id must be 1-64 chars of [A-Za-z0-9._-]")
        if template_id in self._builtin_paths():
            raise OrchestrateError(f"{template_id!r} is a built-in template (read-only)")
        try:
            schem = parse_and_validate(schematic_obj)
        except SchemaValidationError as e:
            raise OrchestrateError(f"invalid schematic: {e}")
        with self._lock:
            self._user[template_id] = schematic_obj
        path = USER_TEMPLATES_DIR / f"{template_id}.json"
        try:
            path.write_text(json.dumps(schematic_obj, indent=2), encoding="utf-8")
        except OSError as e:
            raise OrchestrateError(f"could not persist template: {e}")
        self._hf.upload(path)
        log_event("template_saved", id=template_id, task_type=schem.task_type)
        return _schematic_summary(template_id, schem, "user")

    def delete(self, template_id: str) -> bool:
        if template_id in self._builtin_paths():
            raise OrchestrateError(f"{template_id!r} is a built-in template (cannot delete)")
        with self._lock:
            existed = self._user.pop(template_id, None) is not None
        if existed:
            try:
                (USER_TEMPLATES_DIR / f"{template_id}.json").unlink(missing_ok=True)
            except OSError:
                pass
            self._hf.delete(f"{template_id}.json")
            log_event("template_deleted", id=template_id)
        return existed

    def list(self) -> list[dict]:
        rows = []
        for tid, path in self._builtin_paths().items():
            try:
                rows.append(_schematic_summary(tid, from_json_text(path.read_text(encoding="utf-8")), "builtin"))
            except SchemaValidationError:
                continue
        with self._lock:
            user = dict(self._user)
        for tid, data in sorted(user.items()):
            try:
                rows.append(_schematic_summary(tid, parse_and_validate(data), "user"))
            except SchemaValidationError:
                continue
        return rows


# minimal built-in fallback when a request gives a prompt but no template/schematic.
_FREEFORM = {
    "task_type": "freeform", "task": "freeform",
    "stages": [{"name": "write", "role": "generator",
                "instructions": "Produce exactly what the user asked for. No preamble, "
                                "no meta-commentary; begin with the content directly.",
                "inputs": ["prompt"], "max_tokens": 16384}],
    "output_rules": {"format": "markdown"},
    "judge_config": {"rules": [], "plugins": [], "llm_judges": []},
    "max_rounds": 1,
}

# sentinel template id that triggers LLM planning from the freeform prompt.
PLAN_ID = "auto"


def _stamp_label(schem: TaskSchematic, prompt: str, label: str) -> TaskSchematic:
    if schem.task.startswith("<FILL") or not schem.task:
        return dataclasses.replace(schem, task=(label or prompt[:60]).strip() or schem.task_type)
    return schem


def _nonce_for(schem: TaskSchematic) -> str:
    return artifacts.make_nonce() if schem.output_rules.get("format") == "files" else ""


def resolve_schematic(store: TemplateStore, prompt: str, *, template_id: str | None,
                      schematic_obj: dict | None, label: str = ""
                      ) -> tuple[TaskSchematic | None, str, bool]:
    #   precedence: inline schematic > "auto" planner > stored/built-in template >
    #   freeform fallback. returns (schematic_or_None, nonce, plan_mode). When
    #   plan_mode is True the schematic is built later by the planner (needs a client).
    if schematic_obj is not None:
        try:
            schem = parse_and_validate(schematic_obj)
        except SchemaValidationError as e:
            raise OrchestrateError(f"invalid 'schematic': {e}")
    elif template_id == PLAN_ID:
        return None, "", True
    elif template_id:
        schem = store.get(template_id)
    else:
        schem = parse_and_validate(_FREEFORM)
    schem = _stamp_label(schem, prompt, label)
    return schem, _nonce_for(schem), False


async def plan_now(prompt: str, scheduler, http_client, *, label: str = "",
                   log=lambda *_: None) -> tuple[TaskSchematic, str]:
    #   LLM planner: freeform prompt -> validated TaskSchematic (never raises;
    #   falls back to a freeform single-stage schematic internally).
    from orchestrator.planner import plan
    schem = await plan(prompt, scheduler, http_client, log=log)
    schem = _stamp_label(schem, prompt, label)
    return schem, _nonce_for(schem)


def serialize_run_result(res, *, task: str) -> dict:
    report = res.final_report
    return {
        "ok": res.ok, "task": task, "body": res.body,
        "artifacts": list(res.artifacts or []),
        "rounds": res.rounds, "decompositions": res.decompositions,
        "judge": {"ok": (report.ok if report is not None else None),
                  "hard_fails": (report.hard_fails if report is not None else []),
                  "soft_flags": (report.soft_flags if report is not None else [])},
        "stages": [{"name": s.name, "role": s.role.value, "provider": s.provider,
                    "family": s.family, "ok": s.ok, "duration_s": s.duration_s,
                    "error": s.error} for s in res.stages],
        "providers_used": res.providers_used, "error": res.error,
    }


class _TemplateHFSink:
    """mirror user templates to templates/ in the private HF Dataset (optional)."""

    def __init__(self) -> None:
        self.repo_id = os.environ.get("TEMPLATES_HF_REPO", os.environ.get("METRICS_PUBLIC_HF_REPO", "")).strip()
        self.token = (os.environ.get("HF_TOKEN", "") or os.environ.get("HUGGINGFACE_TOKEN", "")).strip()
        self._api = None
        self.enabled = False
        if not (self.repo_id and self.token):
            return
        try:
            from huggingface_hub import HfApi
            self._api = HfApi(token=self.token)
            self._api.create_repo(self.repo_id, repo_type="dataset", private=True, exist_ok=True)
            self.enabled = True
            log_event("templates_hf_enabled", repo=self.repo_id)
        except Exception as e:  # noqa: BLE001
            log_event("templates_hf_disabled", reason=repr(e)[:200])

    def upload(self, path: Path) -> None:
        if not self.enabled:
            return
        try:
            self._api.upload_file(path_or_fileobj=str(path),
                                  path_in_repo=f"templates/{path.name}",
                                  repo_id=self.repo_id, repo_type="dataset")
        except Exception as e:  # noqa: BLE001
            log_event("templates_hf_upload_error", file=path.name, error=repr(e)[:160])

    def delete(self, name: str) -> None:
        if not self.enabled:
            return
        try:
            self._api.delete_file(f"templates/{name}", repo_id=self.repo_id, repo_type="dataset")
        except Exception:  # noqa: BLE001 - file may not exist remotely
            pass

    def download_all(self, dest: Path) -> None:
        if not self.enabled:
            return
        try:
            from huggingface_hub import snapshot_download
            snap = snapshot_download(self.repo_id, repo_type="dataset", token=self.token,
                                     allow_patterns="templates/*.json")
            src = Path(snap) / "templates"
            if src.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                for f in src.glob("*.json"):
                    (dest / f.name).write_bytes(f.read_bytes())
        except Exception as e:  # noqa: BLE001
            log_event("templates_hf_download_error", reason=repr(e)[:160])
