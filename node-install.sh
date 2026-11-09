#!/bin/bash
# Installs or updates pve-unifi-dns on this Proxmox node. Run as root from the
# folder that holds the project files. setup.sh on your workstation copies the
# files here and calls this with the settings from setup.conf.
#
# Settings (environment):
#   UNIFI_HOST      gateway / console address                 (required)
#   DOMAIN          local DNS domain                          (required)
#   DNS_SERVER      resolver for the node itself              (default: UNIFI_HOST)
#   SET_NODE_DNS    yes|no  set the node's DNS server + search domain   (default yes)
#   INSTALL_LLDP    yes|no  install lldpd (topology nesting in UniFi)   (default yes)
#   INSTALL_UI      yes|no  Datacenter > UniFi DNS tab                  (default yes)
#   ACME            yes|no  certificate for <node>.<DOMAIN>             (default no)
#   ACME_EMAIL, ACME_DIRECTORY, ACME_PLUGIN_ID, ACME_DNS_API   when ACME=yes
#   CHECK_ONLY      yes     only report, change nothing
# Secrets are read from files next to this script and deleted afterwards:
#   .apikey         UniFi API key (only needed on first install or to change it)
#   .acme-data      DNS provider credentials for ACME, KEY=value lines

set -Eeuo pipefail
VERBOSE=${VERBOSE:-no}
# On any failure, say which command failed, where, and with what exit code.
trap 'rc=$?; [[ $BASH_COMMAND == return* ]] || printf "   ERROR %s line %s: \"%s\" exited with %s\n" node-install.sh "$LINENO" "$BASH_COMMAND" "$rc" >&2' ERR
# Hide a command's output unless this is a verbose install.
quiet() {
    local rc=0 out
    out=$(mktemp)
    if [ "$VERBOSE" = yes ]; then "$@" || rc=$?; else "$@" >"$out" 2>&1 || rc=$?; fi
    if [ "$rc" -ne 0 ]; then
        [ "$VERBOSE" = yes ] || tail -20 "$out" | sed 's/^/   | /' >&2
        printf '   ERROR %s line %s: "%s" exited with %s\n' "${BASH_SOURCE[1]:-node-install.sh}" "${BASH_LINENO[0]}" "$*" "$rc" >&2
    fi
    rm -f "$out"
    return "$rc"
}
HERE=$(cd "$(dirname "$0")" && pwd)
cleanup() { for f in "$HERE/.apikey" "$HERE/.acme-data"; do [ -f "$f" ] && shred -u "$f" 2>/dev/null || rm -f "$f"; done; }
trap cleanup EXIT

: "${UNIFI_HOST:?UNIFI_HOST is required}"
: "${DOMAIN:?DOMAIN is required}"
DOMAIN=$(echo "$DOMAIN" | tr 'A-Z' 'a-z' | sed 's/^\.*//; s/\.*$//')
DNS_SERVER=${DNS_SERVER:-$UNIFI_HOST}
SET_NODE_DNS=${SET_NODE_DNS:-yes}
INSTALL_LLDP=${INSTALL_LLDP:-yes}
INSTALL_UI=${INSTALL_UI:-yes}
ACME=${ACME:-no}
CHECK_ONLY=${CHECK_ONLY:-no}
NODE=$(hostname -s)
FQDN="$(echo "$NODE" | tr 'A-Z' 'a-z').$DOMAIN"
CONF=/etc/pve/priv/pve-unifi-dns.conf
OLD_CONFS="/etc/pve/pve-unifi-dns.conf /etc/pve-unifi-dns.conf"

valid_name() { [[ "$1" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$ ]]; }
for v in UNIFI_HOST DOMAIN DNS_SERVER; do
    valid_name "${!v}" || { echo "invalid $v: ${!v}"; exit 1; }
done

step() { printf '\n== %s\n' "$*"; }
ok()   { printf '   ok    %s\n' "$*"; }
did()  { printf '   done  %s\n' "$*"; }
warn() { printf '   WARN  %s\n' "$*"; }
fail() { printf '   FAIL  %s\n' "$*"; exit 1; }

# ------------------------------------------------------------------ checks --
step "Checks on $NODE"
command -v pveversion >/dev/null || fail "this is not a Proxmox node"
ok "$(pveversion | cut -d/ -f1-2)"
command -v python3 >/dev/null || fail "python3 missing"
ok "python $(python3 -c 'import sys; print(sys.version.split()[0])')"
if pvecm status >/dev/null 2>&1; then
    ok "cluster member: config is shared, one node does the sync (RUN_ON_NODE)"
    CLUSTER=yes
else
    ok "standalone node"
    CLUSTER=no
fi
REACHABLE=yes
timeout 5 bash -c "</dev/tcp/$UNIFI_HOST/443" 2>/dev/null && ok "UniFi console $UNIFI_HOST reachable" \
    || { REACHABLE=no; warn "cannot reach $UNIFI_HOST:443: fix the address in Proxmox > Datacenter > UniFi DNS > Setup"; }
existing=""
for f in $CONF $OLD_CONFS; do [ -f "$f" ] && { existing=$f; break; }; done
if [ -n "$existing" ]; then ok "existing config $existing"; else ok "no config yet (first install)"; fi
NO_KEY=no
if [ ! -f "$HERE/.apikey" ] && ! grep -qs '^UNIFI_API_KEY=.' $CONF $OLD_CONFS; then
    NO_KEY=yes
    warn "no UniFi API key yet: finish in Proxmox > Datacenter > UniFi DNS > Setup"
fi

if [ "$CHECK_ONLY" = yes ]; then
    step "Current state"
    for u in pve-unifi-dns.timer pve-unifi-dns-ui.service lldpd.service; do
        printf '   %-26s %s\n' "$u" "$(systemctl is-active $u 2>/dev/null || true)"
    done
    grep -q unifi-dns-tab /usr/share/pve-manager/index.html.tpl && echo "   UI tab patch present" || echo "   UI tab patch absent"
    echo "   node DNS: $(pvesh get /nodes/$NODE/dns --output-format json)"
    [ -x /usr/local/bin/pve-unifi-dns.py ] && [ -n "$existing" ] && /usr/local/bin/pve-unifi-dns.py --list | tail -1
    exit 0
fi

# ------------------------------------------------------------------ files --
step "Program files"
install -d -m 700 /var/lib/pve-unifi-dns && chmod 700 /var/lib/pve-unifi-dns
install -m 755 "$HERE/pve-unifi-dns.py" /usr/local/bin/pve-unifi-dns.py
install -m 644 "$HERE/pve-unifi-dns.service" "$HERE/pve-unifi-dns.timer" /etc/systemd/system/
did "sync script and timer units"

# ------------------------------------------------------------- web UI tab --
if [ "$INSTALL_UI" = yes ]; then
    step "Datacenter > UniFi DNS tab"
    install -m 755 "$HERE/pve-unifi-dns-ui.py" /usr/local/bin/pve-unifi-dns-ui.py
    install -m 755 "$HERE/pve-unifi-dns-ui-patch" /usr/local/sbin/pve-unifi-dns-ui-patch
    install -d /usr/local/share/pve-unifi-dns
    install -m 644 "$HERE/ui/unifi-dns-tab.js" /usr/local/share/pve-unifi-dns/unifi-dns-tab.js
    rm -f /usr/local/share/pve-unifi-dns/index.html
    install -m 644 "$HERE/99pve-unifi-dns-ui" /etc/apt/apt.conf.d/99pve-unifi-dns-ui
    install -m 644 "$HERE/pve-unifi-dns-ui.service" /etc/systemd/system/
    install -d -m 700 /var/lib/pve-unifi-dns
    [ -f /var/lib/pve-unifi-dns/index.html.tpl.orig ] \
        || cp /usr/share/pve-manager/index.html.tpl /var/lib/pve-unifi-dns/index.html.tpl.orig
    systemctl daemon-reload
    systemctl stop pve-unifi-dns-ui.service 2>/dev/null || true
    if ss -ltnH 'sport = :8009' | grep -q .; then
        fail "port 8009 is used by another program on this node: $(ss -ltnpH 'sport = :8009' | grep -o 'users:.*' | head -1)"
    fi
    systemctl enable -q pve-unifi-dns-ui.service
    systemctl start pve-unifi-dns-ui.service
    quiet /usr/local/sbin/pve-unifi-dns-ui-patch
    sleep 1
    systemctl is-active -q pve-unifi-dns-ui.service && ok "service on port 8009" || fail "pve-unifi-dns-ui did not start"
    grep -q unifi-dns-tab /usr/share/pve-manager/index.html.tpl && ok "tab added to the Proxmox UI" || fail "UI patch missing"
fi

# ----------------------------------------------------------------- config --
step "Config $CONF (root only)"
if [ ! -f "$CONF" ] && [ -n "$existing" ] && [ "$existing" != "$CONF" ]; then
    cp "$existing" "$CONF" && rm -f "$existing"
    did "moved $existing to $CONF"
fi
RUN_ON=""
[ "$CLUSTER" = no ] && RUN_ON=$NODE
if [ "$CLUSTER" = yes ]; then
    [ -f "$CONF" ] && RUN_ON=$(sed -n 's/^RUN_ON_NODE=//p' "$CONF" | head -1)
    RUN_ON=${RUN_ON:-$NODE}
fi
UNIFI_HOST="$UNIFI_HOST" DOMAIN="$DOMAIN" RUN_ON="$RUN_ON" python3 - "$CONF" "$HERE/pve-unifi-dns.conf" "$HERE/.apikey" <<'PY'
import os, re, sys
conf, template, keyfile = sys.argv[1:4]
want = {"UNIFI_HOST": os.environ["UNIFI_HOST"], "DOMAIN": os.environ["DOMAIN"],
        "RUN_ON_NODE": os.environ["RUN_ON"]}
if os.path.exists(keyfile):
    key = open(keyfile).read().strip()
    if key:
        want["UNIFI_API_KEY"] = key
KEY = re.compile(r"\s*([A-Z_]+)=")
base = open(conf).read().splitlines() if os.path.exists(conf) else open(template).read().splitlines()
out, present = [], set()
for ln in base:
    m = KEY.match(ln)
    if m and m.group(1) in want:
        ln = "%s=%s" % (m.group(1), want[m.group(1)])
    if m:
        present.add(m.group(1))
    out.append(ln)
for ln in open(template).read().splitlines():          # settings added in newer versions
    m = KEY.match(ln)
    if m and m.group(1) not in present:
        out.append(ln if m.group(1) not in want else "%s=%s" % (m.group(1), want[m.group(1)]))
        present.add(m.group(1))
for k, v in want.items():
    if k not in present:
        out.append("%s=%s" % (k, v))
with open(conf, "w") as fh:
    fh.write("\n".join(out) + "\n")
print("   done  UNIFI_HOST=%s DOMAIN=%s RUN_ON_NODE=%s%s" % (
    want["UNIFI_HOST"], want["DOMAIN"], want["RUN_ON_NODE"], ", API key updated" if "UNIFI_API_KEY" in want else ""))
PY
if [ "$REACHABLE" = no ]; then
    NO_KEY=yes
elif [ "$NO_KEY" = no ]; then
    if /usr/local/bin/pve-unifi-dns.py --list >/dev/null 2>"$HERE/.err"; then
        ok "UniFi API key works"
    else
        sed 's/^/   /' "$HERE/.err"
        warn "UniFi API call failed: check the key in Proxmox > Datacenter > UniFi DNS > Setup"
        NO_KEY=yes
    fi
    rm -f "$HERE/.err"
fi

# --------------------------------------------------------------- node DNS --
if [ "$SET_NODE_DNS" = yes ]; then
    step "Node DNS"
    cur=$(pvesh get /nodes/$NODE/dns --output-format json)
    if echo "$cur" | DNS_SERVER="$DNS_SERVER" DOMAIN="$DOMAIN" python3 -c 'import json, os, sys
d = json.load(sys.stdin)
sys.exit(0 if d.get("dns1") == os.environ["DNS_SERVER"] and d.get("search") == os.environ["DOMAIN"] else 1)'; then
        ok "server $DNS_SERVER, search $DOMAIN"
    else
        pvesh set /nodes/$NODE/dns --dns1 "$DNS_SERVER" --search "$DOMAIN"
        did "was $cur, now server $DNS_SERVER, search $DOMAIN"
    fi
fi

# ------------------------------------------------------------------ lldpd --
if [ "$INSTALL_LLDP" = yes ]; then
    step "lldpd (guests shown under this host in UniFi)"
    if dpkg -s lldpd >/dev/null 2>&1; then
        ok "installed, $(systemctl is-active lldpd)"
    else
        export DEBIAN_FRONTEND=noninteractive
        quiet apt-get install -y -q lldpd \
            || { quiet apt-get update -q && quiet apt-get install -y -q lldpd; }
        did "installed, $(systemctl is-active lldpd)"
    fi
fi

# ------------------------------------------------------------------- ACME --
if [ "$ACME" = yes ]; then
    step "Certificate for $FQDN"
    : "${ACME_EMAIL:?ACME_EMAIL is required}" "${ACME_PLUGIN_ID:?}" "${ACME_DNS_API:?}"
    ACME_DIRECTORY=${ACME_DIRECTORY:-https://acme-v02.api.letsencrypt.org/directory}
    if pvesh get /cluster/acme/account --output-format json | grep -q '"default"'; then
        ok "ACME account 'default' exists"
    elif [ -t 0 ]; then
        echo "   Registering ACME account 'default' for $ACME_EMAIL."
        echo "   The CA's terms of service are shown next; answer the question yourself."
        pvenode acme account register default "$ACME_EMAIL" --directory "$ACME_DIRECTORY"
    else
        warn "no ACME account yet: add one in Proxmox (Datacenter > UniFi DNS > Setup > Certificate > Accounts) and run this again"
        ACME=skip
    fi
fi
if [ "$ACME" = yes ]; then
    if pvesh get /cluster/acme/plugins --output-format json | grep -q "\"plugin\":\"$ACME_PLUGIN_ID\""; then
        if [ -f "$HERE/.acme-data" ]; then
            pvenode acme plugin set "$ACME_PLUGIN_ID" --api "$ACME_DNS_API" --data "$HERE/.acme-data"
            did "DNS plugin $ACME_PLUGIN_ID credentials updated"
        else
            ok "DNS plugin $ACME_PLUGIN_ID exists"
        fi
    else
        [ -f "$HERE/.acme-data" ] || fail "no DNS provider credentials (.acme-data) for plugin $ACME_PLUGIN_ID"
        pvenode acme plugin add dns "$ACME_PLUGIN_ID" --api "$ACME_DNS_API" --data "$HERE/.acme-data"
        did "DNS plugin $ACME_PLUGIN_ID added ($ACME_DNS_API)"
    fi
    pvenode config set --acme account=default --acmedomain0 "$FQDN,plugin=$ACME_PLUGIN_ID"
    did "node $NODE will request $FQDN"
    have=$(openssl x509 -in /etc/pve/local/pveproxy-ssl.pem -noout -ext subjectAltName 2>/dev/null || true)
    if echo "$have" | grep -q "DNS:$FQDN"; then
        ok "certificate for $FQDN already installed (Proxmox renews it daily when due)"
    else
        pvenode acme cert order --force
        did "certificate ordered and installed; Proxmox renews it automatically"
    fi
fi

# ------------------------------------------------------------- first sync --
if [ "$NO_KEY" = yes ]; then
    step "Finished on $NODE (setup continues in Proxmox)"
    echo "   Reload Proxmox, open Datacenter > UniFi DNS; the Setup window opens by itself."
    exit 0
fi

step "Sync"
systemctl daemon-reload
since=$(date '+%Y-%m-%d %H:%M:%S')
# through systemd, so it can never overlap a timer run; waits until the sync has finished
systemctl start pve-unifi-dns.service || true
journalctl -u pve-unifi-dns.service --since "$since" -o cat --no-pager \
    | grep -vE '^(Starting|Finished|pve-unifi-dns.service)' | sed 's/^/   /' | tail -30
systemctl is-failed -q pve-unifi-dns.service && fail "sync failed, see: journalctl -u pve-unifi-dns.service"
systemctl enable -q --now pve-unifi-dns.timer
ok "timer every 2 min: $(systemctl is-active pve-unifi-dns.timer)"

step "Finished on $NODE"
echo "   Records: /usr/local/bin/pve-unifi-dns.py --list"
echo "   Status:  /usr/local/bin/pve-unifi-dns.py --status"
[ "$INSTALL_UI" = yes ] && echo "   UI:      reload Proxmox, then Datacenter > UniFi DNS"
if [ "$ACME" = yes ]; then
    echo "   Open:    https://$FQDN:8006 (trusted certificate)"
else
    echo "   Open:    https://$FQDN:8006 (self-signed certificate until ACME is set up)"
fi
