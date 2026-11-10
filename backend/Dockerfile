# MCPanel panel image: Django API + WebSockets + bundled SPA, Celery workers and beat, plus the
# Redis server and the small supervisor that run them all in one container (`all`).
#
# Build from the repository root (the frontend is part of the build context):
#   docker build -f backend/Dockerfile -t mcpanel/panel:local .
#
# Behind a TLS-intercepting egress proxy, pass the proxy and its CA bundle; the CA
# is mounted as a BuildKit secret only for the dependency download steps and is
# never stored in an image layer:
#   docker build -f backend/Dockerfile --build-arg HTTPS_PROXY=http://proxy:3128 \
#     --secret id=build_ca,src=/path/to/ca-bundle.pem -t mcpanel/panel:local .
#
# Runtime roles are selected by the entrypoint: all (everything in one container, what
# deploy/docker-compose.yml runs) | web (default) | worker | beat | manage.
#
# Base images: the Debian release is pinned (trixie), so a rebuild with --pull brings security
# fixes but never a new Debian release by surprise. Redis comes from the official image of the
# same 7.4 line the stack has always used; `docker compose build --pull` refreshes both.

# --- Stage 1: frontend (static SPA build) --------------------------------------------
FROM node:22-alpine AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    --mount=type=secret,id=build_ca \
    if [ -s /run/secrets/build_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/build_ca; fi; \
    npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- Stage 2: Python dependencies (locked, no dev group) --------------------------------
FROM python:3.13-slim-trixie AS python-deps
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3 \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /build
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=secret,id=build_ca \
    if [ -s /run/secrets/build_ca ]; then export SSL_CERT_FILE=/run/secrets/build_ca; fi; \
    uv sync --locked --no-dev --no-install-project

# --- Stage 3: Redis binaries (official Debian-based image) ---------------------------------
FROM redis:7.4 AS redis

# --- Stage 4: runtime ---------------------------------------------------------------------
FROM python:3.13-slim-trixie AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH \
    DJANGO_SETTINGS_MODULE=mcpanel.settings

# Unprivileged runtime user. The panel never needs root: it has no Docker socket and
# writes nothing outside /tmp (uploads in transit; the celery beat schedule in `beat` mode)
# and /run/mcpanel (`all` mode: the supervisor's status file and the Redis socket). That
# directory belongs to the runtime user alone; the Compose file mounts a tmpfs over it.
RUN groupadd --gid 10001 mcpanel \
    && useradd --uid 10001 --gid 10001 --no-log-init --create-home --home-dir /home/mcpanel \
       --shell /usr/sbin/nologin mcpanel \
    && install -d -o 10001 -g 10001 -m 0700 /run/mcpanel

# redis-server for `all` mode (it listens on a Unix socket in /run/mcpanel only, see
# backend/container/supervise.py) and redis-cli for looking at it:
#   docker compose exec panel redis-cli -s /run/mcpanel/redis.sock info server
# The build fails if the binary does not run on this base or is not Redis 7.4.
COPY --from=redis /usr/local/bin/redis-server /usr/local/bin/redis-cli /usr/local/bin/
RUN redis-server --version \
    && redis-server --version | grep -Eq 'v=7\.4\.[0-9]+ ' \
    && redis-cli --version

WORKDIR /app
COPY --from=python-deps /opt/venv /opt/venv
COPY backend/manage.py backend/pyproject.toml ./
COPY backend/mcpanel ./mcpanel
COPY backend/apps ./apps
# settings.FRONTEND_DIST defaults to BASE_DIR / "frontend_dist" (= /app/frontend_dist);
# MCPANEL_FRONTEND_DIST overrides it.
COPY --from=frontend /src/frontend/dist ./frontend_dist
COPY --chmod=0755 backend/container/entrypoint.sh /usr/local/bin/mcpanel-entrypoint
COPY --chmod=0644 backend/container/healthcheck.py /usr/local/bin/mcpanel-healthcheck.py
COPY --chmod=0644 backend/container/supervise.py /usr/local/bin/mcpanel-supervise.py

# collectstatic only imports settings; the dummy values exist for this build step
# only and are not part of the image environment.
RUN MCPANEL_SECRET_KEY=build-time-only-not-a-secret \
    MCPANEL_AGENT_TOKEN=build-time-only-not-a-secret \
    python manage.py collectstatic --noinput -v 0 \
    && python -m compileall -q mcpanel apps

# Git revision the image was built from (deploy/scripts/upgrade.sh passes it and reads it back
# to know what is deployed). Last on purpose: a new revision does not invalidate any layer above.
ARG MCPANEL_REVISION=unknown
LABEL io.mcpanel.revision=$MCPANEL_REVISION

USER 10001:10001
EXPOSE 8000
# In `all` mode the same script also checks the supervisor's children, both workers and the
# reconcile tick (it finds the supervisor's status file; deploy/docker-compose.yml passes `all`
# and sets its own timing).
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD ["python", "/usr/local/bin/mcpanel-healthcheck.py"]
ENTRYPOINT ["mcpanel-entrypoint"]
CMD ["web"]
