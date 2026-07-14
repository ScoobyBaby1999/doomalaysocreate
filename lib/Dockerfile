# Hugging Face Spaces (Docker SDK) image for the FULL doomalaysocreate product: the panel gateway
# plus the web app served at /. Built from the tree the deploy-space GitHub Action
# assembles: backend (lib/*) at the root, frontend source under app/.
# CPU basic / free is plenty - all real work is I/O-bound on the LLM provider APIs.

# --- stage 0: privatemode proxy binary (static Go binary, extracted at build time) ---
# Only ~25MB; COPY --from extracts it without needing Docker daemon at runtime.
FROM ghcr.io/edgelesssys/privatemode/privatemode-proxy:latest AS privatemode

# --- stage 1: build the web app (vite) so the runtime image ships only dist/ -------
ARG VITE_MAIN_SPACE
FROM node:22-alpine AS webbuild
WORKDIR /web
COPY app/package.json app/package-lock.json ./
# Use install (not ci) so the lock file auto-updates when package.json adds new deps
RUN npm install
# Copy source files ONLY (not node_modules — npm ci already installed deps).
# Using selective COPY to avoid clobbering /web/node_modules/.
COPY app/index.html app/tsconfig.json app/tailwind.config.js app/postcss.config.js app/vite.config.ts ./
COPY app/src/ ./src/
COPY app/public/ ./public/
ENV VITE_MAIN_SPACE=${VITE_MAIN_SPACE}
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

# Privatemode proxy binary (static Go binary, no runtime deps).
COPY --from=privatemode /bin/privatemode-proxy /usr/local/bin/privatemode-proxy

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
    WORKSPACE_BASE=/data/workspaces \
    BYPASS_TOOL_CONSENT=true \
    DOOMALAYSOCREATE_DB_PATH=/data/doomalaysocreate.db
ENV MAIN_SPACE_URL=https://scoobybaby1999-doomalaysocreate.hf.space
RUN mkdir -p /data && chmod 777 /data && useradd -m -u 1000 appuser && chown -R appuser:appuser /app /data

# NOTE: The GLM bridge (Node.js mini-service on port 3030) is NO LONGER NEEDED.
# The Python backend calls Puter/Z.ai/NVIDIA/OpenRouter directly via urllib
# (agent_sessions._glm_call_native). The bridge is kept as an optional fallback
# but is not started by default. This eliminates the Bun/Node crash that was
# causing the Space to restart indefinitely.
#
# If you want to enable the bridge later, install Node.js and uncomment:
#   RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
#       && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
#       && apt-get install -y --no-install-recommends nodejs \
#       && rm -rf /var/lib/apt/lists/*
#   COPY glm-bridge /app/glm-bridge
#   RUN cd /app/glm-bridge && npm install
#   CMD ["sh", "-c", "(cd /app/glm-bridge && while true; do node index.ts; sleep 3; done) & sleep 2 && cd /app && python critique_service.py"]

USER appuser

# Start ONLY the Python gateway — the GLM bridge is not needed (Python calls
# Puter/Z.ai directly). This is the simplest, most stable configuration.
CMD ["python", "critique_service.py"]
