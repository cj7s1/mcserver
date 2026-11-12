# MCPanel plugin for Unraid: changes

Newest first. The first heading is the plugin's version: build.sh writes it into mcpanel.plg, and Unraid's
Plugins page offers an update when the version of the pluginURL's file is greater (compared as text). Any
change of src/ gets a new entry: the date, with a letter for a second one on the same day (2026.11.12a).

## 2026.11.12

- First version, not run on a real Unraid server yet (docs/UNRAID.md, "The Unraid plugin": the first-run
  checklist).
- Settings > User Utilities > MCPanel: status of the stack and the plugin, Check for updates (git fetch),
  Update (git pull --ff-only, the dry run of upgrade.sh, then upgrade.sh run after a confirmation), Roll
  back (dry run first), boot tasks, the game servers' firewall, Docker Compose, settings. Every button that
  changes something shows the exact commands first; one action at a time; logs in /tmp/mcpanel/logs.
- At every Docker start (event docker_started): the boot tasks of the checkout (array-start.sh run: Compose
  link, shutdown hook, firewall where it was installed, stack check). No User Script needed.
- Install Compose: Docker Compose v5.7.0 (the official release, SHA-256 pinned), kept on the flash drive.
