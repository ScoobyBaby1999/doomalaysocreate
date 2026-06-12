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
FROM python:3.11-slim
WORKDIR /app

COPY requirements.txt /app/requirements.txt
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
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

CMD ["python", "critique_service.py"]
