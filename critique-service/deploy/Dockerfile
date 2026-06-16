# Hugging Face Spaces (Docker SDK) image for the FULL loom product: the panel gateway
# plus the web app served at /. Built from the tree the deploy-space GitHub Action
# assembles: backend (critique-service/*) at the root, frontend source under app/.
# CPU basic / free is plenty - all real work is I/O-bound on the LLM provider APIs.

# --- stage 1: build the web app (vite) so the runtime image ships only dist/ -------
FROM node:22-alpine AS webbuild
WORKDIR /web
COPY app/package.json app/package-lock.json ./
RUN npm ci
COPY app/ .
RUN npm run build

# --- stage 2: the python gateway, serving the built app from ./static --------------
# python:3.11-slim is enough for the service + the Claude Agent SDK (needs 3.10+).
# When the OpenHands free tier is re-enabled (see requirements.txt) bump to 3.12-slim.
FROM python:3.11-slim
WORKDIR /app

# git/zip/unzip: the agent orchestrator clones, packs, and unpacks repos & archives
# in its workspace. Kept minimal; cleaned in the same layer.
RUN apt-get update && apt-get install -y --no-install-recommends git zip unzip \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
# The Claude Agent SDK wheel bundles the Claude Code CLI (a self-contained binary),
# so no Node.js is needed at runtime.
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app
#   frontend source isn't needed at runtime; the built bundle is.
RUN rm -rf /app/app
COPY --from=webbuild /web/dist /app/static

# HF Spaces route public traffic to port 7860 by default; the service also honors
# $PORT / $APP_PORT if the platform sets them. bind 0.0.0.0 so it's reachable.
ENV PORT=7860
EXPOSE 7860

# run as a non-root user (HF runs containers as uid 1000).
# HOME must be writable & non-root: the bundled Claude Code CLI keeps its state there.
# AGENT_ROOT holds per-session agent workspaces (ephemeral /tmp is fine).
ENV HOME=/home/appuser \
    AGENT_ROOT=/tmp/agent \
    WORKSPACE_BASE=/tmp/workspace \
    BYPASS_TOOL_CONSENT=true \
    LOOM_DB_PATH=/data/loom.db
RUN mkdir -p /data && chmod 777 /data && useradd -m -u 1000 appuser && chown -R appuser:appuser /app /data
USER appuser

CMD ["python", "critique_service.py"]
