# Unraid: stopping the servers when Docker stops

This page is about array stop, reboot and shutdown on Unraid: what happens to running Minecraft
servers, how to give them time to save, and how they come back afterwards.

## What Unraid does when it stops Docker

1. It stops the containers it manages itself, the ones from the Docker tab (they carry a
   `net.unraid.docker.managed` label). The Minecraft containers are created by MCPanel and do not
   carry that label, so Unraid does not stop them.
2. It tells the Docker daemon to end and kills it 30 seconds later.
3. Docker stops whatever still runs on its way down: all Minecraft servers at the same moment,
   together with everything else on the host.

A server stopped this way gets only a few seconds. If it is not done saving, it is killed.

## The four ways a server is stopped

| Who stops it | Time the server gets | World saved first |
|---|---|---|
| **Stop** or **Restart** in the panel | The server's stop timeout (server settings, default 90 s) | Yes |
| **Stop** in Unraid's Docker tab | Unraid's Docker stop timeout (Settings → Docker, default 10 s) | No |
| Docker itself: array stop, reboot or shutdown without the hook, restart of the Docker service | Up to 20 s (10 s before this release) | No |
| The hook on this page: array stop, reboot or shutdown | Up to 45 s, never more than the server's own stop timeout | Yes |

The 20 seconds apply to containers created by this release. After the upgrade every existing
server shows "restart required", although none of its settings changed: its container was made
by the previous version. Its next start or restart recreates the container (world data is not
touched). A server on a node whose agent is still the old version keeps showing it until that
agent is updated too.

## The hook

`deploy/unraid/stopping_docker` is a script that Unraid runs right before it stops Docker. It
asks the panel to stop all running servers the same way the Stop button does, all at once, and
waits for them. The panel marks each server it stops so that it can start it again after boot.

The hook finishes within 60 seconds, is ended by a watchdog after 70, and always reports
success, so it cannot block a shutdown.

### When the panel cannot do it

A plain `docker stop` gives a server time to save, but Docker then counts the server as stopped
by hand and does not start it again at boot, and only the panel could. So the hook uses it in
one case only:

| Situation | What the hook does | The servers after the next boot |
|---|---|---|
| The panel stops the servers (the normal case) | Nothing more | Started by the panel, one per minute |
| No panel container is running | `docker stop` for every running Minecraft container, up to 45 s each | **Stay stopped** until you start them in the panel: nobody marked them |
| The panel runs but its command fails (it cannot reach its agent or its database, or it takes longer than 60 s) | Nothing more: Docker stops the servers that still run, with its own timeout (up to 20 s, no save first) | Started by Docker, all at once; servers the panel had already stopped and marked are started by the panel |
| The panel runs but does not know the command (an MCPanel version from before this hook) | The same (containers made by that version get 10 s) | Started by Docker, all at once |
| Docker does not answer, or the hook is ended by its watchdog | Nothing | Started by Docker; by the panel if it had stopped and marked them already |

The syslog says which case it was (`grep mcpanel /var/log/syslog`).

**Before you roll MCPanel back to a version without this feature**, take the hook out:
`install-hook.sh remove`, and delete the User Scripts entry or the line in `/boot/config/go`
that installs it at boot. A hook left in place does no harm with the old version (the last
row but one of the table), but it does nothing useful either and logs an error at every stop.

### Install

In the Unraid terminal (adjust the path if your copy of MCPanel is somewhere else):

```bash
bash /mnt/user/appdata/mcpanel-src/deploy/unraid/install-hook.sh install
bash /mnt/user/appdata/mcpanel-src/deploy/unraid/install-hook.sh status
```

This copies the hook to `/usr/local/emhttp/plugins/mcpanel/event/stopping_docker`. That folder
is in RAM and is rebuilt at every boot, so the install has to run again at every boot. Pick one:

**User Scripts plugin.** Add a script with these two lines and set its schedule to
**At Startup of Array**:

```bash
#!/bin/bash
bash /mnt/user/appdata/mcpanel-src/deploy/unraid/install-hook.sh install
```

Do not use "At Stopping of Array" for the hook itself: that runs after Docker has stopped,
which is too late.

**/boot/config/go.** This file runs before the array is mounted, so keep a copy of the two
files on the flash drive:

```bash
mkdir -p /boot/config/mcpanel
cp /mnt/user/appdata/mcpanel-src/deploy/unraid/stopping_docker \
   /mnt/user/appdata/mcpanel-src/deploy/unraid/install-hook.sh /boot/config/mcpanel/
```

and add this line to `/boot/config/go`:

```bash
bash /boot/config/mcpanel/install-hook.sh install
```

Copy the two files again after an MCPanel upgrade.

**Another Compose project name.** The hook finds the panel container by its Compose project,
`mcpanel` by default (`docker compose ls` shows the name of yours). If your stack has another
name, add `--project <name>` to the install command, in the terminal and in the User Scripts
entry or the `go` line alike:

```bash
bash /mnt/user/appdata/mcpanel-src/deploy/unraid/install-hook.sh install --project minecraft
```

Unraid passes no environment variables to the hook, so the name is written into the installed
copy; `install-hook.sh status` shows which project the installed hook looks for. With the wrong
name the hook finds no panel and falls back to `docker stop` (see the table above).

To take the hook out: `install-hook.sh remove`, and delete the User Scripts entry or the line
in `go`.

### Raise the shutdown time-out

Settings → Disk Settings → **Shutdown time-out** is how long Unraid waits for a clean shutdown
before it forces one (an unclean shutdown, followed by a parity check). The hook can take 60 to
70 seconds before Unraid starts to stop Docker and the array, so add that time: with Unraid's
default of 90 seconds, set it to **at least 150**.

`install-hook.sh install` and `install-hook.sh status` read the current value (from
`/boot/config/disk.cfg`) and print a warning while it is below 150.

## How the servers come back

- Servers the hook stopped through the panel are started again by the panel after boot, **one
  per minute**, in order of their names. Until its turn a server shows "Stopped because the host
  was shutting down; it is started again automatically".
- Servers that were already stopped stay stopped.
- If you start, stop, kill, archive or delete a server yourself in the meantime, the panel
  leaves it as you set it.
- The panel does not start a server by itself when something went wrong with it after it was
  stopped: an operation on it that changes its files or its container (a restore, a world
  reset, an update, a move to another node) failed, was marked as failed or was cut off, or the
  panel stopped it because it kept crashing. Its files may be incomplete, so check it and start
  it yourself. The audit log has `instance.not_resumed_after_boot` with the reason. A failed
  backup or snapshot does not count.
- Without the hook nothing changes: Docker starts every server that was running, all at once.

## Check that it works

1. `install-hook.sh status` prints "Installed and up to date".
2. With a server running, stop the array (not a reboot: the system log is in RAM and is lost at
   reboot unless you mirror it). Then look at Tools → System Log, or:

   ```bash
   grep mcpanel /var/log/syslog
   ```

   You should see `asking the panel to stop the running servers`, one `stopped <name> in N s`
   line per server, a summary line and `finished after N s`.
3. Start the array again. In the panel, the audit log has `instance.stopped_for_host_shutdown`
   for each server and, a minute or so after the panel is up, `instance.resumed_after_boot`.
   The Activity page shows a "Start server (after the host restart)" job per server.
4. The server's console log from before the stop ends with `Saving worlds` and
   `All dimensions are saved`.

To try the hook without stopping the array, run
`/usr/local/emhttp/plugins/mcpanel/event/stopping_docker` by hand. It stops all running servers
and marks them; they come back when the panel's containers start the next time, or right away
when you press Start. If the panel's containers are not restarted within ten minutes, the
panel drops the marks (Docker was evidently not stopped): the servers then stay stopped until
you start them, and their status no longer says that they are started automatically.
