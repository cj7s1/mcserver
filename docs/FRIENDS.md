# Playing with friends

How to let friends join your game servers, start them from Discord, and (if you want) use the panel themselves.
Three independent parts: use the ones you need.

> **Status (2026-11-12):** new in this version and **not tried with real players yet**. The panel side is
> unit-tested; the Discord bot was tested only against stand-ins for Discord (the real login, the slash
> commands in a Discord server and the invite link have not been tried). Several games' join steps were
> written from general knowledge: they are marked **check this for your game version** below.
>
> **Upgrading to this version changes the database** (one migration): the upgrade needs
> `./scripts/upgrade.sh run --with-migrations`, or the tick box of the Unraid plugin's Update
> (DEPLOYMENT.md, "Upgrading": what a rollback loses).

## 1. Share a server

### The Share card

Every server's **Overview** has a **Share** card; the **Share** button at the top of the server's pages opens
the same. It shows what a friend needs, for the game of that server:

* **the address** to connect to, and each **port with its protocol** (TCP or UDP);
* **how strangers are kept out**: a join password, a whitelist, accounts, or nothing (and whether that is on);
* **the join steps in plain language** (where to click in that game) and **what the friend needs** (the
  same Minecraft version, the modpack, a mod such as HKMP 2.4.3, ...);
* **Copy address**, **Copy password** and **Copy invite** (a ready message for Discord or a chat);
* **a checklist**: the server runs, a public address is set, the protection is on, the ports to forward,
  and the warning when the game lets anyone in by default; a missing item links to where you fix it.

Everyone who can see the server sees the card. **Only users who may change the server's settings see the
join password**, and only they can copy the invite text with the password in it.

**What to send:** the invite text without the password, and the password another way (say it, or a
private message). An invite text with the password is one copy away from everyone in a group chat.

### The address friends use

The card uses **System** (left sidebar) → **Public address** (a DNS name such as `play.example.org`, or
your internet IP). Without it, the address of the server's node; without that, the card says none is set.
Your home IP changes from time to time: a free dynamic DNS name (DuckDNS, your router's own) keeps the
address the same.

### Forward the ports, but check who can join first

Players from outside your home need a **port forward** on your router for every port the card lists, to
your server's LAN IP, with the protocol it names. The panel never changes your router. Before you forward a
game's port, read [GAMES.md](GAMES.md), "Who can join by default": Terraria, Palworld, HKMP and
Counter-Strike 2 let anyone in until you set a password, Rust has no password or whitelist in the panel,
and Project Zomboid lets anyone create an account. On Unraid, also install the game servers' firewall
([UNRAID.md](UNRAID.md), "The game servers' firewall"): a game server runs code you do not control, and
without it a compromised one reaches your server and your LAN.

### Keep strangers out

* **Minecraft: the whitelist.** In the Share card, **Add a friend** takes a Minecraft name, puts it on the
  whitelist and offers to switch the whitelist on. The Players page lists and edits it too. With the
  whitelist on, only the names on it can join.
* **Valheim, Palworld, Counter-Strike 2, Satisfactory:** the join password is under Settings → Game
  settings → **Secrets** (Valheim requires one; Satisfactory asks at creation).
* **Factorio:** the game password in the server's game settings. **Terraria:** the server password in its
  settings (`serverconfig.txt`; TShock: its own config).
* **Project Zomboid:** switch "Anyone may create an account" off; then only accounts you created can join.
* **HKMP:** its whitelist. **Rust:** the panel offers none (GAMES.md).
* **Valheim with crossplay** (Xbox and Game Pass players): they join with a **join code** instead of the
  address. Valheim prints it in the server's log when it starts: the server's **Console** page.
  **Check this for your game version** (the code is new at every start, as far as we know).

### The join steps per game

The card shows the steps; this is where they came from:

| Game | The steps on the card | |
|---|---|---|
| Minecraft | Multiplayer, Add Server, the address, Join Server; the version and the modpack from the server | from the server's definition |
| Factorio | Multiplayer, Connect to address | **check this for your game version** |
| Satisfactory | Server Manager, Add Server, the address, then Join Game | **check this for your game version** |
| Terraria | Multiplayer, Join via IP: character, address, port; TShock: `/register`, then `/login` | **check this for your game version** |
| tModLoader | as Terraria; the server's mods are offered for download when you join | **check this for your game version** |
| Palworld | Join Multiplayer Game, the address field at the bottom | **check this for your game version** |
| Valheim | Start Game, a character, Join Game, Add Server; the crossplay join code from the console | **check this for your game version** |
| Project Zomboid | Join, Add, the IP, port and account; Build 41 or 42 as the server | **check this for your game version** |
| HKMP | install HKMP 2.4.3 with a mod installer (Lumafly), pause, the menu at the top left | **check this for your game version** |
| Counter-Strike 2 | Enable Developer Console, `~`, `connect <address>`, `password <password>` | **check this for your game version** |
| Rust | F1, `client.connect <address>` | **check this for your game version** |

When a step is wrong for your version, tell the friend the right one, and please report it.

## 2. Stop when empty

A server that nobody plays on can stop by itself: the server's **Settings → Resources → Stop when empty**, in minutes
(5 to 1440; 0 = off). The panel looks at the player count it samples every minute anyway. It stops the
server when it has been ready for at least that time (and at least 5 minutes), every sample of that time
said 0 players, there was no gap of more than 3 minutes in the samples, and the newest is less than 3
minutes old. It never stops a server while a job runs for it (a backup, an update) or while the count is
unknown. The stop is the ordinary Stop: the world is saved first.

You see it as the job "Stop (empty for N minutes)" and in the audit log; the notification **"Server stopped
because nobody played on it"** (event `instance.idle_stopped`) goes to the notification channels that
subscribed to it. The server stays stopped until someone starts it: in the panel, by a schedule, or from
Discord.

**Valheim cannot use it:** it has no way to tell how many players are online (no console, and no query on a
private server); its settings say so. Every other game can (Minecraft by the server list ping; the others
through RCON, the console, their REST API or the Steam query).

## 3. The Discord bot

Friends type `/start valheim` in your Discord server and the server starts. The bot is a program inside
the panel's container that **connects out to Discord only**: no port to open, nothing exposed.

### Set it up (once)

1. Open <https://discord.com/developers/applications>, **New Application**, any name ("MCPanel").
2. **Bot → Reset Token**, copy the token (Discord shows it once). Leave every **Privileged Gateway Intent**
   off: the bot needs none. Switch **Public Bot** off, so that only you can add it to a Discord server.
3. In Discord: User Settings → Advanced → **Developer Mode** on. Right-click your Discord server → **Copy
   Server ID**. For the roles that may start and stop servers: Server Settings → Roles → right-click a role →
   **Copy Role ID** (or pick them from the list in the panel once the bot is connected).
4. In the panel: **System → Discord**: paste the **Bot token** and the **Discord server id**, choose the
   **Roles that may start and stop servers**, optionally an **Announcement channel**, switch **Bot on** and
   save. The state turns **connected** within seconds. The token is never shown again (the page says it is
   set). Before the bot has connected, roles and channel are typed as IDs; afterwards you pick them by name.
5. Open **Invite the bot to your Discord server** (the link appears once the bot is connected) and add the
   bot to your Discord server. It asks for the scopes
   `bot` and `applications.commands` and no permission at all, or View Channels + Send Messages when you
   chose an announcement channel. If the bot cannot post there, give its role access to that channel.
6. **Switch servers on one by one:** each server's **Settings → General → Discord → Available in Discord**
   (off by default: the bot neither lists nor accepts other servers). There, optionally: **Roles that may
   start and stop it** (instead of the default roles), and **Let /join show the password** (then everyone in
   your Discord server can read it with `/join`).

The slash commands appear in your Discord server at once (they are registered for that server only).

### What friends type, and what they see

| Command | Who | Answer |
|---|---|---|
| `/servers` | everyone in your Discord server | the available servers, running or not, players online |
| `/status <server>` | everyone | its state, players and address |
| `/join <server>` | everyone | the Share card's invite text, visible to the member only; the password only with "Let /join show the password" |
| `/start <server>` | members with one of the roles | "starting...", in the channel; then "X is ready." or "X did not start: <why>" |
| `/stop <server>` | members with one of the roles | stops it (the world is saved first); with players online it asks to repeat it with `confirm: True` |

Server names complete as you type, from the available servers only. Refusals ("you need one of the roles",
"no server called X is available in Discord") are visible to the member only; starts and stops are answered
in the channel, and also posted in the announcement channel when you chose one.

### What the bot can never do

* run console commands, change settings, files, backups or anything else: it lists, starts and stops;
* start a server the web UI would refuse: the same rules apply (the memory check without "Start anyway",
  missing passwords, a server that is busy: the panel's message is the answer);
* answer in another Discord server: it answers only in the one you configured;
* show a secret other than the join password, and that only where you allowed it.

Every start and stop is in the audit log under the service user **`discord-bot`** (it cannot sign in) with
the member's Discord name and ID. Each member may start or stop 4 times in 10 minutes and use 20 other
commands a minute.

### When the page shows an error

* **"Discord refused the bot token":** the token was reset or mistyped. Developer portal → Bot → Reset Token,
  paste the new one, save.
* **"No connection to Discord"**: no internet, or Discord is down. The bot tries again by itself.
* **Not running:** the bot's process did not report for a while; `docker compose logs --tail 50 panel | grep
  -F '[discord]'` says why.
* **Connected, but no slash commands in Discord:** the bot was not invited with the link the page shows
  (it needs the `applications.commands` scope), or the server ID belongs to another Discord server.
* **"This bot only answers in its own Discord server."**: it was added to another Discord server than the
  one whose ID you entered.
* **No announcements:** give the bot's role View Channel and Send Messages in that channel.

Changes of the settings apply without a restart. To switch the bot off: **Bot on** off (or clear the token); to cut it off completely, Reset Token in the
developer portal.

## 4. Friends in the panel (optional)

A friend can also get an account in the panel, with the role **Friend**: they see the servers you granted
them, their status, the power buttons (start, stop, restart) and the Share card (never the password). Nothing else: no
console, files, settings, backups, players or other servers.

**Invite a friend:** **Users → Invite a friend**: a user name, optionally an e-mail address, and the servers.
The panel shows a link: send it privately. It works **once, for 24 hours**; the friend chooses a password
there. A new link later: the user's **Reset link** on the Users page. More servers later: the server's
Access page, with the role Friend.

### How a friend reaches the panel without making it public

**Never forward the panel's port on your router.** Two ways that keep it private (guidance; **check this
for your setup**, neither was tried with this panel yet):

* **Tailscale:** share the one machine with your friend (Tailscale admin console → Machines → the Unraid
  server → **Share**). They join with their own Tailscale account and reach only that machine; an access
  rule can narrow it to the panel's port. The panel must listen where that traffic arrives
  (`MCPANEL_BIND`) or sit behind Tailscale Serve (HTTPS on your tailnet name).
* **Cloudflare Access:** a Cloudflare Tunnel (the `cloudflared` container) to the panel, and a Zero Trust
  **Access application** in front of it that lets in only the e-mail addresses you list. Nothing is opened
  on your router.

Behind either, the panel needs the address your friend types (DEPLOYMENT.md, "HTTPS / remote access"):

```ini
MCPANEL_ALLOWED_HOSTS=panel.example.org                  # the name friends open (add it to the list)
MCPANEL_CSRF_TRUSTED_ORIGINS=https://panel.example.org
MCPANEL_BEHIND_TLS_PROXY=true                            # when Cloudflare or Tailscale Serve does the HTTPS
MCPANEL_FORWARDED_ALLOW_IPS=172.17.0.5                   # the proxy's address as the panel sees it
```

`MCPANEL_FORWARDED_ALLOW_IPS` is the exact address of the tunnel or proxy (DEPLOYMENT.md says how to find
it), never a whole range. The panel's own sign-in and the Friend role still apply behind both.
