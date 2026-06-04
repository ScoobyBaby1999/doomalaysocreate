# doomalays-Create
AI 10x Productivity Loop Forward Deployed Engineer Prompt Orchestration Agentic Vibe Coder

## `critique-service/` — loom's judge panel, as a hosted endpoint

A slim, token-guarded `POST /api/critique` service ported from **loom**. It fans a
plan (markdown *or* a loom JSON schematic) out to a diverse panel of judge LLMs —
different model families across different providers, so the critiques are
uncorrelated — and merges them into one consolidated, deduped bullet list. One
judge being rate-limited never fails the request.

- **Deploy free on Hugging Face Spaces (Docker).** Persistent public URL, reachable
  from anywhere.
- **Invoke from Android (or any repo)** via the `.claude/skills/critique/` skill —
  a thin client over the hosted endpoint. The phone is the client; the Space is the
  backbone.
- **Editable panel:** judges live in `critique-service/panel.json`; any
  `provider/model` named there is registered on the fly against that provider's key.

See **[`critique-service/README.md`](critique-service/README.md)** for the full API
contract, deployment steps, and security notes.
