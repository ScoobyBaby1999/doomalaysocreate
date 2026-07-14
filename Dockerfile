# Dockerfile for Hugging Face Spaces (and any Docker host).
# Builds the Next.js standalone output and runs on port 7860.
# On startup: prisma generate + db push (auto-creates schema), then next start.

FROM node:20-slim AS base

# Install bun (used for package management + runtime)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates unzip \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://bun.sh/install | bash \
    && ln -s /root/.bun/bin/bun /usr/local/bin/bun

ENV BUN_INSTALL=/root/.bun
ENV PATH=$BUN_INSTALL/bin:$PATH

# ---- Build stage
FROM base AS builder
WORKDIR /app

# Copy manifests first for better layer caching
COPY package.json bun.lock* ./
COPY prisma ./prisma

# Install dependencies
RUN bun install --frozen-lockfile 2>/dev/null || bun install

# Copy the rest of the source
COPY . .

# Generate Prisma client
RUN bun run db:generate

# Build Next.js (standalone output)
RUN bun run build

# ---- Runtime stage
FROM base AS runner
WORKDIR /app

ENV NODE_ENV=production
ENV PORT=7860
ENV HOSTNAME=0.0.0.0
# Default DB path: HF persistent storage (/data), fallback to local
ENV DATABASE_URL=file:/data/custom.db

# Create /data directory (HF mounts persistent storage here on paid tiers)
RUN mkdir -p /data /app

# Copy standalone build
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public
COPY --from=builder /app/prisma ./prisma
COPY --from=builder /app/node_modules/.prisma ./node_modules/.prisma
COPY --from=builder /app/node_modules/@prisma ./node_modules/@prisma

# Copy the startup script
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 7860

# Healthcheck (optional — HF probes / itself)
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
  CMD curl -f http://localhost:7860/api/health || exit 1

CMD ["/app/docker-entrypoint.sh"]
