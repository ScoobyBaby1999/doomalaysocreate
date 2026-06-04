# Hugging Face Spaces (Docker SDK) image for the loom critique panel.
# CPU basic / free is plenty - all real work is I/O-bound on the LLM provider APIs.
FROM python:3.11-slim

# loom critique panel: a slim POST /api/critique service. only dependency is httpx;
# everything else (the HTTP server, JSON, asyncio) is stdlib.
WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

# HF Spaces route public traffic to port 7860 by default; the service also honors
# $PORT / $APP_PORT if the platform sets them. bind 0.0.0.0 so it's reachable.
ENV PORT=7860
EXPOSE 7860

# run as a non-root user (HF runs containers as uid 1000).
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

CMD ["python", "critique_service.py"]
