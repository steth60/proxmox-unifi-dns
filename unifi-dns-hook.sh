#!/bin/sh
# Optional Proxmox hookscript: triggers a sync right after a guest starts or stops,
# so you don't wait for the timer. Install:
#   cp unifi-dns-hook.sh /var/lib/vz/snippets/ && chmod +x /var/lib/vz/snippets/unifi-dns-hook.sh
#   qm  set <vmid> --hookscript local:snippets/unifi-dns-hook.sh   # VMs
#   pct set <ctid> --hookscript local:snippets/unifi-dns-hook.sh   # containers
# Arguments from Proxmox: $1 = vmid, $2 = phase
case "$2" in
  post-start)
    # DHCP lease / guest agent need a moment; sync in the background after 20 s
    ( sleep 20; systemctl start --no-block pve-unifi-dns.service ) >/dev/null 2>&1 &
    ;;
  post-stop)
    systemctl start --no-block pve-unifi-dns.service
    ;;
esac
exit 0
