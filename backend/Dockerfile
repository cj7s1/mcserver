# MCPanel panel image: Django API + WebSockets + bundled SPA, Celery worker and beat.
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
# Runtime roles are selected by the entrypoint: web (default) | worker | beat | manage.

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
FROM python:3.13-slim AS python-deps
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

# --- Stage 3: runtime ---------------------------------------------------------------------
FROM python:3.13-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH \
    DJANGO_SETTINGS_MODULE=mcpanel.settings

# Unprivileged runtime user. The panel never needs root: it has no Docker socket and
# writes nothing outside /tmp (uploads in transit, celery beat schedule).
RUN groupadd --gid 10001 mcpanel \
    && useradd --uid 10001 --gid 10001 --no-log-init --create-home --home-dir /home/mcpanel \
       --shell /usr/sbin/nologin mcpanel

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

# collectstatic only imports settings; the dummy values exist for this build step
# only and are not part of the image environment.
RUN MCPANEL_SECRET_KEY=build-time-only-not-a-secret \
    MCPANEL_AGENT_TOKEN=build-time-only-not-a-secret \
    python manage.py collectstatic --noinput -v 0 \
    && python -m compileall -q mcpanel apps

USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD ["python", "/usr/local/bin/mcpanel-healthcheck.py"]
ENTRYPOINT ["mcpanel-entrypoint"]
CMD ["web"]
