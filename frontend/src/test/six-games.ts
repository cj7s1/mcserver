/**
 * The catalog entries (GET /api/catalog/games) of the six Steam-era games exactly as the backend builds
 * them (apps/games/{palworld,valheim,zomboid,hkmp,cs2,rust}.py after the review of feature/more-games,
 * `GameDefinition.catalog_entry()`: options with `pattern`, `fixed_version.updates_on_restart`),
 * so the tests run against the real shapes: derived and conditional ports, generated/revealable secrets
 * with patterns, virtual configs, folder worlds. To refresh: dump `[d.catalog_entry() for d in
 * apps.games.all()]` from a Django shell and replace the array.
 */
import type { GameInfo } from "@/lib/types";

const ENTRIES = [
  {
    "id": "palworld",
    "label": "Palworld",
    "variants": [],
    "capabilities": [
      "broadcast",
      "config_files",
      "consistent_backups",
      "diagnostics",
      "player_actions",
      "players",
      "world_import_export",
      "world_reset"
    ],
    "memory_model": "plain",
    "default_memory_mb": 16384,
    "notes": [
      "Dedicated server on the thijsvanloef/palworld-server-docker image. The first start downloads the game (about 4.6 GB on disk) and takes a minute or two; every start checks for an update.",
      "Needs at least 8 GB of memory (16 GB by default; big worlds and many players need more).",
      "Forward the game port on UDP. The REST API the panel uses stays inside the panel's network.",
      "Admins: players type /AdminPassword <password> in game; the admin password is under Settings -> Secrets."
    ],
    "ports": [
      {
        "name": "game",
        "label": "Game",
        "container_port": 8211,
        "protocol": "udp",
        "default_host_port": 8211,
        "help": "Players connect to this UDP port (Palworld's default is 8211). The server listens on the same number inside its container.",
        "one_to_one": true
      }
    ],
    "config_files": [
      {
        "id": "server",
        "path": "(container settings)",
        "codec": "json",
        "label": "Server",
        "help": "Kept by the panel and passed to the server at its start (restart to apply).",
        "fields": [
          {
            "key": "server_name",
            "type": "string",
            "label": "Server name",
            "pattern": "^(?! )[A-Za-z0-9 _.,:;!?()\\[\\]@#%+=/'-]{1,64}(?<! )$",
            "pattern_help": "1 to 64 letters, digits, spaces and . , : ; ! ? ( ) [ ] @ # % + = / ' - _ (no quotes or backslashes)",
            "default": "Palworld server",
            "initial": true
          },
          {
            "key": "description",
            "type": "string",
            "label": "Description",
            "pattern": "^[^\"\\\\\\x00-\\x1f\\x7f]{0,256}$",
            "pattern_help": "no double quotes or backslashes, at most 256 characters",
            "default": "",
            "initial": true
          },
          {
            "key": "max_players",
            "type": "int",
            "label": "Max players",
            "min": 1,
            "max": 32,
            "default": 16,
            "initial": true
          },
          {
            "key": "community",
            "type": "bool",
            "label": "List on the community server browser",
            "help": "Off: only players who know the address (and the join password) join.",
            "default": false,
            "initial": true
          }
        ],
        "managed_keys": [],
        "secret_keys": []
      },
      {
        "id": "gameplay",
        "path": "(container settings)",
        "codec": "json",
        "label": "Gameplay (PalWorldSettings.ini)",
        "help": "The image writes PalWorldSettings.ini from these at every start (restart to apply); edits to the file itself are overwritten.",
        "fields": [
          {
            "key": "difficulty",
            "type": "enum",
            "label": "Difficulty",
            "options": [
              "None",
              "Casual",
              "Normal",
              "Hard"
            ],
            "default": "None",
            "group": "World"
          },
          {
            "key": "death_penalty",
            "type": "enum",
            "label": "Death penalty",
            "help": "None, Item (drop items), ItemAndEquipment, All (also Pals).",
            "options": [
              "None",
              "Item",
              "ItemAndEquipment",
              "All"
            ],
            "default": "Item",
            "group": "World"
          },
          {
            "key": "pvp",
            "type": "bool",
            "label": "PvP",
            "default": false,
            "group": "World"
          },
          {
            "key": "player_damage",
            "type": "bool",
            "label": "Players can damage each other",
            "default": false,
            "group": "World"
          },
          {
            "key": "friendly_fire",
            "type": "bool",
            "label": "Friendly fire",
            "default": false,
            "group": "World"
          },
          {
            "key": "raids",
            "type": "bool",
            "label": "Raids on bases",
            "default": true,
            "group": "World"
          },
          {
            "key": "fast_travel",
            "type": "bool",
            "label": "Fast travel",
            "default": true,
            "group": "World"
          },
          {
            "key": "hardcore",
            "type": "bool",
            "label": "Hardcore (death is permanent)",
            "default": false,
            "group": "World"
          },
          {
            "key": "pal_lost",
            "type": "bool",
            "label": "Pals are lost on death",
            "default": false,
            "group": "World"
          },
          {
            "key": "exp_rate",
            "type": "number",
            "label": "Experience rate",
            "min": 0.1,
            "max": 20.0,
            "default": 1.0,
            "group": "Rates"
          },
          {
            "key": "capture_rate",
            "type": "number",
            "label": "Pal capture rate",
            "min": 0.5,
            "max": 2.0,
            "default": 1.0,
            "group": "Rates"
          },
          {
            "key": "spawn_rate",
            "type": "number",
            "label": "Pal appearance rate",
            "min": 0.5,
            "max": 3.0,
            "default": 1.0,
            "group": "Rates"
          },
          {
            "key": "day_speed",
            "type": "number",
            "label": "Day time speed",
            "min": 0.1,
            "max": 5.0,
            "default": 1.0,
            "group": "Rates"
          },
          {
            "key": "night_speed",
            "type": "number",
            "label": "Night time speed",
            "min": 0.1,
            "max": 5.0,
            "default": 1.0,
            "group": "Rates"
          },
          {
            "key": "pal_attack",
            "type": "number",
            "label": "Damage from Pals",
            "min": 0.1,
            "max": 5.0,
            "default": 1.0,
            "group": "Rates"
          },
          {
            "key": "pal_defense",
            "type": "number",
            "label": "Damage to Pals",
            "min": 0.1,
            "max": 5.0,
            "default": 1.0,
            "group": "Rates"
          },
          {
            "key": "player_attack",
            "type": "number",
            "label": "Damage from players",
            "min": 0.1,
            "max": 5.0,
            "default": 1.0,
            "group": "Rates"
          },
          {
            "key": "player_defense",
            "type": "number",
            "label": "Damage to players",
            "min": 0.1,
            "max": 5.0,
            "default": 1.0,
            "group": "Rates"
          },
          {
            "key": "player_hunger",
            "type": "number",
            "label": "Player hunger rate",
            "min": 0.1,
            "max": 5.0,
            "default": 1.0,
            "group": "Rates"
          },
          {
            "key": "player_stamina",
            "type": "number",
            "label": "Player stamina use",
            "min": 0.1,
            "max": 5.0,
            "default": 1.0,
            "group": "Rates"
          },
          {
            "key": "work_speed",
            "type": "number",
            "label": "Pal work speed",
            "min": 0.1,
            "max": 5.0,
            "default": 1.0,
            "group": "Rates"
          },
          {
            "key": "drop_rate",
            "type": "number",
            "label": "Enemy item drop rate",
            "min": 0.5,
            "max": 3.0,
            "default": 1.0,
            "group": "Rates"
          },
          {
            "key": "gather_rate",
            "type": "number",
            "label": "Gathered item rate",
            "min": 0.5,
            "max": 3.0,
            "default": 1.0,
            "group": "Rates"
          },
          {
            "key": "egg_hatching_hours",
            "type": "number",
            "label": "Hours to hatch a huge egg",
            "min": 0,
            "max": 240,
            "default": 72.0,
            "group": "Rates"
          },
          {
            "key": "guild_max_players",
            "type": "int",
            "label": "Max players per guild",
            "min": 1,
            "max": 100,
            "default": 20,
            "group": "Limits"
          },
          {
            "key": "base_camps_per_guild",
            "type": "int",
            "label": "Bases per guild",
            "min": 1,
            "max": 10,
            "default": 4,
            "group": "Limits"
          },
          {
            "key": "base_camp_workers",
            "type": "int",
            "label": "Pals working per base",
            "min": 1,
            "max": 50,
            "default": 15,
            "group": "Limits"
          },
          {
            "key": "autosave_seconds",
            "type": "number",
            "label": "Autosave every (seconds)",
            "min": 30,
            "max": 3600,
            "default": 30.0,
            "group": "Saving"
          }
        ],
        "managed_keys": [],
        "secret_keys": []
      }
    ],
    "secrets": [
      {
        "key": "admin_password",
        "label": "Admin password",
        "help": "Players become admins in game with /AdminPassword <password>; the panel's REST API access uses it as well. Generated when the server is created; show it here.",
        "required": false,
        "link": "",
        "generated": true,
        "reveal": true,
        "pattern": "^[A-Za-z0-9_-]{12,64}$",
        "pattern_help": "12 to 64 letters, digits, - and _"
      },
      {
        "key": "server_password",
        "label": "Join password",
        "help": "Players need it to join. Empty: anyone who knows the address joins.",
        "required": false,
        "link": "",
        "pattern": "^[A-Za-z0-9_.,!?@#%+=:-]{1,64}$",
        "pattern_help": "1 to 64 letters, digits and _ . , ! ? @ # % + = : -"
      }
    ],
    "fixed_version": {
      "id": "public",
      "label": "Steam public branch",
      "help": "Players' game clients update themselves, so the server always runs the current public build: the image checks for an update at every start. There is no version to choose; 'update' means restart.",
      "updates_on_restart": true
    },
    "min_memory_mb": 8192,
    "default_stop_timeout": 60,
    "player_name_pattern": "^[^\\x00-\\x1f\\x7f]{1,64}$",
    "player_list_labels": {
      "bans": "Bans (user ids)"
    },
    "world": {
      "kind": "folder",
      "extension": ".zip",
      "folder": "Pal/Saved/SaveGames",
      "set_aside_folder": "saves-replaced",
      "reset_fields": [],
      "import_help": "a zip of a world folder from Pal/Saved/SaveGames/0/ with Level.sav in it",
      "max_import_bytes": 17179869184
    }
  },
  {
    "id": "valheim",
    "label": "Valheim",
    "variants": [],
    "capabilities": [
      "config_files",
      "diagnostics",
      "player_actions",
      "players",
      "world_import_export",
      "world_reset"
    ],
    "memory_model": "plain",
    "default_memory_mb": 8192,
    "notes": [
      "Dedicated server on the community-valheim-tools image. The first start downloads the game (about 4 GB on disk: the image keeps two copies) and takes about a minute; every start checks the game files.",
      "Needs at least 4 GB of memory (8 GB by default).",
      "Forward the game port and the next one (game + 1) on UDP; with crossplay also game + 2.",
      "The join password needs at least 5 characters and must not contain the server name.",
      "Valheim has no remote console: admins, bans and the permitted list are files the panel edits; there is no list of the players online."
    ],
    "ports": [
      {
        "name": "game",
        "label": "Game",
        "container_port": 2456,
        "protocol": "udp",
        "default_host_port": 2456,
        "help": "Players connect to this UDP port (Valheim's default is 2456). The query port is the next number.",
        "one_to_one": true
      },
      {
        "name": "query",
        "label": "Steam query",
        "container_port": 2457,
        "protocol": "udp",
        "default_host_port": 2457,
        "help": "Always the game port + 1 (UDP); the server browser and Join IP use it.",
        "derived_from": {
          "port": "game",
          "offset": 1
        },
        "one_to_one": true
      },
      {
        "name": "crossplay",
        "label": "Crossplay",
        "container_port": 2458,
        "protocol": "udp",
        "default_host_port": 2458,
        "help": "Always the game port + 2 (UDP); published only while crossplay is on.",
        "derived_from": {
          "port": "game",
          "offset": 2
        },
        "one_to_one": true,
        "when": {
          "config": "server",
          "key": "crossplay",
          "value": true
        }
      }
    ],
    "config_files": [
      {
        "id": "server",
        "path": "(container settings)",
        "codec": "json",
        "label": "Server",
        "help": "Kept by the panel and passed to the server on its command line at its start (restart to apply).",
        "fields": [
          {
            "key": "server_name",
            "type": "string",
            "label": "Server name",
            "pattern": "^[A-Za-z0-9 _.,:!?'()#+@&-]{1,64}$",
            "pattern_help": "1 to 64 letters, digits, spaces and _ . , : ! ? ' ( ) # + @ & -",
            "default": "Valheim server",
            "initial": true
          },
          {
            "key": "public",
            "type": "bool",
            "label": "List in the community server browser",
            "help": "Off: players join by address (Join IP). The query port answers only for listed servers.",
            "default": false,
            "initial": true
          },
          {
            "key": "crossplay",
            "type": "bool",
            "label": "Crossplay (Xbox and Microsoft Store players)",
            "help": "Uses PlayFab's relay and the game port + 2 (published while this is on).",
            "default": false,
            "initial": true
          },
          {
            "key": "save_interval",
            "type": "int",
            "label": "Save every (seconds)",
            "min": 60,
            "max": 7200,
            "default": 1800,
            "group": "Saving"
          },
          {
            "key": "backups",
            "type": "int",
            "label": "World backups the game keeps",
            "help": "Valheim's own copies next to the world (not the panel's backups).",
            "min": 0,
            "max": 20,
            "default": 4,
            "group": "Saving"
          },
          {
            "key": "preset",
            "type": "enum",
            "label": "World preset",
            "help": "Empty: Valheim's normal settings. Applies to the world the server runs.",
            "options": [
              "",
              "normal",
              "casual",
              "easy",
              "hard",
              "hardcore",
              "immersive",
              "hammer"
            ],
            "default": "",
            "group": "World modifiers",
            "initial": true
          },
          {
            "key": "modifier_combat",
            "type": "enum",
            "label": "Modifier: combat",
            "options": [
              "",
              "veryeasy",
              "easy",
              "hard",
              "veryhard"
            ],
            "default": "",
            "group": "World modifiers"
          },
          {
            "key": "modifier_deathpenalty",
            "type": "enum",
            "label": "Modifier: deathpenalty",
            "options": [
              "",
              "casual",
              "veryeasy",
              "easy",
              "hard",
              "hardcore"
            ],
            "default": "",
            "group": "World modifiers"
          },
          {
            "key": "modifier_resources",
            "type": "enum",
            "label": "Modifier: resources",
            "options": [
              "",
              "muchless",
              "less",
              "more",
              "muchmore",
              "most"
            ],
            "default": "",
            "group": "World modifiers"
          },
          {
            "key": "modifier_raids",
            "type": "enum",
            "label": "Modifier: raids",
            "options": [
              "",
              "none",
              "muchless",
              "less",
              "more",
              "muchmore"
            ],
            "default": "",
            "group": "World modifiers"
          },
          {
            "key": "modifier_portals",
            "type": "enum",
            "label": "Modifier: portals",
            "options": [
              "",
              "casual",
              "hard",
              "veryhard"
            ],
            "default": "",
            "group": "World modifiers"
          },
          {
            "key": "key_nobuildcost",
            "type": "bool",
            "label": "No build cost",
            "default": false,
            "group": "World modifiers"
          },
          {
            "key": "key_playerevents",
            "type": "bool",
            "label": "Raids depend on each player",
            "default": false,
            "group": "World modifiers"
          },
          {
            "key": "key_passivemobs",
            "type": "bool",
            "label": "Passive enemies",
            "default": false,
            "group": "World modifiers"
          },
          {
            "key": "key_nomap",
            "type": "bool",
            "label": "No map",
            "default": false,
            "group": "World modifiers"
          }
        ],
        "managed_keys": [],
        "secret_keys": []
      }
    ],
    "options": [
      {
        "key": "world_name",
        "label": "World name",
        "type": "string",
        "default": "Dedicated",
        "help": "The world's folder name (config/worlds_local/<name>/): letters, digits, - and _. A world reset can start a world under a new name.",
        "pinned": true,
        "pattern": "^[A-Za-z0-9_-]{1,32}$",
        "pattern_help": "1 to 32 letters, digits, - and _"
      }
    ],
    "secrets": [
      {
        "key": "server_password",
        "label": "Join password",
        "help": "Players need it to join. At least 5 characters, and it must not contain the server name.",
        "required": true,
        "link": "",
        "pattern": "^[A-Za-z0-9_.,:!?@#%+=-]{5,64}$",
        "pattern_help": "5 to 64 letters, digits and _ . , : ! ? @ # % + = -"
      }
    ],
    "fixed_version": {
      "id": "public",
      "label": "Steam public branch",
      "help": "The image checks the game at every start, so the server runs the current public build (players' clients update themselves). There is no version to choose; 'update' means restart.",
      "updates_on_restart": true
    },
    "min_memory_mb": 4096,
    "default_stop_timeout": 120,
    "player_name_pattern": "^\\d{17}$",
    "player_list_labels": {
      "whitelist": "Permitted (whitelist)",
      "ops": "Admins",
      "bans": "Banned"
    },
    "world": {
      "kind": "folder",
      "extension": ".zip",
      "folder": "config/worlds_local",
      "set_aside_folder": "saves-replaced",
      "reset_fields": [
        {
          "key": "world_name",
          "type": "string",
          "label": "Name of the new world",
          "help": "Empty: the same name. Letters, digits, - and _ (the world folder's name).",
          "nullable": true,
          "pattern": "^[A-Za-z0-9_-]{1,32}$",
          "pattern_help": "1 to 32 letters, digits, - and _"
        }
      ],
      "import_help": "a zip of a world folder from worlds_local/ (with _main.<n>.fwl2 and .db2 files), or of an older world's <name>.fwl and <name>.db",
      "max_import_bytes": 17179869184
    }
  },
  {
    "id": "project-zomboid",
    "label": "Project Zomboid",
    "variants": [],
    "capabilities": [
      "broadcast",
      "config_files",
      "consistent_backups",
      "console_commands",
      "diagnostics",
      "player_actions",
      "players",
      "query",
      "world_import_export",
      "world_reset"
    ],
    "memory_model": "plain",
    "default_memory_mb": 8192,
    "notes": [
      "Dedicated server on the indifferentbroccoli/projectzomboid-server-docker image. The first start downloads the game (about 7 GB on disk) and takes a few minutes; every start validates the game files.",
      "Needs at least 4 GB of memory (8 GB by default); the Java heap is the container's memory minus 2 GB.",
      "Forward the game port and the RakNet port on UDP (16261 and 16262 by default).",
      "Log in as 'admin' with the admin password (Settings -> Secrets) to get admin rights in game."
    ],
    "ports": [
      {
        "name": "game",
        "label": "Game",
        "container_port": 16261,
        "protocol": "udp",
        "default_host_port": 16261,
        "help": "Players connect to this UDP port (Project Zomboid's default is 16261; Steam queries it too).",
        "one_to_one": true
      },
      {
        "name": "udp",
        "label": "RakNet (direct)",
        "container_port": 16262,
        "protocol": "udp",
        "default_host_port": 16262,
        "help": "The second UDP port players' clients use (default 16262).",
        "one_to_one": true
      }
    ],
    "config_files": [
      {
        "id": "launch",
        "path": "(container settings)",
        "codec": "json",
        "label": "Launch settings",
        "help": "Kept by the panel and passed to the server at its start (restart to apply).",
        "fields": [
          {
            "key": "max_players",
            "type": "int",
            "label": "Max players",
            "min": 1,
            "max": 100,
            "default": 16,
            "initial": true
          }
        ],
        "managed_keys": [],
        "secret_keys": []
      },
      {
        "id": "server-ini",
        "path": "data/Server/<server name>.ini",
        "codec": "kv",
        "label": "Server settings (Server/<name>.ini)",
        "help": "Read when the server starts. The ports, the RCON password and the player limit are set by the image at every start; UPnP and the game's own backups at start are switched off by the panel. Sandbox settings: Server/<name>_SandboxVars.lua in the file manager.",
        "fields": [
          {
            "key": "PublicName",
            "type": "string",
            "label": "Server name (in the server browser)",
            "group": "Listing",
            "initial": true
          },
          {
            "key": "PublicDescription",
            "type": "string",
            "label": "Description",
            "group": "Listing",
            "nullable": true
          },
          {
            "key": "Public",
            "type": "bool",
            "label": "List in the in-game server browser",
            "group": "Listing",
            "default": false,
            "initial": true
          },
          {
            "key": "Password",
            "type": "string",
            "label": "Join password",
            "help": "Players need it to join. Empty: no password.",
            "group": "Access",
            "secret": true,
            "nullable": true,
            "initial": true
          },
          {
            "key": "Open",
            "type": "bool",
            "label": "Anyone may create an account (off: whitelist only)",
            "help": "Off: only accounts an admin created join (Players -> Whitelist).",
            "group": "Access",
            "default": true,
            "initial": true
          },
          {
            "key": "PVP",
            "type": "bool",
            "label": "PvP",
            "group": "Gameplay",
            "default": true,
            "initial": true
          },
          {
            "key": "PauseEmpty",
            "type": "bool",
            "label": "Pause while nobody is online",
            "group": "Gameplay",
            "default": true
          },
          {
            "key": "GlobalChat",
            "type": "bool",
            "label": "Global chat",
            "group": "Gameplay",
            "default": true
          },
          {
            "key": "SaveWorldEveryMinutes",
            "type": "int",
            "label": "Save the world every (minutes, 0 = only on events)",
            "group": "Saving",
            "min": 0,
            "max": 1440,
            "default": 0
          }
        ],
        "managed_keys": [
          "BackupsOnStart",
          "DefaultPort",
          "MaxPlayers",
          "RCONPassword",
          "RCONPort",
          "UDPPort",
          "UPnP"
        ],
        "secret_keys": [
          "RCONPassword",
          "Password"
        ]
      }
    ],
    "options": [
      {
        "key": "server_name",
        "label": "Server name (files)",
        "type": "string",
        "default": "servertest",
        "help": "Names the server's settings, world and database files (letters, digits, - and _). Chosen once.",
        "pinned": true,
        "pattern": "^[A-Za-z0-9_-]{1,32}$",
        "pattern_help": "1 to 32 letters, digits, - and _"
      },
      {
        "key": "branch",
        "label": "Game branch",
        "type": "enum",
        "default": "public",
        "help": "Build 42 (Steam's public branch) or Build 41 (legacy41). Players need the same build. Chosen once.",
        "pinned": true,
        "options": [
          "public",
          "legacy41"
        ],
        "option_labels": {
          "public": "Build 42 (public)",
          "legacy41": "Build 41 (legacy41)"
        }
      }
    ],
    "secrets": [
      {
        "key": "admin_password",
        "label": "Admin password",
        "help": "The password of the in-game account 'admin' (log in with it to get admin rights). Generated when the server is created; show it here. It is set again at every start.",
        "required": false,
        "link": "",
        "generated": true,
        "reveal": true,
        "pattern": "^[A-Za-z0-9_-]{8,64}$",
        "pattern_help": "8 to 64 letters, digits, - and _"
      }
    ],
    "stop_words": [
      "quit"
    ],
    "command_hint": "Project Zomboid server commands without \"/\", e.g. players, save, servermsg \"Hello\", help.",
    "fixed_version": {
      "id": "public",
      "label": "Steam branch chosen at creation",
      "help": "The image checks the game at every start and runs the newest build of the server's branch (Build 42 public or Build 41 legacy41). There is no version to choose; 'update' means restart.",
      "updates_on_restart": true
    },
    "min_memory_mb": 4096,
    "default_stop_timeout": 120,
    "player_name_pattern": "^[A-Za-z0-9_.-]([A-Za-z0-9_. -]{0,30}[A-Za-z0-9_.-])?$",
    "player_list_labels": {
      "whitelist": "Whitelist (accounts)",
      "ops": "Admins",
      "bans": "Bans"
    },
    "world": {
      "kind": "folder",
      "extension": ".zip",
      "folder": "data/Saves",
      "set_aside_folder": "saves-replaced",
      "reset_fields": [
        {
          "key": "reset_accounts",
          "type": "bool",
          "label": "Also reset accounts, bans and the whitelist",
          "help": "Moves the server's database (db/<name>.db) aside as well. The admin account is made again at the next start.",
          "default": false
        }
      ],
      "import_help": "a zip of a world folder from Saves/Multiplayer/ with map_t.bin in it (same server name and game build)",
      "max_import_bytes": 17179869184
    }
  },
  {
    "id": "hkmp",
    "label": "Hollow Knight (HKMP)",
    "variants": [],
    "capabilities": [
      "broadcast",
      "config_files",
      "console_commands",
      "diagnostics",
      "player_actions",
      "players"
    ],
    "memory_model": "plain",
    "default_memory_mb": 512,
    "notes": [
      "HKMP (Hollow Knight Multiplayer) server on the maximalmax90/hkmpserver image: small (about 25 MB of memory), ready in seconds, nothing to download.",
      "Forward the UDP port (HKMP's default is 2222). Players need Hollow Knight with the HKMP mod of the same version.",
      "There is no shared world: every player keeps their own save."
    ],
    "ports": [
      {
        "name": "game",
        "label": "Game",
        "container_port": 2222,
        "protocol": "udp",
        "default_host_port": 2222,
        "help": "Players connect to this UDP port (HKMP's default is 2222).",
        "one_to_one": true
      }
    ],
    "config_files": [
      {
        "id": "launch",
        "path": "(container settings)",
        "codec": "json",
        "label": "Launch settings",
        "help": "Kept by the panel and passed to the server at its start (restart to apply).",
        "fields": [
          {
            "key": "full_sync",
            "type": "bool",
            "label": "Full synchronisation",
            "help": "HKMP 2.4.3 and later: the server keeps the world state (enemies, breakables) in sync between players.",
            "default": false,
            "initial": true
          }
        ],
        "managed_keys": [],
        "secret_keys": []
      },
      {
        "id": "serversettings",
        "path": "serversettings.json",
        "codec": "json",
        "label": "Server settings (serversettings.json)",
        "help": "Read when the server starts. The server writes this file itself (also the console's set command): edit it while the server is stopped.",
        "fields": [
          {
            "key": "IsPvpEnabled",
            "type": "bool",
            "label": "PvP",
            "default": false,
            "group": "Rules",
            "initial": true
          },
          {
            "key": "IsBodyDamageEnabled",
            "type": "bool",
            "label": "Body damage (touching other players hurts)",
            "default": true,
            "group": "Rules"
          },
          {
            "key": "TeamsEnabled",
            "type": "bool",
            "label": "Teams",
            "default": false,
            "group": "Rules",
            "initial": true
          },
          {
            "key": "AllowSkins",
            "type": "bool",
            "label": "Skins",
            "default": true,
            "group": "Rules"
          },
          {
            "key": "AllowParries",
            "type": "bool",
            "label": "Parries",
            "default": true,
            "group": "Rules"
          },
          {
            "key": "DisplayNames",
            "type": "bool",
            "label": "Show player names",
            "default": true,
            "group": "Map and names"
          },
          {
            "key": "AlwaysShowMapIcons",
            "type": "bool",
            "label": "Always show players on the map",
            "default": false,
            "group": "Map and names"
          },
          {
            "key": "OnlyBroadcastMapIconWithWaywardCompass",
            "type": "bool",
            "label": "Show a player on the map only with the Wayward Compass",
            "default": true,
            "group": "Map and names"
          },
          {
            "key": "NailDamage",
            "type": "int",
            "label": "Nail damage",
            "min": 0,
            "max": 10,
            "default": 1,
            "group": "Damage (PvP)"
          },
          {
            "key": "GreatSlashDamage",
            "type": "int",
            "label": "Great Slash damage",
            "min": 0,
            "max": 10,
            "default": 2,
            "group": "Damage (PvP)"
          },
          {
            "key": "DashSlashDamage",
            "type": "int",
            "label": "Dash Slash damage",
            "min": 0,
            "max": 10,
            "default": 2,
            "group": "Damage (PvP)"
          },
          {
            "key": "CycloneSlashDamage",
            "type": "int",
            "label": "Cyclone Slash damage",
            "min": 0,
            "max": 10,
            "default": 1,
            "group": "Damage (PvP)"
          },
          {
            "key": "VengefulSpiritDamage",
            "type": "int",
            "label": "Vengeful Spirit damage",
            "min": 0,
            "max": 10,
            "default": 1,
            "group": "Damage (PvP)"
          },
          {
            "key": "ShadeSoulDamage",
            "type": "int",
            "label": "Shade Soul damage",
            "min": 0,
            "max": 10,
            "default": 2,
            "group": "Damage (PvP)"
          },
          {
            "key": "DesolateDiveDamage",
            "type": "int",
            "label": "Desolate Dive damage",
            "min": 0,
            "max": 10,
            "default": 1,
            "group": "Damage (PvP)"
          },
          {
            "key": "DescendingDarkDamage",
            "type": "int",
            "label": "Descending Dark damage",
            "min": 0,
            "max": 10,
            "default": 2,
            "group": "Damage (PvP)"
          },
          {
            "key": "HowlingWraithDamage",
            "type": "int",
            "label": "Howling Wraiths damage",
            "min": 0,
            "max": 10,
            "default": 1,
            "group": "Damage (PvP)"
          },
          {
            "key": "AbyssShriekDamage",
            "type": "int",
            "label": "Abyss Shriek damage",
            "min": 0,
            "max": 10,
            "default": 2,
            "group": "Damage (PvP)"
          }
        ],
        "managed_keys": [],
        "secret_keys": []
      },
      {
        "id": "whitelist",
        "path": "whitelist.json",
        "codec": "json",
        "label": "Whitelist (whitelist.json)",
        "help": "Read when the server starts; while it runs, the console's whitelist on/off changes it.",
        "fields": [
          {
            "key": "enabled",
            "type": "bool",
            "label": "Only whitelisted players may join",
            "default": false
          }
        ],
        "managed_keys": [],
        "secret_keys": []
      }
    ],
    "stop_words": [
      "exit"
    ],
    "command_hint": "HKMP server commands without /, e.g. list, announce Hello, kick <name>, whitelist on.",
    "fixed_version": {
      "id": "2.4.3",
      "label": "HKMP 2.4.3",
      "help": "The HKMP server comes with the panel's runtime image; a panel release updates it. Players need the same HKMP version in their game.",
      "updates_on_restart": false
    },
    "min_memory_mb": 256,
    "default_stop_timeout": 40,
    "player_name_pattern": "^[A-Za-z0-9_.:@+/=-]{1,64}$",
    "player_list_labels": {
      "whitelist": "Whitelist",
      "ops": "Authorized (admins)",
      "bans": "Bans"
    }
  },
  {
    "id": "cs2",
    "label": "Counter-Strike 2",
    "variants": [],
    "capabilities": [
      "broadcast",
      "config_files",
      "console_commands",
      "diagnostics",
      "player_actions",
      "players",
      "query"
    ],
    "memory_model": "plain",
    "default_memory_mb": 4096,
    "notes": [
      "Dedicated server on the joedwards32/cs2 image. The first start downloads the whole game (about 60 GB on disk; check the free space first) and can take a long time; every start checks for an update.",
      "Needs at least 2 GB of memory (4 GB by default).",
      "Forward the game port on UDP (and the CSTV port while CSTV is on). RCON uses the same number on TCP: never forward the TCP port.",
      "A Game Server Login Token (GSLT, app 730, https://steamcommunity.com/dev/managegameservers) is needed for players outside your network; add it under Settings -> Secrets."
    ],
    "ports": [
      {
        "name": "game",
        "label": "Game",
        "container_port": 27015,
        "protocol": "udp",
        "default_host_port": 27015,
        "help": "Players connect to this UDP port (CS2's default is 27015). RCON listens on the same number over TCP inside the panel's network only.",
        "one_to_one": true
      },
      {
        "name": "tv",
        "label": "CSTV",
        "container_port": 27020,
        "protocol": "udp",
        "default_host_port": 27020,
        "help": "Spectators connect here (UDP); published only while CSTV is on.",
        "one_to_one": true,
        "when": {
          "config": "server",
          "key": "cstv",
          "value": true
        }
      }
    ],
    "config_files": [
      {
        "id": "server",
        "path": "(container settings)",
        "codec": "json",
        "label": "Server",
        "help": "Kept by the panel and passed to the server at its start (restart to apply). server.cfg is written by the image from these at every start.",
        "fields": [
          {
            "key": "server_name",
            "type": "string",
            "label": "Server name",
            "pattern": "^[A-Za-z0-9._-][A-Za-z0-9 ._-]{0,62}[A-Za-z0-9._-]$|^[A-Za-z0-9._-]$",
            "pattern_help": "1 to 64 letters, digits, spaces, . _ and - (no other signs: the image runs its start line through the shell)",
            "default": "Counter-Strike 2 server",
            "initial": true
          },
          {
            "key": "max_players",
            "type": "int",
            "label": "Max players",
            "min": 1,
            "max": 64,
            "default": 10,
            "initial": true
          },
          {
            "key": "game_mode",
            "type": "enum",
            "label": "Game mode",
            "options": [
              "competitive",
              "casual",
              "deathmatch",
              "wingman",
              "armsrace"
            ],
            "default": "competitive",
            "initial": true,
            "group": "Game"
          },
          {
            "key": "start_map",
            "type": "string",
            "label": "Start map",
            "pattern": "^[a-z0-9_]{1,64}$",
            "pattern_help": "a map name such as de_inferno (lower-case letters, digits and _)",
            "default": "de_inferno",
            "initial": true,
            "group": "Game"
          },
          {
            "key": "map_group",
            "type": "string",
            "label": "Map group",
            "pattern": "^[a-z0-9_]{1,64}$",
            "pattern_help": "a map group such as mg_active (lower-case letters, digits and _)",
            "default": "mg_active",
            "group": "Game"
          },
          {
            "key": "lan",
            "type": "bool",
            "label": "LAN only",
            "help": "Only players on your local network can join.",
            "default": false,
            "group": "Network"
          },
          {
            "key": "cstv",
            "type": "bool",
            "label": "CSTV (spectators)",
            "help": "Publishes the CSTV port (UDP) while on.",
            "default": false,
            "group": "Network"
          },
          {
            "key": "bot_quota",
            "type": "int",
            "label": "Bots",
            "min": 0,
            "max": 64,
            "nullable": true,
            "group": "Bots",
            "help": "Empty: the game mode's default."
          },
          {
            "key": "bot_difficulty",
            "type": "enum",
            "label": "Bot difficulty",
            "options": [
              "",
              "0",
              "1",
              "2",
              "3"
            ],
            "help": "0 easy, 1 normal, 2 hard, 3 expert; empty: the game mode's default.",
            "default": "",
            "group": "Bots"
          },
          {
            "key": "bot_quota_mode",
            "type": "enum",
            "label": "Bot fill mode",
            "options": [
              "",
              "fill",
              "competitive",
              "normal"
            ],
            "default": "",
            "group": "Bots"
          }
        ],
        "managed_keys": [],
        "secret_keys": []
      }
    ],
    "secrets": [
      {
        "key": "gslt",
        "label": "Game Server Login Token (GSLT)",
        "help": "Needed for the server to be listed and reachable for players outside your network: create one for app 730 (one token per server). Without it the server works on your LAN.",
        "required": false,
        "link": "https://steamcommunity.com/dev/managegameservers",
        "pattern": "^[A-F0-9]{32}$",
        "pattern_help": "32 characters, digits and A-F (as Steam shows it)"
      },
      {
        "key": "server_password",
        "label": "Join password",
        "help": "Players need it to join (connect <address>; password <password>). Empty: no password.",
        "required": false,
        "link": "",
        "pattern": "^[A-Za-z0-9]{1,64}$",
        "pattern_help": "1 to 64 letters and digits"
      },
      {
        "key": "tv_password",
        "label": "CSTV password",
        "help": "Spectators need it while CSTV is on. Empty: anyone may watch.",
        "required": false,
        "link": "",
        "pattern": "^[A-Za-z0-9]{1,64}$",
        "pattern_help": "1 to 64 letters and digits"
      }
    ],
    "stop_words": [
      "_restart",
      "exit",
      "quit"
    ],
    "command_hint": "Server console commands, e.g. status, say Hello, changelevel de_dust2, mp_restartgame 1.",
    "fixed_version": {
      "id": "public",
      "label": "Steam public branch",
      "help": "The image runs SteamCMD at every start, so the server always runs the current build (players' clients update themselves). There is no version to choose; 'update' means restart.",
      "updates_on_restart": true
    },
    "min_memory_mb": 2048,
    "default_stop_timeout": 30,
    "player_name_pattern": "^[^\";\\\\\\x00-\\x1f\\x7f]{1,64}$",
    "player_list_labels": {}
  },
  {
    "id": "rust",
    "label": "Rust",
    "variants": [],
    "capabilities": [
      "broadcast",
      "config_files",
      "consistent_backups",
      "console_commands",
      "diagnostics",
      "player_actions",
      "players",
      "query",
      "world_import_export",
      "world_reset"
    ],
    "memory_model": "plain",
    "default_memory_mb": 16384,
    "notes": [
      "Dedicated server on the didstopia/rust-server image (pinned by digest). The first start downloads the game (about 10 GB on disk) and generates the map, which takes several minutes; every start checks for an update.",
      "Needs at least 10 GB of memory (16 GB by default; big maps need more).",
      "Forward the game port and the query port on UDP, and the Rust+ app port on TCP while Rust+ is on. WebRCON stays inside the panel's network."
    ],
    "ports": [
      {
        "name": "game",
        "label": "Game",
        "container_port": 28015,
        "protocol": "udp",
        "default_host_port": 28015,
        "help": "Players connect to this UDP port (Rust's default is 28015).",
        "one_to_one": true
      },
      {
        "name": "query",
        "label": "Steam query",
        "container_port": 28017,
        "protocol": "udp",
        "default_host_port": 28017,
        "help": "The server browser asks this UDP port (default 28017); it must differ from the game port.",
        "one_to_one": true
      },
      {
        "name": "app",
        "label": "Rust+ app",
        "container_port": 28082,
        "protocol": "tcp",
        "default_host_port": 28082,
        "help": "The Rust+ companion app connects here (TCP); published only while Rust+ is on.",
        "one_to_one": true,
        "when": {
          "config": "server",
          "key": "rust_plus",
          "value": true
        }
      }
    ],
    "config_files": [
      {
        "id": "server",
        "path": "(container settings)",
        "codec": "json",
        "label": "Server",
        "help": "Kept by the panel and passed to the server at its start (restart to apply).",
        "fields": [
          {
            "key": "server_name",
            "type": "string",
            "label": "Server name",
            "pattern": "^(?![+-])[^\"\\\\;\\x00-\\x1f\\x7f]{1,64}$",
            "pattern_help": "1 to 64 characters without quotes, backslashes or ;, not starting with + or -",
            "default": "Rust server",
            "initial": true
          },
          {
            "key": "description",
            "type": "string",
            "label": "Description",
            "pattern": "^(?:(?![+-])[^\"\\\\;\\x00-\\x1f\\x7f]{1,512})?$",
            "pattern_help": "at most 512 characters without quotes, backslashes or ;, not starting with + or -",
            "default": ""
          },
          {
            "key": "max_players",
            "type": "int",
            "label": "Max players",
            "min": 1,
            "max": 500,
            "default": 50,
            "initial": true
          },
          {
            "key": "world_size",
            "type": "int",
            "label": "Map size",
            "help": "1000 to 6000 (3500 is Rust's default). A new size makes a new map: use a world reset.",
            "min": 1000,
            "max": 6000,
            "default": 3500,
            "initial": true,
            "group": "Map"
          },
          {
            "key": "seed",
            "type": "int",
            "label": "Map seed",
            "help": "Chosen at random when the server is created; a world reset can pick a new one.",
            "min": 0,
            "max": 2147483647,
            "default": null,
            "nullable": true,
            "initial": true,
            "group": "Map"
          },
          {
            "key": "save_interval",
            "type": "int",
            "label": "Save every (seconds)",
            "min": 60,
            "max": 3600,
            "default": 600,
            "group": "Saving"
          },
          {
            "key": "rust_plus",
            "type": "bool",
            "label": "Rust+ companion app",
            "help": "Publishes the app port (TCP) while on.",
            "default": false,
            "group": "Network"
          }
        ],
        "managed_keys": [],
        "secret_keys": []
      }
    ],
    "options": [
      {
        "key": "identity",
        "label": "Server identity",
        "type": "string",
        "default": "rust",
        "help": "The folder of the server's map and saves (server/<identity>/): lower-case letters, digits, - and _. Chosen once.",
        "pinned": true,
        "pattern": "^[a-z0-9_-]{1,32}$",
        "pattern_help": "1 to 32 lower-case letters, digits, - and _"
      }
    ],
    "stop_words": [
      "global.quit",
      "global.restart",
      "quit",
      "restart"
    ],
    "command_hint": "Rust console commands, e.g. status, say Hello, server.save, playerlist.",
    "fixed_version": {
      "id": "public",
      "label": "Steam public branch",
      "help": "The image runs SteamCMD at every start, so the server always runs the current build (players' clients update themselves). There is no version to choose; 'update' means restart.",
      "updates_on_restart": true
    },
    "min_memory_mb": 10240,
    "default_stop_timeout": 180,
    "player_name_pattern": "^[^\"\\\\;\\x00-\\x1f\\x7f]{1,64}$",
    "player_list_labels": {
      "ops": "Owners (admins)",
      "bans": "Bans"
    },
    "world": {
      "kind": "folder",
      "extension": ".zip",
      "folder": "server",
      "set_aside_folder": "saves-replaced",
      "reset_fields": [
        {
          "key": "seed",
          "type": "int",
          "label": "Seed of the new map",
          "help": "Empty: a new random map.",
          "min": 0,
          "max": 2147483647,
          "nullable": true
        },
        {
          "key": "wipe_blueprints",
          "type": "bool",
          "label": "Also wipe blueprints",
          "help": "Moves the players' learned blueprints (player.blueprints.*.db) aside as well.",
          "default": false
        }
      ],
      "import_help": "a zip of a server identity folder (server/<identity>/) with the map (.map) and saves (.sav)",
      "max_import_bytes": 17179869184
    }
  }
] as unknown as GameInfo[];

export type SixGameId = "palworld" | "valheim" | "project-zomboid" | "hkmp" | "cs2" | "rust";

/** A fresh copy of one game's catalog entry. */
export function sixGame(id: SixGameId): GameInfo {
  const entry = ENTRIES.find((g) => g.id === id);
  if (!entry) throw new Error(`no catalog entry for ${id}`);
  return structuredClone(entry);
}

export const SIX_GAME_IDS: SixGameId[] = ["palworld", "valheim", "project-zomboid", "hkmp", "cs2", "rust"];
