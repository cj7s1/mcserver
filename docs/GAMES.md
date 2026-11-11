# Game notes: Palworld, Valheim, Project Zomboid, HKMP, Counter-Strike 2, Rust

What you need to know before you create a server of one of these games: which ports to forward on your
router, how long the first start takes and how much disk and memory it needs, and the few rules of each
game. Minecraft, Factorio, Satisfactory and Terraria are described in the [README](../README.md) and
[docs/UNRAID.md](UNRAID.md) section 5.

All six have in common:

* **No version to choose.** The image downloads the game from Steam (Project Zomboid: with
  DepotDownloader) on the first start and checks for an update at every start, so the server always runs
  the build players' clients have. "Update" means restart. (HKMP's server comes with the panel's image.)
* **Ports are published 1:1**: the server listens on the same number inside its container that you
  forward on your router, because the game tells Steam and players that number. Every port is UDP unless
  this page says TCP. The panel picks the game's usual port when it is free, else the next free one.
* **Settings** are a form under Settings -> Game settings. Most of them are passed to the server when it
  starts: the server then shows "restart required".
* **Secrets** (passwords, tokens) are under Settings -> Game settings -> Secrets. They are stored
  encrypted and never shown in logs; the agent hides them in the console as `********`.
* Docker never restarts these servers by itself: a crash leaves the server stopped, and its Diagnostics
  name the usual causes (the game download failed, the disk is full, not enough memory).
* **Stop works while the first start downloads the game** (Counter-Strike 2, Rust, Project Zomboid): it ends
  the job that waits for the server and stops the container.
* Files that may hold the server's passwords (Project Zomboid's logs and ini, Counter-Strike 2's
  `server.cfg` and logs, Rust's logs, Palworld's settings) are masked in the editor and in downloads and
  are never put into a zip; Project Zomboid's own backup zips are not handed out at all (the panel switches
  them off and takes its own backups).

## Palworld

* **Ports:** forward the game port, UDP 8211 by default. The REST API the panel uses (8212) stays inside
  the panel's Docker network.
* **First start:** about 1-2 minutes; the game takes about 4.6 GB in the server folder (left out of
  backups). Later starts take seconds.
* **Memory:** at least 8 GB, 16 GB by default; big worlds and many players need more.
* **Admins:** there is no admin list. Players become admins in game with `/AdminPassword <password>`. The
  admin password is generated when the server is created; show it under Secrets (the panel uses the same
  password for its own access to the server, so it cannot be empty).
* **Join password:** optional (Secrets). "List on the community server browser" is off by default.
* **Players:** the online list, kick and ban by the player's user id (`steam_<SteamID64>`, shown next to
  online players; the name of an online player works too), unban while stopped or running.
* **World:** Saves shows the world folder; reset starts a new world, import takes a zip of a world folder
  from `Pal/Saved/SaveGames/0/` (with `Level.sav`), download gives you a zip of the current world (the
  game's own rolling backups left out).

## Valheim

* **Ports:** forward the game port and the next one (UDP 2456 and 2457 by default). With crossplay on, also
  game + 2 (2458). You choose only the game port; the others follow it.
* **First start:** about a minute; the image keeps the game twice (about 4 GB on disk, left out of backups).
* **Memory:** at least 4 GB, 8 GB by default.
* **Join password:** required. At least 5 characters (letters, digits and `_ . , : ! ? @ # % + = -`), and it
  must not contain the server name: Valheim refuses such a password.
* **No console:** Valheim has no remote console, so the panel cannot list or kick players. Admins, bans and
  the permitted list (whitelist) are files the panel edits with SteamID64s (17 digits); Valheim reads them
  when it starts, so a change applies at the latest at the next start.
* **World:** the world name is chosen once (a reset can start a new world under a new name). Import takes
  a zip of a world folder from `worlds_local/` (or an older world's `<name>.fwl` + `<name>.db`).
  Valheim cannot save on request: a download of a running server holds its last save.

## Project Zomboid

* **Ports:** forward both UDP ports, 16261 and 16262 by default. RCON stays inside the panel's network.
* **First start:** a few minutes; the game takes about 7 GB on disk (left out of backups). Every start
  validates the game files (about half a minute).
* **Memory:** at least 4 GB, 8 GB by default. The Java heap is the server's memory minus 2 GB.
* **Build:** chosen once: Build 42 (Steam's public branch) or Build 41 (`legacy41`). Players need the same
  build.
* **Admin login:** the server has an in-game account `admin`. Its password is generated when the server is
  created; show it under Secrets, then log in with `admin` and that password.
* **Server name** (chosen once) names the settings, world and database files (`data/Server/<name>.ini`,
  `data/Saves/Multiplayer/<name>/`). The settings form edits the ini; the sandbox settings are
  `data/Server/<name>_SandboxVars.lua` in the file manager.
* **Players:** the online list, kick, ban/unban, admin and removing from the whitelist go through RCON while
  the server runs; Project Zomboid keeps accounts and bans in its database, so the panel cannot list them.
  A whitelist entry is an account with a password: create it in the console (`adduser "name" "password"`)
  and give the player both, or let players create their own accounts (`Open` on in the server settings).

## Hollow Knight (HKMP)

* **Ports:** forward UDP 2222 (by default).
* **Start:** seconds; about 25 MB of memory (512 MB by default). Nothing is downloaded.
* **Version:** the HKMP server comes with the panel's image; players need the HKMP mod of the same
  version (2.4.3).
* **No shared world:** every player keeps their own save; there is nothing to back up but the settings.
* **Console:** commands such as `list`, `announce <text>`, `kick <name>`, `whitelist on`. The settings files
  (`serversettings.json`, `whitelist.json`) are written by the server itself: edit them while it is stopped.

## Counter-Strike 2

* **Ports:** forward the game port on UDP, 27015 by default (and the CSTV port, UDP 27020, while CSTV is
  on). RCON uses the same number on TCP inside the panel's network: never forward the TCP port.
* **First start:** the whole game is downloaded: about **60 GB** in the server folder. Check the free
  space first; on a slow line the first start takes hours (the panel waits up to 4 hours for it).
* **Memory:** at least 2 GB, 4 GB by default.
* **Game Server Login Token (GSLT):** without one the server works on your LAN but is not listed and players
  outside your network may not find it. Create one for app **730** at
  <https://steamcommunity.com/dev/managegameservers> (one token per server) and paste it under Secrets.
* **Settings:** server name, players, game mode, start map, map group, LAN only, bots, CSTV. Only letters,
  digits, spaces, `.`, `_` and `-` are allowed in the name (the image runs its start line through the
  shell). `server.cfg` is written by the image at every start and cannot be edited.

## Rust

* **Ports:** forward the game port and the query port on UDP (28015 and 28017 by default), and with Rust+
  on the app port on TCP (28082). WebRCON stays inside the panel's network.
* **First start:** the game (about 10 GB) is downloaded and the map is generated: several minutes, longer
  for big maps.
* **Memory:** at least 10 GB, 16 GB by default.
* **Map:** size and seed are settings; the seed is chosen at random when the server is created and stays
  the same. A world reset is a map wipe: the map, the saves and the world's databases are moved aside
  (blueprints only when you choose so), owners and bans stay, and the new map gets the seed you enter or a
  random one. Import takes a zip of a server identity folder (`server/<identity>/`).
* **Players:** the online list, kick, ban/unban and owners (admins) by SteamID64 (an online player's name
  works too).
