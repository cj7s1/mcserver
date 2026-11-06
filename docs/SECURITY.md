# Security model

MCPanel manages Docker on a machine you care about. Read this before exposing it anywhere.

## Trust boundaries

| Boundary | What protects it |
|---|---|
| Browser → panel | Session cookie (HttpOnly, SameSite=Lax), CSRF token on every unsafe request, WebSocket origin validation, login throttling (10/min), scrypt password hashing. |
| Panel → agent | Shared bearer token (`MCPANEL_AGENT_TOKEN` = `AGENT_TOKEN`), constant-time comparison. On the single host the agent is reachable only on the internal Compose network (no published port). Additional nodes serve HTTPS (`AGENT_TLS=auto`) and the panel **pins** their certificate by SHA-256 fingerprint, so the token is only sent after the handshake proved the agent's identity (`backend/apps/nodes/tls.py`). |
| Agent → Docker | The agent validates every container spec on its own: image allow-list, no privileged mode, `cap_drop: ALL` + `CHOWN, DAC_OVERRIDE, FOWNER, SETUID, SETGID, KILL` (needed by the itzg entrypoint to fix ownership and drop to the runtime UID), `no-new-privileges`, PID limit, memory limit, only the instance directory mounted, allowed networks, port range. |
| Agent → host files | All file operations are confined to `<data root>/instances/<id>` using fd-based traversal with `O_NOFOLLOW`; symlinks are never followed; `..` is rejected; writes are atomic. Downloads only from allow-listed provider CDNs over HTTPS and must match a published SHA-1/SHA-256/SHA-512. SpigotMC jars (no published hash) are downloaded by the panel instead and uploaded with a SHA-256 the agent verifies (see below). |
| Panel users → servers | Permission matrix (see `backend/apps/accounts/permissions.py`). Global roles apply everywhere; per-server grants apply to one server. All checks are enforced in the backend; the UI only hides what you cannot do. Every state-changing action is written to the audit log (secrets redacted). |

## What Docker does **not** protect you from

Modded and plugin servers run **arbitrary third-party Java code** with the privileges of the
Minecraft process inside the container. A malicious mod can:

* read and modify everything in its own server directory (worlds, configs, the RCON password file
  `.rcon-cli.env` that itzg writes),
* make outbound network connections,
* use as much CPU/RAM as its limits allow,
* attempt container-escape exploits against the kernel.

Docker namespaces and the dropped capabilities raise the bar, but they are **not** a strong sandbox
against a determined attacker. Only install mods, plugins and modpacks from sources you trust. Uploaded
JARs are treated as untrusted and are never executed by the panel itself.

## Plugin downloads without a published hash (SpigotMC)

Modrinth, CurseForge and Hangar publish a hash for every file, and the agent's `/files/fetch` only downloads
from their CDNs when the file matches that hash. SpigotMC publishes none: the panel reaches it through the
Spiget API, whose CDN (`cdn.spiget.org`) holds a copy of each free resource's latest file
(`docs/reference/spiget-api.md`). SpigotMC itself answers automated clients with a Cloudflare browser
challenge; the panel does not try to get around it.

Two designs were considered:

1. *Panel hashes, agent re-downloads*: the panel downloads the jar, computes its SHA-256 and gives URL + hash to
   `/files/fetch`. That would add `cdn.spiget.org` to the agent's allow-list, download every jar twice, and the
   hash would only prove that both downloads matched, not that the file is what the author uploaded. It also
   leaves a window in which the file the panel inspected and the file the agent stores can differ.
2. *Panel downloads once and streams the verified bytes to the agent* (**chosen**). The panel fetches only
   `https://cdn.spiget.org/file/spiget-resources/<id>.jar` (the host and path of Spiget's redirect are checked,
   redirects are never followed, the size is capped at 200 MB), checks the bytes against the SHA-1 the CDN's
   storage reports, refuses anything that is not a Bukkit/Paper plugin jar for this server, and uploads exactly
   those bytes with their SHA-256. The agent commits the file only if the SHA-256 of what it received matches
   (`PUT /files/upload?sha256=`), with the same path confinement as every other write. The agent's download
   allow-list is unchanged.

What this does **not** protect against: a malicious or compromised upload on SpigotMC, or a compromised Spiget
mirror. There is no publisher hash to compare against, so a SpigotMC install has the same trust level as a jar
you upload yourself. Premium (paid) and externally hosted resources are never downloaded; the panel links to
SpigotMC instead. The mirror can lag behind SpigotMC, so the panel asks for confirmation when Spiget's copy was
stored before the latest release and records the version read from the jar's `plugin.yml`.

## The agent is privileged

Anything that can talk to the Docker socket is effectively root on the host. The agent therefore:

* is never published on a host port in the single-host stack and only listens on the internal network.
  An additional node (`deploy/docker-compose.node.yml`) must publish it so the panel can reach it; it
  then serves HTTPS with a pinned certificate. Bind it to the LAN/VPN interface the panel uses
  (`MCPANEL_NODE_BIND`) and never forward it to the internet,
* refuses API requests whose source address is on an instance network (Minecraft containers run
  third-party code), even with a valid token,
* exposes only the narrow operations listed in `agent/mcagent/main.py` (no arbitrary exec, no arbitrary
  mounts, no arbitrary images),
* requires the bearer token on every request (except the unauthenticated `/v1/health`, which reveals
  only whether Docker is reachable).

Keep `MCPANEL_AGENT_TOKEN` secret and long (48+ random characters).

## Network exposure

* The panel binds to `127.0.0.1` by default (`MCPANEL_BIND`). On Unraid, set it to the server's LAN IP.
* For access from outside your LAN, put the panel behind an HTTPS reverse proxy (Nginx Proxy Manager,
  SWAG, Caddy, Traefik…) and set `MCPANEL_BEHIND_TLS_PROXY=true`, `MCPANEL_SECURE_COOKIES=true`,
  `MCPANEL_ALLOWED_HOSTS` and `MCPANEL_CSRF_TRUSTED_ORIGINS` to the public hostname. Prefer a VPN
  (WireGuard/Tailscale) over exposing the panel publicly at all.
* The panel never configures router port forwarding, UPnP or DNS. Minecraft game ports are published
  on the Docker host only; making them reachable from the internet is a deliberate step you take on
  your router.
* RCON (25575) is never published; only the agent reaches it on the container network.

## Secrets

* `MCPANEL_SECRET_KEY`, `MCPANEL_AGENT_TOKEN`, `POSTGRES_PASSWORD`, `AGENT_RESTIC_PASSWORD` come from
  the environment (or `*_FILE` variants) — never from the repository.
* RCON passwords and provider API keys are encrypted at rest with a key derived from
  `MCPANEL_SECRET_KEY`. Changing the secret key makes them unreadable (RCON passwords are regenerated on
  the next container recreation; re-enter API keys).
* **Back up `AGENT_RESTIC_PASSWORD`** (or `<data root>/agent/restic-password` if it was generated).
  Without it, backups cannot be restored.

## Reporting

This is a self-hosted project; open an issue on the repository for security problems that do not put
other users at risk, otherwise contact the maintainer privately.
