FROM python:3.14.8-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /uvx /bin/

RUN groupadd --gid 10001 meridian \
    && useradd --gid meridian --uid 10001 --home-dir /app --shell /usr/sbin/nologin meridian

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev --no-cache \
    && chown -R meridian:meridian /app

# The dataset DB and KB, owned by root so the app user can only read them (rule 1). First boot
# copies the DB to DB_PATH (the Railway volume, D13). The KB index is rebuilt from the KB docs and
# checked against the committed index at startup; the fee schedule is read at startup too.
COPY data/meridian_dataset/scheduling_db/meridian_scheduling.sqlite ./data/meridian_dataset/scheduling_db/
COPY data/meridian_dataset/knowledge_base ./data/meridian_dataset/knowledge_base
COPY kb/manifest.toml ./kb/
COPY kb/converted ./kb/converted
COPY kb/index ./kb/index
COPY scripts/entrypoint.sh /entrypoint.sh

# Starts as root only to make the volume directory writable (Railway mounts volumes root-owned),
# then drops to the app user for good.
ENTRYPOINT ["/entrypoint.sh"]

ENV HOME=/app \
    UV_CACHE_DIR=/tmp/uv-cache \
    ENV=production \
    APP_TODAY=2026-10-16

EXPOSE 8000

CMD ["uv", "run", "--no-dev", "--frozen", "--no-sync", "uvicorn", "meridian.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-server-header", "--proxy-headers", "--forwarded-allow-ips", "*"]
