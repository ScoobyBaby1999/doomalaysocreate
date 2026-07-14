#!/bin/sh
set -e

# Initialize the database schema (idempotent — safe on every boot)
echo "[entrypoint] initializing database schema..."
bunx prisma db push --skip-generate 2>/dev/null || npx prisma db push --skip-generate 2>/dev/null || true

echo "[entrypoint] starting doomalaysocreate on port ${PORT:-7860}..."
exec node server.js
