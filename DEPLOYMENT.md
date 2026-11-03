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
```

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
./scripts/generate-secrets.sh --node        # writes node.env (AGENT_TOKEN, AGENT_RESTIC_PASSWORD)
# edit node.env: MCPANEL_DATA_DIR, MC_UID/MC_GID, MCPANEL_NODE_BIND (the LAN/VPN address), port
docker compose -f docker-compose.node.yml --env-file node.env up -d --build
docker compose -f docker-compose.node.yml --env-file node.env exec agent python -m mcagent fingerprint
```

In the panel, open **System → Nodes → Add node** and enter:

* URL: `https://<node address>:<MCPANEL_NODE_PORT>` (default port 8700)
* Token: `AGENT_TOKEN` from `node.env`
* Fingerprint: the `AB:CD:…` line printed above. The panel reads the certificate the agent presents
  and refuses to save the node if it does not match.

Then run **Check**. Servers can be created on the node from the create wizard and moved between nodes
(**Settings → Danger zone → Move to another node**). Each node keeps its own restic backups.

Notes:

* The agent port must be reachable from the panel host only. Bind it to the LAN or VPN interface
  (`MCPANEL_NODE_BIND`) and do not forward it on your router. A WireGuard/Tailscale link between the
  hosts is a good fit.
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
cd deploy && docker compose up -d --build
```

On additional nodes: `docker compose -f docker-compose.node.yml --env-file node.env up -d --build`.
The certificate (and so the pin) survives upgrades because it lives in the data directory.

Migrations run automatically when `panel` starts, and `worker`/`beat` wait for it. Running Minecraft
servers are not restarted by an upgrade. Before major upgrades, back up the database:
`docker compose exec postgres pg_dump -U mcpanel mcpanel > mcpanel-db.sql`.

## Backing up the panel itself

* **Database:** use `pg_dump` (see above). It holds users, server definitions, schedules and backup metadata.
* **`MCPANEL_DATA_DIR`:** server files. These are best protected with in-panel backups plus your normal NAS backup.
* **`MCPANEL_BACKUP_DIR`:** restic repositories, which are encrypted with `AGENT_RESTIC_PASSWORD`.
* **`deploy/.env`:** your secrets.

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
