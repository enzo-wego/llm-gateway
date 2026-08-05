# llm-gateway — packaging only. The bundled Claude CLI is a self-contained,
# platform-specific binary shipped inside claude-agent-sdk, so dependencies are
# pip-installed for the target platform at build time. No Node runtime is
# needed, and the host's .venv / site-packages must never be copied in.
FROM python:3.12-slim

# Match the VPS host user (enzo = 1001:1001) so the container can write to the
# host-owned config/ bind mount. Docker Desktop virtualizes ownership, so the
# Mac is unaffected by the value.
ARG APP_UID=1001
ARG APP_GID=1001

RUN groupadd --gid "${APP_GID}" app \
 && useradd --uid "${APP_UID}" --gid "${APP_GID}" \
        --create-home --home-dir /home/app --shell /usr/sbin/nologin app

WORKDIR /app

# Dependency layer first, before app/, so it caches across source edits.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app app/ ./app/

# Env-loader entrypoint: reloads the persisted env file on every start so
# PUT /config survives `docker compose restart` (see the script's header). This
# is the systemd EnvironmentFile equivalent; it is packaging, not an app change.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh

# The bundled CLI writes session state under $HOME. /home/app is owned by app,
# and the named claude-home volume inherits that ownership on first mount.
ENV HOME=/home/app \
    PYTHONUNBUFFERED=1

USER app

EXPOSE 8750

# /health is the only route that does not require X-API-Key. Probe it with the
# stdlib rather than installing curl.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8750/health', timeout=3)"]

# The entrypoint loads config/.env into the environment, then execs this CMD.
# Bind 0.0.0.0 inside the container; host exposure is controlled by the compose
# port publish, not by the app.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8750", "--log-level", "info"]
