# MCPanel node agent image: FastAPI service that owns the Docker socket.
#
# Build from the repository root:
#   docker build -f agent/Dockerfile -t mcpanel/agent:local .
#
# Behind a TLS-intercepting egress proxy, pass the proxy and its CA bundle; the CA
# is mounted as a BuildKit secret only for the dependency download step and is
# never stored in an image layer:
#   docker build -f agent/Dockerfile --build-arg HTTPS_PROXY=http://proxy:3128 \
#     --secret id=build_ca,src=/path/to/ca-bundle.pem -t mcpanel/agent:local .
#
# The agent listens on 0.0.0.0:8700 *inside its container* so the panel/worker can
# reach it on the internal Compose network. Never publish this port on the host.

# --- Stage 1: Python dependencies (locked, no dev group) --------------------------------
FROM python:3.13-slim AS python-deps
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3 \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /build
COPY agent/pyproject.toml agent/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=secret,id=build_ca \
    if [ -s /run/secrets/build_ca ]; then export SSL_CERT_FILE=/run/secrets/build_ca; fi; \
    uv sync --locked --no-dev --no-install-project

# --- Stage 2: runtime ---------------------------------------------------------------------
FROM python:3.13-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH

# restic performs the encrypted, deduplicated instance backups (static Go binary).
# Version verified against https://hub.docker.com/v2/repositories/restic/restic/tags
COPY --from=restic/restic:0.19.1 /usr/bin/restic /usr/local/bin/restic

WORKDIR /app
COPY --from=python-deps /opt/venv /opt/venv
COPY agent/mcagent ./mcagent
COPY --chmod=0644 agent/container/healthcheck.py /usr/local/bin/mcagent-healthcheck.py
RUN python -m compileall -q mcagent && restic version

# The agent deliberately runs as root:
#  * it talks to /var/run/docker.sock, which is root-equivalent on the host anyway
#    (the socket is root:docker 0660 and the docker GID differs between hosts);
#  * it creates instance/staging/trash directories and chowns them, and every file it
#    writes into an instance, to AGENT_RUNTIME_UID/GID (the UID the Minecraft
#    process runs as), which requires CAP_CHOWN;
#  * restic must read and restore world files owned by that runtime UID, including
#    files a server creates with restrictive modes, and preserve their ownership.
# Its attack surface is kept narrow instead: bearer-token API, image allow-list,
# validated container specs and fd-based, symlink-safe file access (see
# docs/SECURITY.md). The Minecraft servers it starts never run as root.
USER 0:0
EXPOSE 8700
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python", "/usr/local/bin/mcagent-healthcheck.py"]
CMD ["uvicorn", "mcagent.main:app", "--host", "0.0.0.0", "--port", "8700", "--no-proxy-headers", "--no-server-header"]
