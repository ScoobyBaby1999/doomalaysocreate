# Repo Reorg Critical Reference

> **Source**: User directive (preserved verbatim as the canonical instruction set).

## The user's directive

push the following "C:\Users\Admin\Desktop\local repo" to the correct repo files; they are critical fixes.
the repo is messed up; the frontend (socreate) and backend (c) seem to have been merged in a weird half-hearted way. if we can either fix that by reverting the frontend back to the nearest stable fix without reverting any actual fixes or patches within the files - or you can do it manally using a method of your preference. both ways, the goal is to push the files in the folder above, sort out the repo so that thew frontend (socreate) and backend (c) are clear and sepearate branches - the frontend and backend should have clean seperation, they should remain ambigious of one another for the most part. also if we can push all the folders&files in the critique-service/ folder up a level to root or rename critique-service to main or something appropriate. HF Space: https://huggingface.co/spaces/ScoobyBaby1999/doomalaysocreate/tree/main. Github: https://github.com/ScoobyBaby1999/doomalaysocreate

## Key points

1. **Critical fixes folder**: `C:\Users\Admin\Desktop\local repo\` contains 7 files that must land in the correct repo paths:
   - `agent.ts` -> `src/api/agent.ts` (frontend)
   - `AgentChat.tsx` -> `src/screens/AgentChat.tsx` (frontend)
   - `ChatMessageBubble.tsx` -> `src/components/ChatMessageBubble.tsx` (frontend)
   - `chatStore.ts` -> `src/state/chatStore.ts` (frontend)
   - `SessionSidebar.tsx` -> `src/components/SessionSidebar.tsx` (frontend)
   - `chat_routes.py` -> `critique-service/chat_routes.py` (backend, lands on c branch)
   - `critique_service.py` -> `critique-service/critique_service.py` (backend, lands on c branch)

2. **Clean separation target**:
   - `socreate` branch = **frontend only** (`src/`, `package.json`, `tsconfig.json`, `vite.config.ts`, `index.html`, `postcss.config.js`, `tailwind.config.js`, `public/`, `.github/`, `.gitignore`, `README.md`, `API-CONTRACT.md`)
   - `c` branch = **backend only** (Python files, `Dockerfile`, etc — no `src/`, no `package.json`)

3. **critique-service/ relocation**: On the `c` branch, push all folders/files in `critique-service/` up a level to root OR rename `critique-service` to `main` (or another appropriate name). Frontend (socreate) branch should have NO backend Python files at all.

4. **Endpoints**:
   - HF Space: <https://huggingface.co/spaces/ScoobyBaby1999/doomalaysocreate/tree/main>
   - GitHub: <https://github.com/ScoobyBaby1999/doomalaysocreate>
