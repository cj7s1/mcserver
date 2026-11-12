#!/bin/bash
# MCPanel plugin, first step of every install (also at every boot and at an update): the files of the
# version before go, because the plugin manager never overwrites a file that exists. The shutdown hook that
# install-hook.sh wrote into the plugin's event folder stays: an update must not leave the server without it.
umask 022
D="${MCPANEL_PLUGIN_ROOT:-}/usr/local/emhttp/plugins/mcpanel"
KEEP=""
if [ -f "${D}/event/stopping_docker" ] && [ ! -L "${D}/event/stopping_docker" ]; then
    KEEP="$(mktemp "${MCPANEL_PLUGIN_ROOT:-}/run/mcpanel-hook.XXXXXX")" && cp -p "${D}/event/stopping_docker" "${KEEP}" || KEEP=""
fi
rm -rf "${D}"
if [ -n "${KEEP}" ]; then
    mkdir -p "${D}/event" && mv -f "${KEEP}" "${D}/event/stopping_docker" && chmod 755 "${D}/event/stopping_docker"
fi
exit 0
