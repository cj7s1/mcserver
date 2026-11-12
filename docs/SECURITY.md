# Security model

MCPanel manages Docker on a machine you care about. Read this before exposing it anywhere.

## Trust boundaries

| Boundary | What protects it |
|---|---|
| Browser → panel | Session cookie (HttpOnly, SameSite=Lax), CSRF token on every unsafe request, WebSocket origin validation (scheme, host **and port** must be the panel's own origin or one of `MCPANEL_CSRF_TRUSTED_ORIGINS`, so another web app on the same machine cannot open the console with your cookies), `X-Frame-Options: DENY` and a Content-Security-Policy (`script-src 'self'`, `frame-ancestors 'none'`) on every page, login throttling (10/min), scrypt password hashing. Request bodies are limited before Django reads them, so before any login check (see "Request size limits"). |
| Panel → agent | Shared bearer token (`MCPANEL_AGENT_TOKEN` = `AGENT_TOKEN`), constant-time comparison. On the single host the agent is reachable only on the internal Compose network (no published port). Additional nodes serve HTTPS (`AGENT_TLS=auto`) and the panel **pins** their certificate by SHA-256 fingerprint, so the token is only sent after the handshake proved the agent's identity (`backend/apps/nodes/tls.py`). |
| Agent → Docker | The agent validates every container spec on its own: image allow-list, no privileged mode, `cap_drop: ALL` + `CHOWN, DAC_OVERRIDE, FOWNER, SETUID, SETGID, KILL` (needed by the itzg entrypoint to fix ownership and drop to the runtime UID), `no-new-privileges`, PID limit, memory limit, only the instance directory mounted, allowed networks, port range. |
| Agent → host files | All file operations are confined to `<data root>/instances/<id>` using fd-based traversal with `O_NOFOLLOW`; symlinks are never followed; `..` is rejected; writes are atomic. The data root and its `instances` folder are mode 0750, owner root (set at every agent start), so other host users and guest SMB logins cannot read server folders. Downloads only from allow-listed provider CDNs over HTTPS and must match a published SHA-1/SHA-256/SHA-512. SpigotMC jars (no published hash) are downloaded by the panel instead and uploaded with a SHA-256 the agent verifies (see below). |
| Panel users → servers | Permission matrix (see `backend/apps/accounts/permissions.py`). Global roles apply everywhere; per-server grants apply to one server. All checks are enforced in the backend; the UI only hides what you cannot do. Every state-changing action is written to the audit log (secrets redacted). Resetting a world needs two permissions (`instance.restore` and `instance.files.write`) and the server's name typed exactly; the seed is not written to the audit log. |

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

## Advanced environment variables (`instance.configure`)

The **Environment** tab passes extra variables to the itzg/minecraft-server container. Users with
*Configure server* may set them, so the panel decides what that permission may do: tune the runtime
(server.properties variables such as `MOTD`, Paper/Purpur channels, autopause, logging, CurseForge or
Modrinth pack excludes, proxies), but not what other permissions guard. Refused, by name, prefix or
suffix (`backend/apps/instances/spec.py`, `env_override_error`):

* everything the panel sets itself (type, version, memory, JVM flags, ports, RCON, pinned pack IDs,
  the CurseForge key, `LOAD_ENV_FROM_*`, `SKIP_`/`OVERRIDE_SERVER_PROPERTIES`);
* installing code or content from sources the mods/plugins manager does not hash-verify
  (*Manage mods/plugins*): `MODS`, `PLUGINS`, `SPIGET_RESOURCES`, `MODRINTH_PROJECTS`,
  `CURSEFORGE_FILES`, `MODPACK`, `GENERIC_PACK(S)`, `PACKWIZ_URL`, `DATAPACKS`, `WORLD`, config
  downloads, custom server jars and installers, and every `*_URL`/`*_REPO` variable;
* running commands or changing what is executed (*Send commands*): `RCON_CMDS_*`,
  `STOP_SERVER_DELAY_COMMAND`, `PRE_START_SCRIPT`, `SERVER_RUNNER`, `ENABLE_SSH`, unauthenticated
  remote JMX (`ENABLE_JMX`), Java agents and `JVM_*`/`JAVA_*` option variables, and variables the
  shell or loader act on (`PS4`, `BASH_ENV`, `LD_*`, `PATH`, ...);
* granting operator or whitelist entries (*Manage players*): `OPS*`, `WHITELIST`, `WHITELIST_FILE`;
* weakening protections or leaking secrets: `SKIP_LOG4J_PATCHER`, `DEBUG*` (its `set -x` trace prints
  the RCON password into the console log), `DUMP_SERVER_PROPERTIES`.

Values that were saved before these rules existed are dropped from the container at its next
recreation (the restart banner shows), and saving the Environment tab asks you to remove them.

This is a deny-list: itzg adds variables over time, so treat *Configure server* as a trusted
permission. It also covers the JVM arguments field, where options such as `-javaagent:` or
`-XX:OnOutOfMemoryError=` are legitimate tools but run code inside the container. Grant it only to
people you would let install mods.

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
  third-party code), even with a valid token (403 `forbidden_source`, WebSocket close 4403). The
  subnets come from Docker and are refreshed every minute. Until Docker has answered after a start
  the agent does not let everything through (`agent/mcagent/netguard.py`): loopback and
  `/v1/health` pass; a subnet that was an instance network at the last run (remembered in
  `<data root>/agent/instance-networks.json`, 0600) stays refused; an attached subnet that was
  trusted then passes; an attached subnet it does not know gets 503 `agent_starting` (WebSocket
  1013); an address outside every subnet the agent is attached to passes. With no record at all
  only loopback and health pass,
* exposes only the narrow operations listed in `agent/mcagent/main.py` (no arbitrary exec, no arbitrary
  mounts, no arbitrary images),
* requires the bearer token on every request (except the unauthenticated `/v1/health`, which reveals
  only whether Docker is reachable).

btrfs snapshots need no extra privilege: the default stack takes, rolls back and deletes them (deleting
file by file). Extra privileges are granted only through an override file (docs/UNRAID.md section 6),
never by the default stack: `CAP_SYS_ADMIN` makes btrfs deletes instant and is required for ZFS
snapshots, which also need `/dev/zfs` and an `:rshared` data mount. They do not change the trust model (the agent is root-equivalent through
the Docker socket already), but `/dev/zfs` lets the agent administer *every* dataset on the host's
pools. The agent only builds `zfs` argv lists from dataset names it read from `/proc/self/mountinfo`
or `zfs list` and validated against a strict pattern; requests can name a snapshot (validated
timestamp pattern) but never a dataset, and nothing uses `zfs destroy -R` or `zfs rollback -r`. A dataset
is only treated as a server's own when it is mounted at the server folder with root `/` *and* its
`mountpoint` property is that folder, so a bind mount there can never get its source dataset renamed or
destroyed.

Keep `MCPANEL_AGENT_TOKEN` secret and long (48+ random characters).

## Request size limits

Django's ASGI handler reads a whole request body to disk before any middleware, login check or
URL routing runs, and uvicorn has no limit of its own. The panel therefore checks the size in
front of Django (`backend/apps/core/asgi.py`, `RequestBodyLimit`):

* every request may carry at most 25 MB; a declared `Content-Length` above that is answered 413
  without reading the body, and a chunked body is cut off as soon as it passes the limit;
* only the four upload endpoints (file upload, import, content upload, world replace) accept more,
  up to `MCPANEL_MAX_UPLOAD_BYTES` (default 2 GiB), they must declare the length (411
  otherwise), and above 25 MB the request must carry a session cookie of a signed-in user (one
  database read; permissions and CSRF are checked by Django afterwards, as for every request);
* a multipart request carries at most 1000 files and fields as Django counts them; more is 413.

So an anonymous client can make the panel spool at most 25 MB per request. Spooled bodies go to
`MCPANEL_UPLOAD_TMP` (default `/tmp/mcpanel-uploads`, mode 0700, created by the web process as UID
10001 when it starts) and are emptied once the uploaded files are copied out. More than 25 MB needs
a session of an active user that is still valid for the user's current password (Django's session
auth hash), checked before the body is read: the session as usual, then the user with one more
database read. The agent has its own upload limit (`AGENT_MAX_UPLOAD_BYTES`).

## Network exposure

* The panel binds to `127.0.0.1` by default (`MCPANEL_BIND`). On Unraid, set it to the server's LAN IP.
* Behind a reverse proxy the panel believes `X-Forwarded-For` only from `MCPANEL_FORWARDED_ALLOW_IPS`
  (default `127.0.0.1,::1`). Set it to the proxy's exact address; never to a whole private range or
  `*`, because Minecraft containers and other LAN hosts could then fake their address and get around
  the login rate limit (DEPLOYMENT.md, "HTTPS / remote access").
* An additional node's agent port is published on `MCPANEL_NODE_BIND`, which must be set explicitly
  (no `0.0.0.0` default).
* For access from outside your LAN, put the panel behind an HTTPS reverse proxy (Nginx Proxy Manager,
  SWAG, Caddy, Traefik…) and set `MCPANEL_BEHIND_TLS_PROXY=true`, `MCPANEL_SECURE_COOKIES=true`,
  `MCPANEL_ALLOWED_HOSTS` and `MCPANEL_CSRF_TRUSTED_ORIGINS` to the public hostname. Prefer a VPN
  (WireGuard/Tailscale) over exposing the panel publicly at all.
* The panel never configures router port forwarding, UPnP or DNS. Game ports are published on the
  Docker host only; making them reachable from the internet is a deliberate step you take on your
  router. A server's host address (`host_ip`) is `0.0.0.0` by default: every IPv4 address of the
  host, VPN interfaces (Tailscale, WireGuard) included, and no IPv6.
* Control ports are never published, for any game and by any path (create, a change of ports,
  clone, restore, import): RCON (Minecraft 25575, Factorio and Project Zomboid 27015,
  Counter-Strike 2 on its game port's number over TCP), TShock's REST API (7878), Palworld's REST
  API (8212), Rust's WebRCON (28016) and its web page (8080). The agent reaches them over the
  instance network `mcpanel_instances`, **which every game server shares**: the other game servers
  reach them too (next section). Game ports are published per protocol (Factorio: UDP).
* Redis (job queue, live events, cache) has no password and its cache holds pickled objects, so
  whoever can write to it can run code in the panel. It therefore has no network listener at all:
  it runs inside the panel container on a Unix socket (`--port 0`, mode 700) in `/run/mcpanel`, a
  memory-only directory of the panel's runtime user. Nothing outside that container can reach it,
  the agent and the Minecraft containers included. Only with the optional `external-redis` Compose
  profile is Redis a container on the internal network again, as it was before.

### What a compromised game server can reach

A game server runs code you do not control (mods, plugins, the game itself, and whoever exploits a
bug in it through its forwarded port). Docker's namespaces and the dropped capabilities contain the
process; the network is Docker's default and MCPanel does not restrict it yet:

* **Every other game server.** All game servers sit on one bridge, `mcpanel_instances`, with
  Docker's default of open traffic between its members. Code in one of them reaches every other
  server's control ports (the list above) and game ports, also of servers you did not forward or
  bound to one address, and Docker's DNS resolves their names. The control secrets the panel
  generates are 32 random characters (191 bits) and cannot be guessed; what is exposed is anything
  weaker: an admin password you chose yourself (Palworld's REST API uses it), a Minecraft backend in
  offline mode behind Velocity or BungeeCord (whoever reaches its port joins under any name), and a
  new Satisfactory server during its first setup: the panel claims it before it publishes any port,
  but a neighbour on the bridge can claim it first. The setup then stops with "claimed with another
  admin password" and the server is never published.
* **The Docker host on every address:** the Unraid web UI, SMB/NFS, every port another container
  publishes on the host, and the panel's own port when it is published on a LAN address.
* **The LAN and the internet** (Docker's default outbound NAT). It cannot simply be cut: Minecraft
  checks accounts with Mojang, and the Steam images download the game at every start.

Not reachable from a game container: the agent's API (it refuses requests from the instance
network even with a valid token, above), Postgres, the panel's container address, Redis, a panel
published on `127.0.0.1`, and containers on other Docker networks. No profile grants `NET_RAW` or
`NET_ADMIN`, so a game container cannot spoof addresses or sniff the bridge.

## One panel container

The web server, both Celery workers, the scheduler and Redis are processes of one container
(`panel`), all as UID 10001 without any Linux capability, under a supervisor that needs none either.
Before, they were five containers of the same image with the same secrets, network and user, so no
secret and no network boundary was given up. What is gone is the separation of processes and
files between the LAN-facing web process and the others: they share `/tmp`, `/run/mcpanel` and one
user. What was done about it:

* The scheduler keeps its schedule in memory. Its usual file is a pickle that is loaded at start; in
  a shared `/tmp` a file-write bug in the web process would have become code execution at the next
  scheduler start.
* The supervisor's status file and the Redis socket are in `/run/mcpanel` (mode 0700, tmpfs), not
  under a fixed name in `/tmp`. The status file is JSON and is only read, never executed.
* Redis is started with `PATH`, `TZ` and `LANG` only: none of the panel's secrets are in its
  environment.
* Redis keeps nothing on disk and loads nothing from disk. It is started with saving switched
  off, and before every start the supervisor removes whatever Redis would load from its
  directory (`dump.rdb`, `appendonly.aof`, `appendonlydir`, `temp-*.rdb`). The reason: the web
  process can write to that directory, and the values in the cache are pickles, so a planted
  dump file would become code execution at the next Redis start. If such a file cannot be
  removed, Redis is not started, and the supervisor tries again later.
* The agent stays its own container. The panel container still has no Docker socket, no host
  folders and no capabilities.

## What is kept of a stopped server (the last-run record)

A stopped server has no container. Before the agent removes the container it saves a record of
that run, so that the panel can still show how the run ended.

| | |
|---|---|
| Where | `<MCPANEL_DATA_DIR>/agent/last-run/<server id>.json` on the host of that server's node. One file per server; the next stop replaces it. |
| Mode | The folder is 0700 and the file 0600, owned by the user the agent runs as (root). |
| Content | Exit code, out-of-memory flag, restart count, container id and name, image name and image id, start and end time, the last health-check output, and the last 2,000 console lines (each cut at 16 KiB, 2 MiB in total). Control characters are removed; colour codes stay. |
| Why it is sensitive | Console output can contain player names, player IP addresses, chat, and passwords someone typed into the console. |
| Who can read it | Root on the host. The agent serves it only to callers with the agent token: the console lines through the endpoints that also serve the container log, the rest together with the container status. In the panel the console lines need the same permission as the live console (`instance.console`); the exit state is part of the server's status and visible to everyone who can see the server. |
| What cannot reach it | The Minecraft server and its mods: the file is outside every server folder, and that folder is all a server container gets. It is never part of a restic backup, a clone or the panel database. |
| When it is deleted | With the server. When a server moves to another node, the record on the old node is deleted. Archiving a server keeps it. |

A backup of the whole data directory made outside MCPanel (an appdata backup plugin, for
example) contains these files. Treat such a backup like the console logs themselves.

## Secrets

* `MCPANEL_SECRET_KEY`, `MCPANEL_AGENT_TOKEN`, `POSTGRES_PASSWORD`, `AGENT_RESTIC_PASSWORD` come from
  the environment (or `*_FILE` variants) — never from the repository.
* RCON passwords, provider API keys, node tokens and webhook URLs are encrypted at rest with a key
  derived from `MCPANEL_SECRET_KEY`. A value that cannot be decrypted is never silently replaced: the
  panel logs an ERROR naming the field once, treats it as missing (the System settings API flags
  the CurseForge key in `secrets_unreadable`, a webhook's last error says to enter the URL again) and keeps the
  stored ciphertext, so restoring the old key brings it back. RCON passwords are regenerated
  automatically and take effect at the next container recreation.
* The CurseForge API key is passed only to servers of type *CurseForge modpack* (`AUTO_CURSEFORGE`),
  as `CF_API_KEY`, because the runtime downloads the pack with it on every start. Mods on those
  servers run in the same process and can read it; the runtime has no way to hide it (a
  `CF_API_KEY_FILE` would have to live in `/data`, which mods can read too). Use a key you can revoke
  in the CurseForge console if a mod misbehaves. Other server types never receive it.
* Notification webhooks are sent from the periodic worker inside the panel container, which sits on
  the internal network. The target host is resolved and refused if it is loopback (which now is the
  panel container itself), link-local (169.254.0.0/16, including cloud metadata, and fe80::/10),
  multicast/reserved, one of MCPanel's own services (agent, postgres, panel, redis if it runs as a
  container, every node agent's address and port) or, with `MCPANEL_WEBHOOK_ALLOW_LAN=false`,
  a private/LAN address. LAN targets are allowed by default because Home Assistant, ntfy or Gotify on
  the LAN are the usual receivers. The request goes to the checked address (no second DNS lookup),
  redirects are not followed and delivery errors are generic. Through an egress proxy
  (`HTTPS_PROXY`) the proxy makes the connection, so the address check is the only protection.

### Rotating the secret key

1. Put the current key into `MCPANEL_SECRET_KEY_FALLBACKS` and a new random key (`openssl rand -hex 48`)
   into `MCPANEL_SECRET_KEY` in `deploy/.env`, then `docker compose up -d`. Sessions, password reset
   links and stored secrets made with the old key keep working.
2. `docker compose exec panel python manage.py reencrypt_secrets` re-encrypts every stored secret
   with the new key (`--check` only counts them).
3. Remove the old key from `MCPANEL_SECRET_KEY_FALLBACKS` and `docker compose up -d` again. Users
   whose sessions were signed with the old key have to log in again.
* **Back up `AGENT_RESTIC_PASSWORD`** (or `<data root>/agent/restic-password` if it was generated).
  Without it, backups cannot be restored.
* `deploy/unraid/backup-db.sh` writes the database dump (gzip, **not encrypted**) and a clear-text
  copy of `.env` next to it, in a folder it makes 0700, files 0600. Together they are everything
  needed to impersonate the panel and to open every backup; keep that folder off shares other
  people or devices can read.
* The CurseForge API key is checked against CurseForge before it is saved: a key CurseForge
  refuses (401/403) is not stored. When CurseForge cannot be reached the key is saved with a
  warning.

## Files the upgrade script writes

`deploy/scripts/upgrade.sh run` writes into `deploy/` (or into the folder named by
`MCPANEL_UPGRADE_BACKUP_DIR`), with mode 0600, owned by whoever runs the script (root on Unraid):

| File | Content |
|---|---|
| `upgrade-backup-<time>.sql` | A plain SQL dump of the panel database: users with their password hashes, server definitions, schedules, backup metadata, the audit log and the encrypted secrets (RCON passwords, provider API keys, node tokens, webhook URLs). Login sessions are left out, so the dump cannot be used to act as a logged-in user. |
| `upgrade-backup-<time>.env` | A copy of `.env` from that moment: every secret of the stack in clear text, among them `MCPANEL_SECRET_KEY`, which decrypts the secrets in the dump. |
| `upgrade-state.env` | The rollback point: git revisions, image names and ids, names of database migrations, the paths of the two files above. No secrets. |

The newest three dumps are kept with their `.env` copies; older ones are deleted by the next
run. All three kinds of file are ignored by git (`deploy/*.sql`, `deploy/*.env`). A dump
together with its `.env` copy is everything needed to impersonate the panel: keep the source
folder off network shares and store copies like passwords.

The images of the version before the last upgrade stay on the host under the names
`mcpanel-rollback/...`. They contain no secrets (secrets come from `.env` at start).

## The source folder

Root runs `deploy/scripts/upgrade.sh`, `deploy/unraid/install-hook.sh` (at every array start; it
installs `stopping_docker`, which root runs at shutdown) and `deploy/unraid/backup-db.sh` (daily)
from the checkout, and `upgrade.sh` builds the panel and the agent (which gets the Docker socket)
from it. Whoever can change a file there runs code as root at the next upgrade, array start,
shutdown or nightly dump. On the owner's server (review of 2026-11-11) the scripts were mode 777 and
the compose file 666, in the `appdata` share, which is exported over SMB.

* **`upgrade.sh` refuses before it changes anything.** `upgrade.sh run`, `rollback` and
  `restore-db --yes` refuse (exit 2) while group or others can write a file or folder that git
  tracks, the checkout's own folder, `.git`, `.git/config`, `.git/hooks` or `deploy/.env` (followed
  when it is a symbolic link); `status`, `dry-run` and `rollback --dry-run` only warn.
  Every refusal lists what it found and names the repair: `chmod -R go-w <checkout>`, and
  `chmod go-w <path>` for a path outside the checkout (a copy of the hook files in
  `/boot/config/mcpanel`, an `.env` given with `--env`).
* **The unattended two repair it themselves.** `install-hook.sh` (at every array start) checks
  itself, `stopping_docker` and their folder; `backup-db.sh` (every night) itself, its folder and
  the `.env` it copies. When group or others can write one of them, a run as root from the MCPanel
  checkout runs `chmod -R go-w` on the checkout (its real path: no symbolic link on the command
  line, and `chmod -R` does not follow the links it meets, so nothing outside the checkout is
  changed), writes "permissions of <checkout> were loose and have been tightened ...; if you did
  not expect this, check 'git status' there" to stderr and the syslog (tag `mcpanel`), and goes on.
  A refusal nobody reads would leave the server without its shutdown hook or the panel without its
  nightly dump. Not as root, for a path outside the checkout, or when the `chmod` fails or leaves
  something loose, they refuse as above (`install-hook.sh status` only warns). No self-check
  protects against a script someone already changed; what it does is close the loose state and
  tell you.
* **git as root.** `upgrade.sh` runs git with `-c core.fsmonitor= -c core.hooksPath=/dev/null`, so
  no command from `.git/config` or `.git/hooks` runs, and under `umask 022`: a `rollback` checkout
  never leaves files writable by others. `install-hook.sh` makes its folders under `umask 022` too.
* **`git pull` is yours.** It writes the files it changes with the umask of your shell, and the
  Unraid terminal can leave them writable by everybody. Pull with `(umask 022 && git pull)`; the
  documented commands do.
* **What the check cannot see.** Whoever can write the folder that holds the checkout
  (`/mnt/user/appdata` on Unraid: mode 777 on the owner's server) can rename the whole checkout and
  put another one in its place. The paths in User Scripts and `/boot/config/go` then run that one,
  and its scripts check nothing. So keep that folder out of other people's reach as well: do not
  export `appdata` over SMB or NFS with write access for other users (Shares → appdata → SMB
  Security Settings), and do not give containers you do not fully trust a mount of `/mnt/user` or
  of `appdata` as a whole. Files git does not track are not checked (adding one needs write access
  to a folder that is).

## Images

* The panel and agent images are built on your host and never pulled (`pull_policy: never`): an
  image of the same name from a registry would get every secret of the stack, and the agent
  image the Docker socket.
* Both are built from `python:3.13-slim-trixie`. The Debian release is part of the tag, so a
  rebuild with `--pull` (what `upgrade.sh run` does) brings security fixes of that release and
  never a new Debian release unasked. The agent's extra packages (`btrfs-progs`,
  `zfsutils-linux`) come from that Debian release; restic is copied from `restic/restic:0.19.1`.
* Redis in the panel image is copied from the official `redis:7.4` image; the build fails if the
  binary is not a 7.4 release.
* These are tags, not digests: what a rebuild fetches is decided by Docker Hub. Rebuild at least
  monthly (DEPLOYMENT.md, "Upgrading").

## The Unraid shutdown hook

`deploy/unraid/stopping_docker` (docs/UNRAID-SHUTDOWN.md) is run by Unraid as root when Docker is
about to stop. The only thing it calls is `docker`: `version`, `ps`, `exec <panel container>
python manage.py host_shutdown` and, when no panel container runs, `stop` on containers with the
label `io.mcpanel.managed=true`. What the panel command prints goes to the syslog line by line
and is not executed. The Compose project
name given to `install-hook.sh --project` is checked against Compose's own pattern before it is
written into the installed copy.

The server name a container carries in its `io.mcpanel.name` label has every `=` replaced by
`-`. Unraid decides which containers are its own by searching the output of `docker ps` for the
text `net.unraid.docker.managed=`; a server name must not be able to produce that text, or Unraid
would stop that server itself at array stop and count it as stopped by hand.

## The agent treats the Minecraft process as hostile input

* The instance-network source check also covers WebSocket handshakes (the console log stream),
  which are closed with code 4403 before they are accepted.
* RCON replies are bounded (1 MiB per packet, 4 MiB per command, all fragments within the command's
  timeout); longer output is cut off with a note. `save-on` is always sent once `save-off` was sent,
  even when `save-all flush` fails, times out or the backup/snapshot itself fails.
* Console streaming keeps at most 16 KiB per line (longer lines are truncated) and a bounded backlog
  per viewer; a viewer that cannot keep up loses the oldest lines and is told how many.
* The status ping that counts players (`agent/mcagent/slp.py`) reads at most 2 MiB, at most 100
  sample names of 64 characters, a MOTD of 1024 and a version of 256 characters, and strips `§`
  formatting codes. It replaced the minute's RCON `list` (which logged two lines per minute).
* Text a server wrote is decoded strictly: a file that is not UTF-8 is refused (415 `not_utf8`)
  rather than decoded with replacement characters and saved back damaged; `*.properties` is
  read and written as ISO-8859-1. A file name that is not valid UTF-8 no longer breaks a folder
  listing or the mod inventory: it is shown with U+FFFD (`name_valid: false`), and the panel does
  not rename, move or delete it.
* Mod hashing never reads a file larger than 2 GiB (it is listed unhashed), and callers share one
  background pass per folder: a caller waits at most 240 s, then gets 503 `hashing_busy`, so
  repeated requests for a sparse or huge file cannot tie up the agent's threads without bound.
* A crafted `plugin.yml` cannot freeze the panel: the SpigotMC reader scans at most 2,000 lines
  of at most 1,000 characters each, in linear time.
* Typing `stop` in the console is not sent to the server (Docker would start it again at once):
  with the permission to start and stop the server the panel runs its Stop job instead and audits
  it as `instance.stop` from the console; without that permission the command is refused.
* File reads (editor, downloads, hashing, search, zip export, archive import) refuse FIFOs, sockets
  and device nodes without blocking; clones skip them.
* Archive imports also bound the archive's own index: the zip central directory (128 MiB, 500 000
  entries) is measured before it is loaded, tar headers are counted as they are read, pax/GNU
  long-name headers are limited to 1 MiB each and 64 MiB in total, GNU sparse members are refused.
* An abandoned folder-zip download stops its producer thread, and a zip whose producer died ends
  with an error instead of hanging. restic's output pipes are drained concurrently and a restic
  backup/restore is stopped after 12 hours, with SIGINT first so that restic removes its lock.
* A backup file download whose restic fails ends the response with an error instead of an empty
  or cut-off HTTP 200. Single-file restores escape `[ ] * ?` before passing a name to restic.
* World saving that a backup switched off (`save-off`) is switched on again even if the agent
  process ends during the pause (a marker file; `save-on` at shutdown, at start and every 30 s).

## Games other than Minecraft (Factorio, Satisfactory, Terraria, and six Steam games)

Each game is reviewed code: a definition in the panel and a runtime profile in the agent
(ARCHITECTURE.md, "Games"). The panel and users still cannot choose images, mounts, capabilities,
users or devices; the agent looks them up in the profile of the spec's `game` and refuses a game it
has no profile for.

* **Images:** a profile allows only its own repositories (`AGENT_GAME_<ID>_IMAGES` replaces the list;
  Factorio: `factoriotools/factorio`) and its tag pattern (Factorio: an exact `<version>-rootless`
  tag; the regular tags start as root and need four capabilities, so they are refused).
* **Privileges:** Factorio's rootless image runs as the runtime uid:gid set by the agent
  (`MC_UID`/`MC_GID`), with no capabilities at all, `no-new-privileges` and the server folder as its
  only mount: stricter than the Minecraft containers.
* **Environment:** an allow-list per game, enforced by the panel and again by the agent. Factorio
  accepts only `TZ` and the variables the panel sets (`DLC_SPACE_AGE`, `GENERATE_NEW_SAVE`,
  `LOAD_LATEST_SAVE`, `SAVE_NAME`), each with a value pattern; the image's variables that download
  mods with credentials (`UPDATE_MODS_ON_START`, `USERNAME`, `TOKEN`) or move its files (`CONFIG`,
  `SAVES`, `*_FILE`, ...) are refused.
* **RCON is never published.** The profile's `never_publish` (Factorio: 27015) is enforced by the
  agent whatever the spec says; the agent reaches RCON over the instance network.
* **The RCON password is visible on the host.** The Factorio image passes it to the server on its
  command line (`--rcon-password`), so anyone who can list the host's processes (`ps`) can read it,
  and its entrypoint runs with `set -x`, which prints that command line into the container's log.
  The agent replaces the password with `********` in every log text it hands out (the console, the
  log download, the last-run record), so console viewers in the panel do not see it; `docker logs`
  on the host still shows it. It only grants RCON, which is reachable from the instance network
  only, which every game server shares ("What a compromised game server can reach"). The panel
  writes it to `config/rconpw` before every start (from the server's encrypted control secret); the
  file API never hands that file out (download, zips: the agent leaves it out,
  backup browser) and never writes, renames or deletes it or its folder (403 `managed_file`); it is in
  backups (restic, encrypted).
* **Secrets per server** (`Instance.secrets`, for tokens a game needs) are encrypted like the RCON
  password, never returned by the API (only whether each is set), and audited by name only.
* **Game settings** with secrets (Factorio's game password and factorio.com token in
  `config/server-settings.json`) are masked in the Game settings form; they are plain text in the
  server folder, where the game reads them.
* **Public listing** is off by default for Factorio (`visibility.public=false`); the image's example
  file would list the server publicly.
* **Exits from inside the game** (`/quit`) are not sent: the console runs the panel's Stop instead,
  and the API and schedules refuse them (400 `stop_via_power`). Minecraft too (a deliberate change):
  `stop`, `minecraft:stop` and any line whose final `run` target is stop (`execute ... run stop`,
  `return run stop`, nested, any case, with or without slashes).
* **Extract and import never write secret files.** The panel sends the agent the paths to protect
  (`secret_files.extract_protected`: Factorio `config/rconpw`; Satisfactory the whole `.mcpanel/` folder
  and `saved/ServerSettings.7777.sav`); the agent (feature `extract_protected`) skips every archive
  entry whose final target (after normalisation, the strip of a common folder and the destination,
  compared without case) is one of them or inside one, and a file entry that would replace a folder
  above one; the job log names the skipped paths. A destination inside a protected path is refused
  (403 `managed_file`), and an agent without the feature is refused for games with secret files (409
  `agent_outdated`). TShock's `config.json` is extracted like a file-API write (the panel puts its
  REST token back before every apply). Found by the real-container gate of 2026-11-11 (an extract
  overwrote Factorio's RCON password file and Satisfactory's token file).
* **Image VOLUMEs** are mounted into the server folder (or a tmpfs the profile names), so no data of
  a game server lives in an anonymous Docker volume outside it.

### Console input: the only exec, and stdin

Games that take commands on their console only (Terraria) get one line at a time
(`agent/mcagent/drivers/console_io.py`):

* **Exec is allowed in exactly one form:** a profile names a fixed argv template with exactly one
  element `{line}` (PassiveLemon's Terraria image: `["inject", "{line}"]`, a script that types the line
  into the server's tmux pane). The template is checked when the profile is loaded (a program first,
  one `{line}` element, no other templated element). The agent runs it through Docker's exec API with
  the line as that one argument, never through a shell, without a TTY or stdin, not privileged
  (`no-new-privileges` applies), as the uid:gid the server runs as (the container's `PUID`/`PGID`).
  Nothing of a request chooses the program, the user or any other argument; there is no other exec in
  the agent.
* **stdin:** for images whose server reads its stdin (TShock run directly) the line and a newline are
  written through Docker's attach API to a container the profile created with `stdin_open` (no TTY).
  Nothing is read back through the attachment.
* **Every line is sanitised** before either: surrounding blanks removed, not empty, at most 256
  characters, no control, format (zero-width, bidi), surrogate, private-use or line/paragraph separator
  characters, so a line can never become two commands. Consoles typed through `tmux send-keys` also
  refuse what tmux would not type as text: a single word that is a key name (`C-c`, `Escape`, `0x03`,
  `F1`, ...), a line starting with `-` (an option of `send-keys`) or ending with `;` (tmux's command
  separator). A refused line is 400 `console_line_refused` and never reaches Docker.
* **One writer per server** (a per-server console lock; 409 `console_busy` after 30 s). A command's
  answer is the container log read after the write (bounded), passed through the same redaction as
  every other log text.
* Exits from inside the game (`exit`, TShock's `off`) are turned into the panel's Stop like Factorio's
  `/quit`.

### Terraria: privileges, the TShock REST token, the setup code

* **Vanilla and tModLoader** need root with CHOWN, DAC_OVERRIDE, FOWNER, SETUID and SETGID (the image
  creates its user and chowns the server folder at every start; `--user` fails), with
  `no-new-privileges`; the server runs as `MC_UID`. The image writes its variables unescaped into its
  config file, so the profile refuses all of them except `TZ` (and tModLoader's `MODPACK`, a bare name):
  the panel writes `serverconfig.txt` itself and the server reads that (`SERVERCONFIG=1`).
  tModLoader's launcher downloads the .NET runtime from Microsoft at every container creation (code
  from the internet, into the container layer); that is the image's design and cannot be switched off.
* **TShock** runs with **no capability** as `MC_UID:MC_GID`: the profile replaces the image's root
  bootstrap script (which reads `config.json` with `jq`, can run a `mysql` client with a password on its
  command line, and ignores stop signals) with the server binary, a fixed argument list, `/tmp` as
  working folder and fixed `HOME`/`DOTNET_BUNDLE_EXTRACT_BASE_DIR`. None of this is expressible in a
  spec (`RuntimeProfile.entrypoint`/`command`/`working_dir`).
* **The REST token.** TShock's REST API (port 7878) is never published; the agent reaches it over the
  instance network. The panel generates the token (the server's control secret, `Instance.rcon_password`,
  encrypted in the database) and writes it before every start into `config.json` as the only application
  token (`ApplicationRestTokens`, user `mcpanel`, group superadmin), together with `RestApiEnabled`,
  the port and `LogRest=false`; these keys are managed (refused in Game settings) and put back at every
  apply. The agent's driver reads the token from that file (no symbolic link followed, size-capped);
  it travels only in the query string of requests to the container (TShock's design) and never appears
  in an error message or log line of the agent; the agent also masks it in console text. The text
  editor, downloads (also from the backup browser) and search results show `config.json`, and any
  file the token was copied into, with the token masked; a zip leaves `config.json` out for users
  without `instance.configure`. It is in backups (restic, encrypted). It grants superadmin over that TShock
  server only.
* **The one-time `/setup` code** TShock prints at its first start (it makes whoever types it in game an
  admin) is masked in every console text the agent hands out. The panel creates the owner account itself
  (setup step `owner_account`: password from the secret `owner_password`, sent to the agent in the
  request only, never logged or stored in a file by the panel), and TShock switches the code off at its
  next start. `docker logs` on the host still shows it, and `setup-code.txt` stays in the server folder
  until that restart.

### Satisfactory: claiming, the API token, internal-only TLS

* **Claiming.** An unclaimed Satisfactory server belongs to whoever reaches its API first (a
  passwordless `InitialAdmin` login works until it is claimed). The panel never publishes such a
  server: its spec has no ports until the setup's claim, join-password and game steps are done
  (`setup_state.ports_open`). Until the claim, another game server on the instance network can reach
  the API and claim it first ("What a compromised game server can reach"); the setup then stops
  with `claimed_elsewhere` and the server stays unpublished. The game writes
  `saved/ServerSettings.7777.sav` before anybody claimed it, so that file proves nothing; what counts is the **claim marker** `.mcpanel/claim-verified`
  (size, `mtime_ns` and SHA-256 of that file), which only the agent writes, after it verified the
  claim through the API (an `InitialAdmin` login is refused, an `Administrator` login with the panel's
  admin password works) on a container **without published ports**, or right after the panel's own
  change of the file (join password, new game, a load) on a published container whose marker matched
  right before. The file API never writes the marker or the start wrapper (agent: 403 `managed_file`;
  renames into or out of `.mcpanel/` are refused too), and extracts/imports skip `.mcpanel/` entirely.
* **The start wrapper (fail closed inside the container).** The container's entrypoint is the agent's
  `.mcpanel/start-guard.sh` (`/bin/sh`, written by the agent before every create and start it makes,
  mode 0444). A container with published ports (`MCPANEL_PORTS_PUBLISHED` set at creation, reserved
  for the agent) runs the image's `/init.sh` only while the marker matches the settings file;
  otherwise it prints why and exits 78 before the game starts. So a hand `docker start`, a `docker
  restart` or any other start outside the panel of a server whose claim may be lost never gets an API
  answering on a published port (verified on real containers on 2026-11-11: 9,534 probes every 100 ms
  from the host, none answered as unclaimed). The restart policy is `no`: Docker never starts the
  server by itself (the panel's jobs retry SteamCMD's failures). Limits: the image's `chown -R /config`
  gives the wrapper and the marker to the runtime uid, so a process inside the container could change
  them for a later start; the agent writes the wrapper again before each of its own starts, and the
  defences below still apply.
* **The agent's checks.** Before every start of a container with published ports (apply, start,
  restart) the agent requires the claim file, a matching claim marker, the API token file and, for a
  running container, an API that answers as claimed within 20 s (409 `server_unclaimed`, or
  `claim_unverified` when it cannot tell). A loop on the agent (every 2 s, and at once on Docker's
  `start` events) stops a freshly started published run without a matching marker before its API is
  up, probes every published run as soon as its API answers and stops an unclaimed one, records the
  wrapper's refusals (exit 78) and removes the marker when the settings file changes outside an
  operation of the panel (an admin in the game). The marker is removed whenever the claim may be lost:
  the panel's `mark_unclaimed` (restores, restore of a path or a snapshot, file uploads, edits,
  renames, deletes, extracts and imports under `saved/`, the agent's refusal, `guard_stop`, exit 78,
  a re-run of the claim step) asks the agent to remove it, and the agent removes it itself on every
  file operation under `saved/`, every restore, snapshot rollback and clone. The panel then closes the
  ports, recreates the container without them before any start, and the claim, join-password, game
  and publish steps run again (the publish step has the agent verify the claim on the unpublished
  container first). Steps run in order (409 `setup_order`). A server never joins another network than
  the agent's instance network. The admin password is the server's control secret (encrypted in the
  database, sent to the agent only in the claim and verification requests, never written to disk).
* **Client logins.** After the claim anyone who reaches the server can still log in as a client
  (and read its state) until a client password is set. The create wizard requires an explicit
  choice: a join password, or "open to anyone who knows the address"; the setup applies it and
  checks that it took effect. Clearing the password later in Settings means "open" and needs that
  choice in the request (`"access": "open"`; the UI asks first).
* **API token.** The claim makes a non-expiring API token (`server.GenerateAPIToken`). The agent
  returns it once, in the claim's answer; the panel stores it encrypted (`Instance.secrets`, not
  listed or settable through the secrets API) and writes it to `.mcpanel/api-token` in the server
  folder before every apply, where the agent's driver reads it (no symbolic link followed). Like
  Factorio's RCON password file and the claim file, the file API never hands it out (download, zip,
  backup browser, search) and never writes, renames or deletes it or a folder holding it (403
  `managed_file`; `apps/instances/secret_files.py`); it is in backups (restic, encrypted). It grants
  admin over that game server only, which listens on the instance network and its own published
  ports. The game server itself can read it (it stores its tokens in `saved/ServerSettings.7777.sav`
  anyway). It is never logged; job logs and audit details name steps and keys only.
* **Internal-only TLS.** The game's API uses a self-signed certificate. The agent connects only to
  the server's own container address (from Docker, right before each request; the published port on
  127.0.0.1 with `AGENT_RCON_MODE=published`), with a TLS context of its own that skips certificate
  verification for that hop only: every other HTTPS connection (panel to agent, catalog downloads)
  keeps full verification. The certificate seen first in a container's run is pinned for that run;
  a different one is refused (`api_tls_changed`). Requests are bounded (3 s to connect, at most
  600 s in all, answers at most 8 MB); the agent's `POST /game/{action}` accepts only the driver's
  named operations, with parameters it validates (names, save names, option keys, sizes).
* **Privileges.** The image needs root mode on Unraid (it runs unprivileged as uid 1000 only): the
  profile adds exactly CHOWN, DAC_OVERRIDE, SETUID and SETGID (fewer fail at start), with
  `no-new-privileges`; the image drops to `MC_UID`/`MC_GID`. The game port stays 7777 in the
  container (a different port would be a new, unclaimed server). Its variables that download other
  branches, skip updates or print every variable (`STEAMAPPID`, `STEAMBETA*`, `SKIPUPDATE`,
  `DEBUG`, ...) are refused by the panel and the agent.

### Palworld, Valheim, Project Zomboid, HKMP, Counter-Strike 2, Rust

The same rules as above: a reviewed definition and profile per game, the image and tag allow-list,
an environment allow-list with a pattern for every value (the panel checks it, the agent again),
the control ports in `never_publish`, `no-new-privileges`, only folders inside the server's own
folder mounted, and `restart_policy: no` (Docker never starts these servers by itself). Their images download the game
from Steam at every start (HKMP's server is in its image): code from the internet, into the server
folder. Rust's image is pinned by digest; the others are exact version tags, which whoever controls
the image's repository can still move.

| Game | Who runs the game | Capabilities | Control port (never published) |
|---|---|---|---|
| Palworld | the image starts as root and drops to `MC_UID`/`MC_GID` | CHOWN DAC_OVERRIDE FOWNER SETUID SETGID | REST API 8212/tcp (HTTP Basic with the admin password); RCON 25575/tcp is off |
| Valheim | root at start; supervisord stays root and runs the game as `MC_UID`/`MC_GID` | CHOWN DAC_OVERRIDE SETUID SETGID KILL | none (no remote console); the image's ports 80 and 9001 are off |
| Project Zomboid | **root**: the image never drops privileges | CHOWN DAC_OVERRIDE FOWNER | RCON 27015/tcp |
| HKMP | the image starts as root and drops to `MC_UID`/`MC_GID` | CHOWN DAC_OVERRIDE FOWNER SETUID SETGID | none (console input through the one exec) |
| Counter-Strike 2 | uid 1000:1000 from the start (the image's user) | none (Docker's init as PID 1) | RCON on the game port's number over TCP |
| Rust | the image starts as root and drops to `MC_UID`/`MC_GID` | CHOWN DAC_OVERRIDE SETUID SETGID | WebRCON 28016/tcp and its web page 8080/tcp |

* **Project Zomboid's game runs as root** with three capabilities: the process that reads packets
  from the internet on its UDP ports is uid 0. Unraid has no user namespaces, so uid 0 in the
  container is uid 0 of the host, held back only by the namespaces, the capability set and Docker's
  seccomp profile. Treat a Project Zomboid server (and its mods) as the most privileged of the six.
* **Secrets on the command line.** Project Zomboid's image passes the admin password as
  `-adminpassword`, and Rust's start line carries the RCON password: anything that can list the
  host's processes (`ps`) reads them, and the launchers print them into the container log. The
  agent masks them in every log text it hands out (console, log download, last-run record, command
  replies); `docker logs` on the host still shows them. Counter-Strike 2's image writes the RCON
  password into `server.cfg` at every start; the file API never writes that file, and the editor and
  downloads mask it.
* **Image defaults never survive.** The panel generates the control secret (32 characters, 191
  bits) and the admin passwords (Palworld 24 characters, Project Zomboid 20), and the agent always
  replaces the images' defaults (CS2's `changeme`, Rust's `docker`, Project Zomboid's `admin`); an
  injected value must match the variable's pattern.
* **Who owns the files.** `MC_UID`/`MC_GID` is 99:100 on Unraid (`nobody:users`), the owner of all
  share data, and Counter-Strike 2's fixed 1000 is normally the first Unraid user account. A game
  that breaks out of its container as that uid, even without root, reaches what that uid owns.
* **Who can join** once a port is forwarded: docs/GAMES.md, "Who can join by default". Rust has no
  password or whitelist in the panel, and Project Zomboid lets anyone create an account by default.

## Reporting

This is a self-hosted project; open an issue on the repository for security problems that do not put
other users at risk, otherwise contact the maintainer privately.
