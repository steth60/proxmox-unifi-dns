# UniFi DNS for Proxmox VE

Creates local DNS records in a UniFi console for your Proxmox guests and nodes, and adds a
UniFi DNS page to the Proxmox web UI to manage them.

Each running container or VM gets `<guest>.<domain>`, each node gets `<node>.<domain>`.
Records are updated every two minutes when guests are created, renamed, stopped or deleted.

> **Use at your own risk.** This is an unofficial tool. It uses undocumented UniFi API calls and
> modifies the Proxmox web UI. It is provided as is, without warranty (see [LICENSE](LICENSE)).
> Test it before relying on it.

## Features

- A records for running guests and nodes, CNAME aliases via tags
- Datacenter > UniFi DNS page in Proxmox: guests, records, setup, manual A/CNAME records
- Guest names and icons pushed to the UniFi client list
- Only records created by this tool are ever changed or deleted

## Requirements

- Proxmox VE 9 (8 is untested)
- UniFi OS console with UniFi Network 9 or newer
- A UniFi API key: UniFi OS > Settings > Control Plane > Integrations
- Clients using the UniFi gateway for DNS

## Install

On the Proxmox host as root:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/steth60/proxmox-unifi-dns/main/install.sh)"
```

The installer asks for the console address, domain and API key, then runs the first sync.
Reload the Proxmox page and open Datacenter > UniFi DNS. If you skipped the key, the Setup
window opens there.

Run the same command again to update or uninstall. For a scripted install use `--unattended`
with `UNIFI_HOST`, `DOMAIN` and `UNIFI_API_KEY` set. To install a fixed version instead of
`main`, set `PUD_REF` to a release tag.

Files installed per node:

| Path | Purpose |
|---|---|
| `/usr/local/bin/pve-unifi-dns.py`, `pve-unifi-dns.timer` | sync, every 2 minutes |
| `/usr/local/bin/pve-unifi-dns-ui.py`, `pve-unifi-dns-ui.service` | API for the page, port 8009 |
| `/usr/share/pve-manager/js/unifi-dns-tab.js` | the page, loaded from `index.html.tpl` |
| `/etc/apt/apt.conf.d/99pve-unifi-dns-ui` | re-adds the page after Proxmox updates |
| `/etc/pve/priv/pve-unifi-dns.conf` | settings and API key, root only |

## Usage

Guest tags:

| Tag | Effect |
|---|---|
| `nodns` | don't publish this guest |
| `dns-NAME` | publish as `NAME.<domain>` |
| `dns-alias-NAME` | add CNAME `NAME.<domain>` |

Manual records added on the page or with the CLI are never touched by the sync.

```bash
pve-unifi-dns.py --status    # what each guest publishes
pve-unifi-dns.py --list      # all records in UniFi, * = created by the sync
pve-unifi-dns.py --add printer 192.168.1.20
pve-unifi-dns.py --remove printer
```

Guest IPs come from, in order: a static IP in the container config, the QEMU guest agent,
the address Proxmox reports for a running container, and the UniFi client list (by MAC).

## Security

- The page's API (port 8009) checks each request against your Proxmox login. Reading needs
  `Sys.Audit` on `/`, changes need `Sys.Modify` on `/`. Browser requests are only accepted
  from the node's own Proxmox page.
- The API key is stored in plain text in `/etc/pve/priv/pve-unifi-dns.conf`, readable by root
  only. It is never returned to the browser.
- The console's TLS certificate is pinned on first contact. If it changes, the key is not sent
  and the sync stops until the key is entered again in Setup. Changing the console address
  also requires entering the key again.
- Create the API key with a UniFi admin limited to the Network application.
- Don't expose port 8009 to the internet. If the Proxmox firewall is on, allow it from your
  management network.
- The page loads device icons from `static.ui.com`.

## Certificates

To avoid browser warnings without installing a CA on every device, use a Let's Encrypt
certificate for `<node>.<domain>`. This needs a domain you own (for example `lan.example.com`)
with DNS at a provider Proxmox supports for the DNS challenge, such as Cloudflare. Names still
resolve only inside your network. Hostnames appear in public certificate transparency logs.

Set it up in Setup > Certificate, which uses Proxmox's own ACME settings.

## Multiple nodes

- Standalone nodes: install on each. Guest names must be unique across nodes.
- Clusters: settings are shared via `/etc/pve` and `RUN_ON_NODE` selects the node that syncs.
  Not tested yet.
- `setup.sh` installs on several nodes over SSH using `setup.conf` (see `setup.conf.example`).

## Uninstall

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/steth60/proxmox-unifi-dns/main/install.sh)" -- --uninstall
```

You're asked whether to delete the records it created and whether to keep the settings.
The Proxmox UI template is restored. lldpd and the node DNS settings are left unchanged.

## Limitations

- Tested on Proxmox VE 9.1 and 9.2 (standalone, containers) with a UDM Pro on UniFi Network 10.6.
- UniFi DNS records, client names and icons use internal API endpoints that may change with
  UniFi updates. Run `pve-unifi-dns.py --list` after updating UniFi.
- The page hooks into internal Proxmox UI classes. If they change, the page won't load; the
  rest of Proxmox is unaffected.
- UniFi has no wildcard records.
- UniFi applies changes a few seconds after the API call and keeps DHCP hostnames for the
  length of the lease.

## Tips

Install `lldpd` on each node (the installer offers it) and UniFi will show guests under their
host in the topology view.

## Troubleshooting

- Page can't reach the service: open `https://<node>:8009` once and accept the certificate,
  or allow port 8009 in the Proxmox firewall.
- Logs: `journalctl -u pve-unifi-dns.service` and `journalctl -u pve-unifi-dns-ui`.
- "Certificate has changed": the console was replaced or reset. Enter the API key in Setup again.

## License

MIT
