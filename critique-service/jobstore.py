from __future__ import annotations
import os
import json
import threading
import time
from pathlib import Path
from typing import Any

from oplog import atomic_write_json, log_event

# Durable job store. A Hugging Face Space filesystem is ephemeral AND a deploy spins
# up a brand-new container, so async jobs (panel critiques, orchestration runs) used to
# die whenever we pushed - root-caused twice in captest (DeepSeek killed at 35 min).
#
# This persists each job dict as data/jobs/<id>.json via atomic write (tmp + rename,
# ported from the timemanager prior art), and mirrors to a private HF Dataset so jobs
# survive a redeploy too. On boot the JobRunner reloads them: panel jobs reschedule any
# unfinished judge; run jobs that were mid-flight are marked interrupted (stage-level
# resume is a later enhancement using the same checkpoint pattern).
#
# Degrades gracefully: no HF repo/token -> local-disk only (survives in-container
# restarts + kill-9, just not a fresh-container deploy). Persistence never breaks a job.

JOBS_DIR = Path(os.environ.get("JOBS_DIR", Path(__file__).resolve().parent / "data" / "jobs"))
JOBS_PERSIST = os.environ.get("JOBS_PERSIST", "1").strip() not in ("0", "false", "no", "")


def _sanitize(job: dict) -> dict:
    #   the job dict is already plain json (dicts/lists/str/num/bool) - the only thing
    #   we must NOT persist is the live `progress` peek (ephemeral, high-churn). drop it.
    out = json.loads(json.dumps(job, default=str))
    for j in (out.get("judges") or {}).values() if isinstance(out.get("judges"), dict) else []:
        j.pop("progress", None)
    return out


class JobStore:
    def __init__(self, dirpath: Path = JOBS_DIR) -> None:
        self.dir = Path(dirpath)
        self.enabled = JOBS_PERSIST
        self._lock = threading.Lock()
        if self.enabled:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                log_event("jobstore_dir_error", error=repr(e)[:200])
                self.enabled = False
        self._hf = _JobHFSink()

    def _path(self, job_id: str) -> Path:
        safe = "".join(c for c in job_id if c.isalnum() or c in "-_") or "job"
        return self.dir / f"{safe}.json"

    def save(self, job: dict) -> None:
        if not self.enabled:
            return
        try:
            path = self._path(job["id"])
            atomic_write_json(path, _sanitize(job))
        except Exception as e:  # noqa: BLE001 - persistence must never break a job
            log_event("jobstore_save_error", job_id=job.get("id"), error=repr(e)[:200])
            return
        self._hf.upload(path)

    def delete(self, job_id: str) -> None:
        if not self.enabled:
            return
        try:
            p = self._path(job_id)
            if p.exists():
                p.unlink()
        except OSError:
            pass
        self._hf.delete(self._path(job_id).name)

    def load_all(self) -> list[dict]:
        #   pull durable copies from HF (if configured) then read every local job file.
        if not self.enabled:
            return []
        self._hf.download_all(self.dir)
        jobs: list[dict] = []
        try:
            files = list(self.dir.glob("*.json"))
        except OSError:
            return []
        for f in files:
            try:
                jobs.append(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError) as e:
                log_event("jobstore_load_error", file=f.name, error=repr(e)[:200])
        return jobs


class _JobHFSink:
    """optional durable mirror of job json to a private HF Dataset (jobs/ prefix)."""

    def __init__(self) -> None:
        self.repo_id = (os.environ.get("JOBS_HF_REPO", "")
                        or os.environ.get("METRICS_HF_REPO", "")).strip()
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
            log_event("jobstore_hf_enabled", repo=self.repo_id)
        except Exception as e:  # noqa: BLE001
            log_event("jobstore_hf_disabled", reason=repr(e)[:200])
            self.enabled = False

    def upload(self, path: Path) -> None:
        if not self.enabled or self._api is None:
            return
        for attempt in range(3):
            try:
                self._api.upload_file(
                    path_or_fileobj=str(path), path_in_repo=f"jobs/{path.name}",
                    repo_id=self.repo_id, repo_type="dataset")
                return
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    log_event("jobstore_hf_upload_error", file=path.name, error=repr(e)[:200])
                    return
                time.sleep(2 ** attempt)

    def delete(self, name: str) -> None:
        if not self.enabled or self._api is None:
            return
        try:
            self._api.delete_file(path_in_repo=f"jobs/{name}", repo_id=self.repo_id,
                                  repo_type="dataset")
        except Exception:  # noqa: BLE001 - missing file is fine
            pass

    def download_all(self, dest: Path) -> None:
        if not self.enabled or self._api is None:
            return
        try:
            from huggingface_hub import snapshot_download
            snap = snapshot_download(self.repo_id, repo_type="dataset", token=self.token,
                                     allow_patterns="jobs/*.json")
            src = Path(snap) / "jobs"
            if src.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                for f in src.glob("*.json"):
                    (dest / f.name).write_bytes(f.read_bytes())
        except Exception as e:  # noqa: BLE001
            log_event("jobstore_hf_download_error", reason=repr(e)[:200])
