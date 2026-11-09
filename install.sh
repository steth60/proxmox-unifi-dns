#!/usr/bin/env bash
# UniFi DNS for Proxmox VE - installer, updater and uninstaller.
#
# Run on the Proxmox VE host as root:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/steth60/proxmox-unifi-dns/main/install.sh)"
#
# Options:
#   --unattended            no dialogs; settings from the environment (see below)
#   --verbose               show every command on screen as it runs
#   --uninstall             remove it (asks whether to delete its DNS records too)
#   --uninstall --purge     remove it and delete its DNS records and Notes lines, no questions
#   --version               print the version and exit
#
# Environment for --unattended (all optional on updates; existing settings are kept):
#   UNIFI_HOST  UniFi console address      DOMAIN        local DNS domain
#   UNIFI_API_KEY  API key (or enter it later in Proxmox > Datacenter > UniFi DNS > Setup)
#   SET_NODE_DNS=yes|no   INSTALL_LLDP=yes|no
#   PUD_REPO=owner/repo   PUD_REF=branch-or-tag   (where to download from)

set -Eeuo pipefail
trap 'rc=$?; printf " \033[01;31mERROR\033[m %s line %s: \"%s\" exited with %s\n" install.sh "$LINENO" "$BASH_COMMAND" "$rc" >&2' ERR

APP="UniFi DNS for Proxmox"
REPO="${PUD_REPO:-steth60/proxmox-unifi-dns}"
REF="${PUD_REF:-main}"
CONF=/etc/pve/priv/pve-unifi-dns.conf
SELF_DIR=/nonexistent        # set when run from a downloaded copy instead of curl | bash
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

RD=$'\033[01;31m'; GN=$'\033[1;92m'; YW=$'\033[33m'; BL=$'\033[36m'; CL=$'\033[m'; BLD=$'\033[1m'
LOG=/var/log/pve-unifi-dns-install.log
LOGFD=""
log()       { [ -z "$LOGFD" ] || printf '%s %s\n' "$(date +%T)" "$*" >&"$LOGFD"; }
msg_info()  { log "INFO  $1"; printf ' %s...\r' "${YW}$1${CL}"; }
msg_ok()    { log "OK    $1"; printf ' \033[K%s✔%s %s\n' "$GN" "$CL" "$1"; }
msg_warn()  { log "WARN  $1"; printf ' \033[K%s!%s %s\n' "$YW" "$CL" "$1"; }
msg_error() { log "ERROR $1"; printf ' \033[K%s✖%s %s\n' "$RD" "$CL" "$1" >&2; }
die()       { msg_error "$1"; exit 1; }

header_info() {
    [ "$UNATTENDED" = yes ] || clear
    cat <<HDR
${BL}${BLD}
   _   _       _ ___ _   ___  _  _ ___
  | | | |_ _  (_) __(_) |   \\| \\| / __|
  | |_| | ' \\ | | _|| | | |) | .\` \\__ \\
   \\___/|_||_||_|_| |_| |___/|_|\\_|___/   for Proxmox VE
${CL}
  Local DNS records in UniFi for every Proxmox guest, managed from the Proxmox UI.

HDR
}

UNATTENDED=no; ACTION=""; PURGE=no; VERBOSE=${VERBOSE:-no}
for a in "$@"; do
    case "$a" in
        --unattended) UNATTENDED=yes ;;
        --verbose) VERBOSE=yes ;;
        --uninstall) ACTION=uninstall ;;
        --purge) PURGE=yes ;;
        --version) cat "$SELF_DIR/VERSION" 2>/dev/null || curl -fsSL "https://raw.githubusercontent.com/$REPO/$REF/VERSION"; exit 0 ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option $a" ;;
    esac
done

# ---------------------------------------------------------------- checks --
[ "$(id -u)" -eq 0 ] || die "run this as root on the Proxmox VE host"

# Everything goes to $LOG: a trace of every command, the full output of the node
# installer, and a snapshot of the node before and after. The API key is never traced.
exec {LOGFD}>>"$LOG"
chmod 600 "$LOG"
on_exit() {
    local rc=$?
    { set +x; } 2>/dev/null
    [ -n "${WORK:-}" ] && rm -rf "$WORK"
    if [ "$rc" -ne 0 ]; then
        log "exit code $rc"
        snapshot "after failure"
        printf '\n %sFailed.%s Full log: %s\n' "$RD" "$CL" "$LOG" >&2
        printf ' Send that file when asking for help (it does not contain the API key).\n' >&2
    fi
}
trap on_exit EXIT
snapshot() {
    (
        set +e +o pipefail
        echo "----- node snapshot: $1"
        echo "## pveversion -v"; pveversion -v 2>&1 | head -20
        echo "## hostname"; hostname -f 2>&1
        echo "## addresses"; ip -br -4 a 2>&1
        echo "## routes"; ip -4 route 2>&1
        echo "## resolv.conf"; cat /etc/resolv.conf 2>&1
        echo "## cluster"; pvecm status 2>&1 | head -15
        echo "## port 8009"; ss -ltnp 'sport = :8009' 2>&1
        echo "## units"; systemctl is-active pve-unifi-dns.timer pve-unifi-dns-ui.service pveproxy.service lldpd.service 2>&1
        echo "## files"; ls -l /usr/local/bin/pve-unifi-dns*.py /usr/local/sbin/pve-unifi-dns-ui-patch \
            /usr/share/pve-manager/js/unifi-dns-tab.js /etc/apt/apt.conf.d/99pve-unifi-dns-ui 2>&1
        echo "## template"; grep -n 'pvemanagerlib\|unifi-dns-tab' /usr/share/pve-manager/index.html.tpl 2>&1
        echo "## config (key hidden)"
        for f in "$CONF" /etc/pve/pve-unifi-dns.conf /etc/pve-unifi-dns.conf; do
            [ -f "$f" ] && { echo "# $f"; sed 's/^\(UNIFI_API_KEY=\).\{1,\}/\1<set>/' "$f"; }
        done
        echo "## console 443"; timeout 5 bash -c "</dev/tcp/$(sed -n 's/^UNIFI_HOST=//p' "$CONF" 2>/dev/null | head -1 || echo none)/443" 2>&1 && echo reachable || echo "not reachable or not configured"
        echo "## journal pve-unifi-dns-ui"; journalctl -u pve-unifi-dns-ui -n 30 --no-pager 2>&1
        echo "## journal pve-unifi-dns"; journalctl -u pve-unifi-dns -n 30 --no-pager 2>&1
        echo "----- end snapshot"
    ) >&"$LOGFD" 2>&1
}
log "===== install.sh $* from $REPO@$REF (SELF_DIR=$SELF_DIR)"
snapshot "before"
BASH_XTRACEFD=$LOGFD
PS4='+ install.sh:${LINENO}: '
set -x
command -v pveversion >/dev/null || die "this is not a Proxmox VE host"
PVE_MAJOR=$(pveversion | sed -n 's|^pve-manager/\([0-9]*\)\..*|\1|p')
command -v python3 >/dev/null || die "python3 is missing"
[ "$UNATTENDED" = yes ] || command -v whiptail >/dev/null || die "whiptail is missing (apt install whiptail) or use --unattended"
NODE=$(hostname -s)
INSTALLED=no
[ -f /usr/local/bin/pve-unifi-dns.py ] && INSTALLED=yes

conf_get() { sed -n "s/^$1=//p" "$CONF" 2>/dev/null | head -1; }

header_info
if [ -n "$PVE_MAJOR" ] && [ "$PVE_MAJOR" -lt 8 ]; then
    die "Proxmox VE $PVE_MAJOR is too old; version 8 or 9 is needed"
fi
if [ -n "$PVE_MAJOR" ] && [ "$PVE_MAJOR" -gt 9 ]; then
    msg_warn "tested on Proxmox VE 9; this is version $PVE_MAJOR"
fi

# ------------------------------------------------------------- uninstall --
uninstall() {
    if [ "$UNATTENDED" = no ] && [ "$PURGE" = no ]; then
        whiptail --backtitle "$APP" --title "Uninstall" --yesno \
            "Remove $APP from node $NODE?" 8 60 || exit 0
        if [ -f "$CONF" ] && whiptail --backtitle "$APP" --title "DNS records" --defaultno --yesno \
            "Also delete the DNS records this node created in UniFi, and the 'UniFi DNS:' lines in guest Notes?\n\nChoose No to leave the records in UniFi." 11 70; then
            PURGE=yes
        fi
    fi
    if [ "$PURGE" = yes ] && [ -x /usr/local/bin/pve-unifi-dns.py ] && grep -qs '^UNIFI_API_KEY=.' "$CONF"; then
        msg_info "Deleting this node's records from UniFi"
        systemctl stop pve-unifi-dns.timer 2>/dev/null || true
        /usr/local/bin/pve-unifi-dns.py --purge 2>&1 | tail -1 | sed 's/^/   /'
        msg_ok "Records and Notes lines removed"
    fi
    msg_info "Stopping services"
    if [ "$VERBOSE" = yes ]; then
        systemctl disable --now pve-unifi-dns.timer pve-unifi-dns-ui.service || true
        systemctl stop pve-unifi-dns.service || true
    else
        systemctl disable --now pve-unifi-dns.timer pve-unifi-dns-ui.service >/dev/null 2>&1 || true
        systemctl stop pve-unifi-dns.service >/dev/null 2>&1 || true
    fi
    msg_ok "Services stopped"
    msg_info "Removing the Proxmox UI tab"
    rm -f /etc/apt/apt.conf.d/99pve-unifi-dns-ui /usr/share/pve-manager/js/unifi-dns-tab.js
    sed -i '/unifi-dns-tab\.js/d' /usr/share/pve-manager/index.html.tpl
    systemctl reload-or-restart pveproxy.service
    msg_ok "UI tab removed (reload the Proxmox page)"
    msg_info "Removing files"
    rm -f /etc/systemd/system/pve-unifi-dns.service /etc/systemd/system/pve-unifi-dns.timer \
          /etc/systemd/system/pve-unifi-dns-ui.service /usr/local/bin/pve-unifi-dns.py \
          /usr/local/bin/pve-unifi-dns-ui.py /usr/local/sbin/pve-unifi-dns-ui-patch /run/pve-unifi-dns.lock
    rm -rf /usr/local/share/pve-unifi-dns
    systemctl daemon-reload
    msg_ok "Program files removed"
    local keep=yes
    if [ "$UNATTENDED" = yes ]; then
        [ "$PURGE" = yes ] && keep=no
    elif whiptail --backtitle "$APP" --title "Settings" --defaultno --yesno \
        "Delete the settings too (UniFi address, domain and API key)?\n\nKeep them to reinstall later without setting up again." 10 70; then
        keep=no
    fi
    if [ "$keep" = no ]; then
        if [ -f /etc/pve/corosync.conf ]; then
            msg_warn "cluster: settings in /etc/pve are shared; left in place for the other nodes"
        else
            rm -f "$CONF" /etc/pve/pve-unifi-dns.icons.json
        fi
        rm -rf /var/lib/pve-unifi-dns
        msg_ok "Settings and state removed"
    else
        msg_ok "Settings kept in $CONF"
    fi
    echo
    msg_ok "$APP removed from $NODE. lldpd and the node's DNS settings were left as they are."
}

if [ "$ACTION" = uninstall ]; then
    [ "$INSTALLED" = yes ] || [ -f "$CONF" ] || die "$APP is not installed on this node"
    uninstall
    exit 0
fi

ask_verbose() {
    [ "$VERBOSE" = yes ] && return 0
    if whiptail --backtitle "$APP" --title "Verbose install" --defaultno --yesno \
"Show every command on screen while it installs?

Choose Yes when troubleshooting. Either way the full log is written to
$LOG" 11 74; then
        VERBOSE=yes
    fi
}

# --------------------------------------------------------- install/update --
if [ "$UNATTENDED" = no ]; then
    if [ "$INSTALLED" = yes ]; then
        choice=$(whiptail --backtitle "$APP" --title "$APP on $NODE" --menu \
            "It is already installed. What do you want to do?" 13 64 3 \
            "update" "Update to the latest version, keep settings" \
            "uninstall" "Remove it from this node" \
            "settings" "Update and change the basic settings" 3>&1 1>&2 2>&3) || exit 0
        [ "$choice" = uninstall ] && { uninstall; exit 0; }
        [ "$choice" = update ] && ASK=no || ASK=yes
        ask_verbose
    else
        whiptail --backtitle "$APP" --title "$APP" --yesno \
"This installs on node $NODE:

 - a sync that keeps UniFi local DNS records for every guest
   (<guest>.<domain>), runs every 2 minutes
 - Datacenter > UniFi DNS in the Proxmox web UI, with a Setup window
 - a small API service on port 8009 used by that tab

Needs a UniFi OS console (UniFi Network 9 or newer) and an API key.
Continue?" 17 74 || exit 0
        ASK=yes
        ask_verbose
    fi
fi

# defaults: existing settings, else what the node already uses
GW=$(ip -4 route show default 2>/dev/null | awk '{print $3; exit}')
NODE_SEARCH=$(pvesh get /nodes/"$NODE"/dns --output-format json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("search",""))' 2>/dev/null || true)
UNIFI_HOST=${UNIFI_HOST:-$(conf_get UNIFI_HOST)}; UNIFI_HOST=${UNIFI_HOST:-$GW}
DOMAIN=${DOMAIN:-$(conf_get DOMAIN)}; DOMAIN=${DOMAIN:-${NODE_SEARCH:-localdomain}}
SET_NODE_DNS=${SET_NODE_DNS:-yes}; INSTALL_LLDP=${INSTALL_LLDP:-yes}
{ set +x; } 2>/dev/null          # no tracing while the API key is in a variable
log "trace paused while the API key is handled"
KEY="${UNIFI_API_KEY:-}"

if [ "$UNATTENDED" = no ] && [ "${ASK:-yes}" = yes ]; then
    UNIFI_HOST=$(whiptail --backtitle "$APP" --title "UniFi console" --inputbox \
        "Address of the UniFi console (gateway) that holds your local DNS:" 9 70 "$UNIFI_HOST" 3>&1 1>&2 2>&3) || exit 0
    DOMAIN=$(whiptail --backtitle "$APP" --title "Domain" --inputbox \
        "Local DNS domain. Guests become <guest>.<domain>.\nUse the Domain Name set on your UniFi networks:" 10 70 "$DOMAIN" 3>&1 1>&2 2>&3) || exit 0
    KEY=$(whiptail --backtitle "$APP" --title "UniFi API key" --passwordbox \
        "UniFi OS > Settings > Control Plane > Integrations > Create API Key.\n\nLeave empty to keep the saved key, or to enter it later in\nProxmox > Datacenter > UniFi DNS > Setup." 13 74 3>&1 1>&2 2>&3) || exit 0
    opts=$(whiptail --backtitle "$APP" --title "Options" --separate-output --checklist \
        "Also do on this node (space to toggle):" 12 74 2 \
        "dns"  "Use $UNIFI_HOST as this node's DNS server, search $DOMAIN" ON \
        "lldp" "Install lldpd so UniFi shows guests under this host" ON 3>&1 1>&2 2>&3) || exit 0
    SET_NODE_DNS=no; INSTALL_LLDP=no
    echo "$opts" | grep -qx dns && SET_NODE_DNS=yes
    echo "$opts" | grep -qx lldp && INSTALL_LLDP=yes
fi
[ -n "$UNIFI_HOST" ] || die "no UniFi console address"
[ -n "$DOMAIN" ] || die "no domain"

# ------------------------------------------------------------- get files --
WORK=$(mktemp -d /tmp/pve-unifi-dns.XXXXXX)
if [ -f "$SELF_DIR/node-install.sh" ] && [ -f "$SELF_DIR/pve-unifi-dns.py" ]; then
    cp -a "$SELF_DIR/." "$WORK/"
    msg_ok "Using files from $SELF_DIR ($(cat "$SELF_DIR/VERSION" 2>/dev/null || echo dev))"
else
    msg_info "Downloading $REPO@$REF"
    curl -fsSL "https://codeload.github.com/$REPO/tar.gz/$REF" | tar -xz -C "$WORK" --strip-components=1 \
        || die "download from github.com/$REPO failed"
    msg_ok "Downloaded $REPO@$REF ($(cat "$WORK/VERSION" 2>/dev/null || echo unknown))"
fi
if [ -n "$KEY" ]; then
    ( umask 077; printf '%s\n' "$KEY" > "$WORK/.apikey" )
fi
log "API key given in this run: $([ -f "$WORK/.apikey" ] && echo yes || echo no)"
unset KEY UNIFI_API_KEY
log "settings: UNIFI_HOST=$UNIFI_HOST DOMAIN=$DOMAIN SET_NODE_DNS=$SET_NODE_DNS INSTALL_LLDP=$INSTALL_LLDP VERBOSE=$VERBOSE"
TRACEFD=$LOGFD
if [ "$VERBOSE" = yes ]; then
    exec {TRACEFD}> >(tee -a "$LOG" >&2)
    BASH_XTRACEFD=$TRACEFD
    msg_ok "Verbose: every command is shown below and logged to $LOG"
fi
set -x

# --------------------------------------------------------------- install --
echo
env UNIFI_HOST="$UNIFI_HOST" DOMAIN="$DOMAIN" SET_NODE_DNS="$SET_NODE_DNS" \
    INSTALL_LLDP="$INSTALL_LLDP" INSTALL_UI=yes ACME=no VERBOSE="$VERBOSE" BASH_XTRACEFD="$TRACEFD" \
    bash -x "$WORK/node-install.sh" 2>&1 | tee -a "$LOG" || die "installation stopped, see the messages above"

ADDR=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
msg_ok "${BLD}$APP is installed on $NODE${CL}"
echo "   Open https://${ADDR:-$NODE}:8006, reload the page, then Datacenter > UniFi DNS."
grep -qs '^UNIFI_API_KEY=.' "$CONF" || echo "   The Setup window opens there to finish: enter the UniFi API key and save."
echo "   Update or remove later by running this installer again."
echo "   Log of this run: $LOG"
{ set +x; } 2>/dev/null
snapshot "after install"
