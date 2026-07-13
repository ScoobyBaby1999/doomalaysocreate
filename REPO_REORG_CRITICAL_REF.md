# Repo Reorg Critical Reference

> **Source**: User directive (preserved verbatim as the canonical instruction set).

## The user's directive

push the following "C:\Users\Admin\Desktop\local repo" to the correct repo files; they are critical fixes.
the repo is messed up; the frontend (socreate) and backend (c) seem to have been merged in a weird half-hearted way. if we can either fix that by reverting the frontend back to the nearest stable fix without reverting any actual fixes or patches within the files - or you can do it manally using a method of your preference. both ways, the goal is to push the files in the folder above, sort out the repo so that thew frontend (socreate) and backend (c) are clear and sepearate branches - the frontend and backend should have clean seperation, they should remain ambigious of one another for the most part. also if we can push all the folders&files in the critique-service/ folder up a level to root or rename critique-service to main or something appropriate. HF Space: https://huggingface.co/spaces/ScoobyBaby1999/doomalaysocreate/tree/main. Github: https://github.com/ScoobyBaby1999/doomalaysocreate

## Follow-up directive (verbatim)

continue, but dont rename to backend/ rename to lib/ or push to root. then go thru everything for a quick sanity check, to make sure everything builds and no redundant, old unsured old version files/code.

## Key points

1. **Critical fixes folder**: `C:\Users\Admin\Desktop\local repo\` contains 7 files that must land in the correct repo paths:
   - `agent.ts` -> `src/api/agent.ts` (frontend, socreate branch)
   - `AgentChat.tsx` -> `src/screens/AgentChat.tsx` (frontend, socreate branch)
   - `ChatMessageBubble.tsx` -> `src/components/ChatMessageBubble.tsx` (frontend, socreate branch)
   - `chatStore.ts` -> `src/state/chatStore.ts` (frontend, socreate branch)
   - `SessionSidebar.tsx` -> `src/components/SessionSidebar.tsx` (frontend, socreate branch)
   - `chat_routes.py` -> `lib/chat_routes.py` (backend, c branch — folder renamed from critique-service to lib)
   - `critique_service.py` -> `lib/critique_service.py` (backend, c branch — folder renamed from critique-service to lib)

2. **Clean separation target**:
   - `socreate` branch = **frontend only** (`src/`, `package.json`, `tsconfig.json`, `vite.config.ts`, `index.html`, `postcss.config.js`, `tailwind.config.js`, `public/`, `.github/`, `.gitignore`, `README.md`, `API-CONTRACT.md`)
   - `c` branch = **backend only** (Python files under `lib/`, plus `Dockerfile`, agent skills, etc — no `src/`, no `package.json`)

3. **critique-service/ relocation (FINAL: per follow-up directive)**: On the `c` branch, the `critique-service/` directory is renamed to `lib/` (NOT `backend/`, NOT push-to-root). Frontend (socreate) branch should have NO backend Python files at all.

4. **Endpoints**:
   - HF Space: <https://huggingface.co/spaces/ScoobyBaby1999/doomalaysocreate/tree/main>
   - GitHub: <https://github.com/ScoobyBaby1999/doomalaysocreate>

5. **Sanity check (per follow-up directive)**: Verify everything builds and no redundant / old / unused version files or code remain.
