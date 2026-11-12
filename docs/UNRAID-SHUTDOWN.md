# Unraid: stopping the servers when Docker stops

This page is about array stop, reboot and shutdown on Unraid: what happens to running Minecraft
servers, how to give them time to save, and how they come back afterwards.

> **Status:** tested on a Linux desktop with real Docker and real Minecraft servers: the panel
> command behind the hook, the hook script run by hand (with a stand-in for `logger`), and the
> restart of the servers after the platform containers were stopped and started again. **Not
> tested:** anything on Unraid itself. That means Unraid really calling the hook when it stops
> Docker, the 30 seconds it gives the Docker daemon, `install-hook.sh` as root on Unraid, a real
> reboot, and large or modded servers against the 20 and 45 second limits. Do the check at the end
> of this page once after installing.

## What Unraid does when it stops Docker

1. It stops the containers it manages itself, the ones made from its own templates (they carry a
   `net.unraid.docker.managed` label). The Minecraft containers and the three MCPanel containers
   are not made that way and do not carry that label, so Unraid does not stop them.
2. It tells the Docker daemon to end and kills it 30 seconds later.
3. Docker stops whatever still runs on its way down: all Minecraft servers at the same moment,
   together with everything else on the host.

A server stopped this way gets at most 20 seconds, and nobody tells it to save first. If it is
not done by then, it is killed.

The panel container is stopped in the same way. A job that is running at that moment (a backup,
an install) is cut off: a job that is waiting for the agent, which is most of its time, is marked
as failed ("interrupted") at once, others after the next start. A server such a job had stopped
for its work stays stopped. Stop the array when the Activity page is empty.

The three MCPanel containers have the restart policy `always`: Docker starts them at its next
start, also when they were stopped on their own before (Docker tab, a backup plugin).

## The four ways a server is stopped

| Who stops it | Time the server gets | World saved first |
|---|---|---|
| **Stop** or **Restart** in the panel | The server's stop timeout (server settings, default 90 s) | Yes |
| **Stop** in Unraid's Docker tab | Unraid's Docker stop timeout (Settings → Docker, default 10 s) | No |
| Docker itself: array stop, reboot or shutdown without the hook, restart of the Docker service | Up to 20 s, never more than the server's own stop timeout (10 s for a container made by a version without this limit) | No |
| The hook on this page: array stop, reboot or shutdown | Up to 45 s, never more than the server's own stop timeout | Yes |

"World saved first" means that the agent sends `save-all flush` and waits for the answer before
the stop. Without it the server still saves as part of its own shutdown, if it gets that far in
the time it has.

The 20 seconds are `AGENT_SHUTDOWN_STOP_TIMEOUT` in `.env`. Keep 20 on Unraid: Unraid kills the
Docker daemon 30 seconds after asking it to stop, and a server that is given much more than 20
seconds could still be stopping at that moment.

The limit is part of a container from the moment it is created. A server whose container was
made by an older version shows "restart required" after the upgrade, although none of its
settings changed. Its next start or restart recreates the container (world data is not touched).
A stopped server has no container and gets the limit at its next start. A server on a node whose
agent is still the old version keeps showing "restart required" until that agent is updated too.

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

"Started by Docker" applies to servers with the default restart policy (`unless-stopped`).
Docker does not start a server whose restart policy is `no`.

If the MCPanel stack is managed by a Compose plugin that stops its stacks when Docker stops
(Compose Manager Plus has such a step), the panel may already be stopped when this hook runs.
That is the second row of the table: the servers are stopped with `docker stop` and stay stopped
after the next boot. This combination was not tested. A stack started from the command line, as
this documentation describes, is not affected.

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
is in RAM and is rebuilt at every boot, so the install has to run again at every boot.

Root runs `install-hook.sh` and `stopping_docker`, so nobody else may be able to change them. When
group or others can write one of them or their folder, `install` and `remove`, run as root from the
MCPanel checkout, run `chmod -R go-w` on the checkout themselves and go on, with a warning on the
screen and in the syslog (tag `mcpanel`: "permissions of ... were loose and have been tightened";
if you did not expect it, check `git status` there): a refusal at an unattended array start would
leave the server without its hook. Not as root, for a copy elsewhere (`/boot/config/mcpanel`), or
when the `chmod` fails, they refuse and name the repair: `chmod -R go-w
/mnt/user/appdata/mcpanel-src` for the checkout, a `chmod go-w` of the files found for a copy.
`status` only warns. See docs/SECURITY.md, "The source folder".

Pick one way to run the install at every boot:

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
- The panel only starts a server that is still meant to run. If something recorded it as
  stopped in the meantime, the panel leaves it stopped.
- The panel does not start a server by itself when something went wrong with it after it was
  stopped: an operation that changes its files (a restore, a world replace or reset, a modpack
  or software update, a move to another node) failed, was marked as failed or was cut off, or
  the panel stopped it because it kept crashing. Its files may be incomplete, so check it and
  start it yourself. A failed backup, snapshot, clone, start or restart does not count.
- When the panel drops a mark like this without starting the server, the audit log has
  `instance.not_resumed_after_boot` with the reason: `not_meant_to_run`, `job_failed`,
  `insufficient_memory` or `host_not_restarted` (the ten-minute rule at the end of this page). A server stopped for
  crashing has `instance.crash_loop_stopped` instead.
- While the agent does not answer, or a job holds the server, the panel waits and tries again a
  minute later.
- A server that does not fit into the host's memory next to the servers already running (the
  memory check, DEPLOYMENT.md "Memory") waits too. When it still does not fit 10 minutes after the
  panel started, the panel drops its mark (`instance.not_resumed_after_boot`, reason
  `insufficient_memory`, with the numbers) and it stays stopped.
- Schedules whose time passed while the host was off: restarts, starts, stops and commands are
  not run late; a missed backup or snapshot runs once its server has been ready for 2 minutes (or
  the panel has been up for 5).
- Without the hook nothing changes: Docker starts every server that was running, all at once.

## Check that it works

1. `install-hook.sh status` prints "Installed and up to date".
2. With a server running, stop the array (not a reboot: the system log is in RAM and is lost at
   reboot unless you mirror it). Then look at Tools → System Log, or:

   ```bash
   grep mcpanel /var/log/syslog
   ```

   You should see `asking the panel to stop the running servers`, one `stopped <name> in N s`
   line per server, a summary line that starts with `host_shutdown:` and `finished after Ns`.
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
