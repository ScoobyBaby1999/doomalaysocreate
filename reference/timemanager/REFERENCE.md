# timemanager — reference only (NOT deployed)

Extracted from `timemanager.rar` (origin/pied) for future use. Lives at repo root so it
persists in git but is **never** part of the HF Space deploy (the workflow only ships
`lib/`). The original `backend/.env` (live API keys) was removed before commit.

## Why it's here — prior art for job persistence (Roadmap P0)

This older sibling app already solved checkpoint/resume, which our current
`lib` lacks (no `atomic_write_json`, no checkpoint module). Port these:

- **`backend/oplog.py:atomic_write_json`** — write `<path>.tmp` then `os.replace` so a
  kill mid-write never leaves a half-file. Use for all job/state snapshots.
- **`backend/content/checkpoint.py`** — `Checkpoint{stem, context, completed_stages,
  credits, schematic_hash}`; `save()` after each stage, `load()` on boot, `clear()` on
  done. Resume = skip `completed_stages`. (Note: it matches by stage NAME, not hash —
  our port should invalidate on schematic/job-shape change to avoid stale resumes.)
- **`backend/content/main.py`** (~line 100-223) — the resume loop: load checkpoint,
  skip completed stages, save after each advance, clear at the end; "pause-not-failure"
  for network errors (keep checkpoint, don't bump attempts).

It also contains a full **frontend** (`*.html`, `js/`, `css/`) — useful reference when
the doomalaysocreate frontend phase starts (calendar, chat, outputs, auth, state views).
