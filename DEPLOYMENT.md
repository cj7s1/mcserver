# Deployment

MCPanel runs as a Docker Compose stack on one Linux Docker host. For Unraid specifics, read
[docs/UNRAID.md](docs/UNRAID.md) as well.

## Requirements

* Docker Engine 24+ with the Compose v2 plugin (`docker compose version`).
* x86_64 or arm64. Large modpacks need 8–12 GB RAM per server plus about 2 GB for the stack.
* Outbound HTTPS to Mojang, Modrinth, CurseForge, FTB, the itzg mirrors and Docker Hub.

## Install

```bash
git clone https://github.com/cj7s1/mcserver.git
cd mcserver/deploy
./scripts/generate-secrets.sh        # writes .env with random secrets; refuses to overwrite
```

Edit `.env` (every variable is documented in `deploy/.env.example`):

| Setting | Why |
|---|---|
| `MCPANEL_DATA_DIR` | Absolute host path for all server data. It is mounted at the **same path** inside the agent. |
| `MCPANEL_BACKUP_DIR` | Optional separate disk for restic repositories. |
| `MC_UID` / `MC_GID` | Owner of server files. Use `99`/`100` on Unraid. |
| `MCPANEL_BIND` | Defaults to `127.0.0.1`. Set your LAN IP to reach the panel from other machines. |
| `MCPANEL_ALLOWED_HOSTS`, `MCPANEL_CSRF_TRUSTED_ORIGINS` | Must contain the host/origin you type into the browser. |
| `MCPANEL_LAN_ADDRESS`, `MCPANEL_PUBLIC_HOSTNAME` | Shown to users as connection addresses (informational only). |
| `AGENT_RESTIC_PASSWORD` | **Store a copy somewhere safe.** Without it, backups can't be restored. |

Start the stack:

```bash
docker compose up -d --build
docker compose ps          # all services healthy (beat has no health check)
```

Open `http://<MCPANEL_BIND>:<MCPANEL_PORT>/`, create the owner account, and create a server.

## What runs

| Service | Image | Privileges |
|---|---|---|
| `panel` | `mcpanel/panel` (`web`) | UID 10001, no capabilities. Runs migrations on start and serves API, WebSockets and UI. |
| `worker` | same (`worker`) | Celery jobs: installs, backups, restores, updates. |
| `beat` | same (`beat`) | Scheduler: reconcile loop, schedules, metrics. Run exactly one. |
| `agent` | `mcpanel/agent` | Root, Docker socket, data directory. Never published. It refuses requests coming from the Minecraft network. |
| `postgres`, `redis` | upstream | Internal network only. |

The agent creates Minecraft containers itself (named `mcpanel-<id>`, label `io.mcpanel.managed=true`)
on the `mcpanel_instances` network. Because Compose doesn't manage them, `docker compose down` does
**not** stop running servers. Stop them in the panel first if you want everything down.

## HTTPS / remote access

The panel speaks plain HTTP. For access from outside your LAN, put an HTTPS reverse proxy in front of it
(any of Nginx Proxy Manager, SWAG, Caddy, Traefik) and set:

```
MCPANEL_BEHIND_TLS_PROXY=true
MCPANEL_SECURE_COOKIES=true
MCPANEL_ALLOWED_HOSTS=panel.example.com
MCPANEL_CSRF_TRUSTED_ORIGINS=https://panel.example.com
MCPANEL_FORWARDED_ALLOW_IPS=192.168.1.10      # the proxy's address as the panel sees it
```

`MCPANEL_FORWARDED_ALLOW_IPS` lists the proxies whose `X-Forwarded-For` header the panel believes.
It defaults to `127.0.0.1,::1` only: Docker and LAN ranges are not trusted automatically, because the
Minecraft containers (third-party mod code) and other machines on your network live there and could
otherwise pretend to be any address (bypassing the login rate limit, faking audit log entries). Until
you set it, every request appears to come from the proxy and all users share one login rate limit;
the panel logs a warning at start. To find the right value, open the panel through the proxy once and
run `docker compose logs --tail 20 panel`: the access log lines start with the address requests came
from. That is the proxy's LAN IP for a proxy on another machine or on the host network, or a Docker
address such as `172.18.0.1` for a proxy container on a bridge network. Use that exact address (a
comma-separated list is allowed), never a whole private range and never `*`.

The proxy must forward WebSockets for `/ws/` (Upgrade/Connection headers). Raise its body-size limit
and its read/send timeouts. Uploads (worlds, archives) can be several GB, and installing a mod with
dependencies or downloading a world zip can take several minutes. For nginx, for example:
`client_max_body_size 4g; proxy_read_timeout 900s; proxy_send_timeout 900s; proxy_request_buffering off;`.
A VPN such as WireGuard or Tailscale is safer than exposing the panel publicly.

Game ports are separate. The panel publishes each server's port on the Docker host only. Players outside
your LAN need a router port forward for that TCP port, which you configure yourself.

## Additional nodes (more Docker hosts)

One panel can manage Minecraft servers on several Docker hosts. Every extra host ("node") runs only
the agent, from `deploy/docker-compose.node.yml`. The panel reaches it over **HTTPS with a pinned
certificate**: the agent generates a self-signed certificate on first start (`AGENT_TLS=auto`, kept in
`<MCPANEL_DATA_DIR>/agent/tls/`), and the panel trusts exactly that certificate. The token is sent only
after the TLS handshake has matched the pin.

On the new host:

```bash
git clone https://github.com/cj7s1/mcserver.git && cd mcserver/deploy
./scripts/generate-secrets.sh --node        # writes node.env (AGENT_TOKEN, AGENT_RESTIC_PASSWORD), mode 600
# edit node.env: MCPANEL_NODE_BIND (required: this host's LAN/VPN IP), MCPANEL_DATA_DIR, MC_UID/MC_GID, port
docker compose -f docker-compose.node.yml --env-file node.env up -d --build
docker compose -f docker-compose.node.yml --env-file node.env exec agent python -m mcagent fingerprint
```

Then register it on the **panel host** (a Nodes screen in the UI is planned; the API
`POST /api/nodes` does the same):

```bash
cd mcserver/deploy
read -rs MCPANEL_NODE_TOKEN && export MCPANEL_NODE_TOKEN   # paste AGENT_TOKEN from node.env, press Enter
docker compose exec -e MCPANEL_NODE_TOKEN panel \
  python manage.py add_node --name node2 --url https://<node address>:8700 \
  --fingerprint <the AB:CD:… line printed above> --lan-address <node LAN IP>
unset MCPANEL_NODE_TOKEN
```

`read -s` keeps the token off the screen and out of your shell history (bash, the Unraid default);
`-e MCPANEL_NODE_TOKEN` passes it by name, so it does not appear in the command line either. Don't
use `--token` on the command line for the same reason.

The panel reads the certificate the agent presents and refuses to save the node if it does not match
the fingerprint, then checks that the agent answers. Servers can be created on the node from the create wizard and moved between nodes
(**Settings → Danger zone → Move to another node**). Each node keeps its own restic backups.

Notes:

* The agent port must be reachable from the panel host only. `MCPANEL_NODE_BIND` has no default:
  set it to the LAN or VPN address of the node (the stack refuses to start without it), and do not
  forward the port on your router. A WireGuard/Tailscale link between the hosts is a good fit. Use
  `0.0.0.0` only if a firewall lets nothing but the panel host reach the port.
* If the node's certificate is regenerated (deleting `agent/tls/`), the panel stops talking to it with
  an `agent_tls` error until you paste the new fingerprint into the node.
* Using your own certificate instead: set `AGENT_TLS=files`, `AGENT_TLS_CERT_FILE` and
  `AGENT_TLS_KEY_FILE` in a compose override. Pin it the same way, or leave the fingerprint empty and
  let the panel verify it against public CAs plus `MCPANEL_AGENT_CA_BUNDLE` (a PEM file mounted into
  panel and worker), in which case the URL's host name must match the certificate.
* A plain `http://` node still works but the API flags it with a warning: its token and console
  traffic cross the network unencrypted.
* If the **default** node is remote (`MCPANEL_AGENT_URL=https://…`), pin it with
  `MCPANEL_AGENT_TLS_FINGERPRINT` in `.env`.

## Upgrading

```bash
cd mcserver && git pull
cd deploy
docker compose pull --ignore-buildable   # newer postgres/redis images (security fixes)
docker compose build --pull              # rebuild panel and agent on fresh base images
docker compose up -d
```

`docker compose up -d --build` alone reuses the base images (`python`, `node`) already on the host and
never updates `postgres`/`redis`, so it skips their security fixes; run the two commands above at
least monthly. On additional nodes: `docker compose -f docker-compose.node.yml --env-file node.env build
--pull`, then `... up -d`. The certificate (and so the pin) survives upgrades because it lives in the
data directory.

The compose file uses tags (`postgres:16-alpine`, `redis:7-alpine`), which move to new patch releases
when you pull. To pin exact builds instead, put a digest in a `docker-compose.override.yml`, e.g.
`services: {postgres: {image: "postgres:16-alpine@sha256:<digest>"}}`, taking the digest from
`docker image inspect --format '{{index .RepoDigests 0}}' postgres:16-alpine` after a pull. Update the
digest yourself on every upgrade; a pinned image never gets security fixes on its own. Don't change
the Postgres major version (16) without a dump and restore.

Migrations run automatically when `panel` starts, and `worker`/`beat` wait for it. Running Minecraft
servers are not restarted by an upgrade. Before major upgrades, back up the database (see below).

## Backing up the panel itself

* **Database:** it holds users (password hashes), server definitions, schedules, backup metadata and
  the encrypted secrets. Dump it into a private directory, not a network share:

  ```bash
  cd mcserver/deploy
  (umask 077 && mkdir -p ~/mcpanel-db-backups && \
   docker compose exec -T postgres pg_dump -U mcpanel --exclude-table-data=django_session mcpanel \
     > ~/mcpanel-db-backups/mcpanel-db-$(date +%F).sql)
  ```

  `umask 077` makes the file readable by you only. `--exclude-table-data=django_session` leaves out
  the login sessions: with them, anyone holding the dump could act as a logged-in user until the
  sessions expire. The dump plus `.env` (whose `MCPANEL_SECRET_KEY` decrypts the stored secrets) is
  everything needed to impersonate the panel, so store both like passwords.
* **`MCPANEL_DATA_DIR`:** server files. These are best protected with in-panel backups plus your normal NAS backup.
* **`MCPANEL_BACKUP_DIR`:** restic repositories, which are encrypted with `AGENT_RESTIC_PASSWORD`.
* **`deploy/.env`:** your secrets (mode 600, git-ignored like `deploy/node.env`).

## Troubleshooting

| Symptom | Check |
|---|---|
| Browser shows "Bad Request (400)" | Add the host you typed to `MCPANEL_ALLOWED_HOSTS`. |
| Login works but actions fail with CSRF errors | Add the exact origin (`http://ip:port`) to `MCPANEL_CSRF_TRUSTED_ORIGINS`. |
| "Node agent unreachable" | `docker compose logs agent`. Check that the token in `.env` is the same for panel and agent (both read `MCPANEL_AGENT_TOKEN`). |
| Server stuck in "Installing" | Open the console. The first start downloads the server and mods, which takes minutes for big packs. Crash loops are detected automatically and shown as *Failed* with a diagnosis. |
| Server "Failed: crash loop" | Overview → Diagnostics lists the cause (missing mods, Java version, memory…). |
| Port conflict on start | Another container or program uses the port. Change it in the server's Network tab. |
| Files owned by the wrong user | Set `MC_UID`/`MC_GID` and restart the server (the runtime fixes ownership on start). |
| Jobs never run | `docker compose ps worker beat`; `docker compose logs worker`. Stuck jobs can be marked failed in Activity. |

Logs: `docker compose logs -f panel worker agent`. Each server's own log is in its console and in
`logs/` in the file manager.
