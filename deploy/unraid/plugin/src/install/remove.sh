#!/bin/bash
# MCPanel plugin removal: its page, its programs and its event scripts go, the shutdown hook in its event folder
# with them, and its run folder /run/mcpanel (the action logs). Kept: /boot/config/plugins/mcpanel (the settings,
# array-start.env, the Compose copy if it was installed), and until the next boot the Compose link and its copy
# in RAM (docker compose keeps working).
umask 022
R="${MCPANEL_PLUGIN_ROOT:-}"
D="${R}/usr/local/emhttp/plugins/mcpanel"
LOCK="${R}/run/mcpanel/lock"
if [ -e "${LOCK}" ] && command -v flock >/dev/null 2>&1 && ! flock -n "${LOCK}" true; then
    echo "An action of the MCPanel page is still running: remove the plugin when it has ended (the page shows it)."
    exit 1
fi
rm -rf "${D}" "${R}/run/mcpanel"
logger -t mcpanel -- "plugin removed: the shutdown hook and the boot tasks at Docker start are gone" 2>/dev/null || true
echo "MCPanel plugin removed, with the shutdown hook, the boot tasks at Docker start and the action logs."
echo "Kept: /boot/config/plugins/mcpanel (settings, array-start.env, the Compose copy if it was installed: delete"
echo "that folder to remove them). Without the plugin, the User Script of docs/UNRAID.md does the boot tasks."
exit 0
