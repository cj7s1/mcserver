# MCPanel plugin for Unraid: changes

Newest first. The first heading is the plugin's version: build.sh writes it into mcpanel.plg, and Unraid's
Plugins page offers an update when the version of the pluginURL's file is greater (compared as text). Any
change of src/ gets a new entry: the date, with a letter for a second one on the same day (2026.11.12a).

## 2026.11.12b

- The firewall's status "not installed" is a normal answer: the task firewall-status ends ok with "(not
  installed: a normal state, nothing failed)" (instance-firewall.sh status exits 3 for it); a real failure stays
  one. Seen on one Unraid 7.3.4 server on 2026-11-12, where it read "Finished: failed (exit 1)".
- The texts say what ran on that server (the install, facts, save, the status, check-updates, boot dry run, boot
  run and firewall tasks) and what was not seen there yet (this page, the boot tasks at a real Docker start,
  Update and Roll back, Install Compose).

## 2026.11.12a

- The run folder (lock, task state, plans, logs) is /run/mcpanel instead of /tmp/mcpanel: under a folder only
  root can write, checked on every use (also by the page's facts and its PHP), new files made with noclobber;
  at most 10 logs and 32 MB (/run is small).
- Install Compose and the boot tasks check the SHA-256 of the copy that is moved into place (in RAM, and on the
  flash drive before it replaces the old one), not only of the file it was copied from.
- The boot tasks run the checkout's scripts only when nothing of them is writable by others or owned by anyone
  but root; otherwise "NEEDS YOU" with the repair on the page and in the syslog. A checkout without .git (the
  ZIP download) works for the boot tasks; updates and rollbacks through the page need a git clone.

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
