#!/bin/bash
# MCPanel plugin, last step of every install: the plugin manager makes folders with mode 0770 and files with
# the umask of whoever runs it (000 on some servers): nothing of the plugin may be writable by group or others.
# Nothing is started here: at boot Docker is not up yet (the boot tasks run when it starts: event/docker_started),
# and after an install or update by hand the page offers them.
umask 022
D="${MCPANEL_PLUGIN_ROOT:-}/usr/local/emhttp/plugins/mcpanel"
chmod -R go-w "${D}" || exit 1
chmod 755 "${D}" "${D}/bin" "${D}/event" "${D}/include" || exit 1
chmod 755 "${D}/bin/mcpanel" "${D}/event/docker_started" || exit 1
mkdir -p "${MCPANEL_PLUGIN_ROOT:-}/boot/config/plugins/mcpanel" || exit 1
echo ""
echo "MCPanel plugin installed. Its page: Settings > User Utilities > MCPanel."
echo "At every Docker start it runs the boot tasks of the MCPanel checkout (Compose link, shutdown hook,"
echo "firewall where it was installed, stack check); no User Script is needed for them any more."
echo "Seen on one Unraid 7.3.4 server, not yet in every part: docs/UNRAID.md has the first-run checklist"
echo "(or: bash <your mcpanel-src>/deploy/unraid/first-run.sh)."
exit 0
