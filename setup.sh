#!/bin/bash
# Set up or update pve-unifi-dns on Proxmox nodes, start to finish.
#
#   ./setup.sh <ssh-host> [<ssh-host> ...]          install / update
#   ./setup.sh --check <ssh-host> [<ssh-host> ...]  report only, change nothing
#
# Settings come from setup.conf next to this script (copy setup.conf.example).
# Each host must accept `ssh <host>` as root with a key. Safe to run again at any time.

set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
CHECK=no
if [ "${1:-}" = "--check" ]; then CHECK=yes; shift; fi
[ $# -ge 1 ] || { sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

CONF_FILE=${SETUP_CONF:-$HERE/setup.conf}
[ -f "$CONF_FILE" ] || { echo "Missing $CONF_FILE: copy setup.conf.example to setup.conf and fill it in."; exit 1; }
# shellcheck disable=SC1090
. "$CONF_FILE"

expand() { case "$1" in "~"/*) echo "$HOME/${1#\~/}" ;; *) echo "$1" ;; esac; }
KEY_FILE=$(expand "${UNIFI_API_KEY_FILE:-}")
ACME_DATA_FILE=$(expand "${ACME_DNS_DATA_FILE:-}")
: "${UNIFI_HOST:?set UNIFI_HOST in setup.conf}" "${DOMAIN:?set DOMAIN in setup.conf}"
if [ "${ACME:-no}" = yes ]; then
    : "${ACME_EMAIL:?set ACME_EMAIL}" "${ACME_PLUGIN_ID:?set ACME_PLUGIN_ID}" "${ACME_DNS_API:?set ACME_DNS_API}"
fi

FILES="pve-unifi-dns.py pve-unifi-dns.conf pve-unifi-dns.service pve-unifi-dns.timer
       pve-unifi-dns-ui.py pve-unifi-dns-ui.service pve-unifi-dns-ui-patch 99pve-unifi-dns-ui
       node-install.sh unifi-dns-hook.sh VERSION"
REMOTE=/root/pve-unifi-dns
SSH_TTY_OPT=-T
[ -t 0 ] && SSH_TTY_OPT=-t      # lets you answer the ACME terms question

rc=0
for host in "$@"; do
    printf '\n################ %s ################\n' "$host"
    if ! ssh -o BatchMode=yes -o ConnectTimeout=8 "$host" true 2>/dev/null; then
        echo "Cannot log in to $host with an SSH key. Fix with: ssh-copy-id root@<address>"
        rc=1; continue
    fi
    ssh -o BatchMode=yes "$host" "mkdir -p $REMOTE/ui && chmod 700 $REMOTE"
    (cd "$HERE" && scp -q $FILES "$host:$REMOTE/" && scp -q ui/unifi-dns-tab.js "$host:$REMOTE/ui/")
    if [ "$CHECK" = no ]; then
        if [ -n "$KEY_FILE" ] && [ -s "$KEY_FILE" ]; then
            scp -q "$KEY_FILE" "$host:$REMOTE/.apikey"
        fi
        if [ "${ACME:-no}" = yes ] && [ -n "$ACME_DATA_FILE" ] && [ -s "$ACME_DATA_FILE" ]; then
            scp -q "$ACME_DATA_FILE" "$host:$REMOTE/.acme-data"
        fi
    fi
    envs=""
    for v in UNIFI_HOST DOMAIN DNS_SERVER SET_NODE_DNS INSTALL_LLDP INSTALL_UI ACME \
             ACME_EMAIL ACME_DIRECTORY ACME_PLUGIN_ID ACME_DNS_API; do
        [ -n "${!v:-}" ] && envs="$envs $v=$(printf '%q' "${!v}")"
    done
    [ "$CHECK" = yes ] && envs="$envs CHECK_ONLY=yes"
    if ! ssh $SSH_TTY_OPT -o BatchMode=yes "$host" "cd $REMOTE && env$envs bash ./node-install.sh"; then
        echo "Setup on $host stopped with an error (see above)."
        rc=1
    fi
done
exit $rc
