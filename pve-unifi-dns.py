#!/usr/bin/env python3
"""
pve-unifi-dns: keep UniFi local DNS records in sync with Proxmox guests.

For every running VM / container in the Proxmox cluster (and every online node) this
creates an A record <guest-name>.<DOMAIN> -> <guest IP> in the UniFi Network application
(Settings > Policy Table > DNS on Network 9.4+, Settings > Routing > DNS before).
When a guest disappears or stops, the record it created is removed.

Control from inside Proxmox, via tags on the guest:
    dns               opt in (only matters when ONLY_TAG=dns)
    nodns             never publish this guest
    dns-<name>        publish as <name>.<DOMAIN> instead of the VM name
    dns-alias-<name>  extra CNAME <name>.<DOMAIN> -> main record (several allowed)

Status is written to each guest's Notes (one line starting "UniFi DNS:") and
can be listed with --status.

UniFi client list: each guest's client entry gets the Proxmox guest name as its
UniFi name, and an icon. The icon is the one chosen in the web page (stored in
ICON_FILE), else a UniFi catalogue entry whose name matches the guest name
(Jellyfin, Home Assistant, Nextcloud ...), else DEFAULT_ICON.

Manual records, from the node shell (never touched by the sync afterwards):
    --list                       every record UniFi holds, ours marked with *
    --add NAME IP                A record; NAME without a dot gets .<DOMAIN>
    --add-cname NAME TARGET      CNAME record
    --remove NAME                delete a record you added by hand

Only records this script created (tracked in STATE_FILE) are ever deleted,
so hand-made UniFi DNS entries are left alone.

Stdlib only. Config is read from /etc/pve/priv/pve-unifi-dns.conf (root-only,
shared by a cluster), else /etc/pve/pve-unifi-dns.conf or /etc/pve-unifi-dns.conf. Set RUN_ON_NODE so the timer can be
enabled on every node while only one of them does the work.
"""

import argparse
import contextlib
import fcntl
import hashlib
import http.client
import json
import os
import re
import socket
import ssl
import subprocess
import sys

CONF_PATHS = ["/etc/pve/priv/pve-unifi-dns.conf", "/etc/pve/pve-unifi-dns.conf", "/etc/pve-unifi-dns.conf"]
NOTE_PREFIX = "UniFi DNS:"

DEFAULTS = {
    "UNIFI_HOST": "192.168.1.1",   # gateway / console IP or name
    "UNIFI_API_KEY": "",           # UniFi OS > Settings > Control Plane > Integrations
    "UNIFI_SITE": "default",
    "DOMAIN": "home.arpa",         # suffix added to every guest name
    "VERIFY_TLS": "false",         # consoles use a self-signed cert by default
    "UNIFI_CERT_SHA256": "",       # console certificate pinned on first contact (used when VERIFY_TLS=false)
    "INCLUDE_STOPPED": "false",    # keep records for stopped guests (needs a known IP)
    "ONLY_TAG": "",                # if set, only guests carrying this Proxmox tag are synced
    "WRITE_NOTES": "true",         # write a status line into each guest's Notes
    "PUBLISH_NODES": "true",       # also publish <node>.<DOMAIN> for every online Proxmox node
    "RUN_ON_NODE": "",             # hostname of the node that does the work; empty = this one
    "STATE_FILE": "/var/lib/pve-unifi-dns/state.json",
    "STATUS_FILE": "/var/lib/pve-unifi-dns/status.json",   # last sync result, read by the web page
    "SYNC_CLIENT_NAMES": "true",   # UniFi client name = Proxmox guest name
    "SYNC_CLIENT_ICONS": "true",   # UniFi client icon = chosen / matched / DEFAULT_ICON
    "DEFAULT_ICON": "5254",        # UniFi device id used when nothing matches (5254 = Proxmox)
    "ICON_FILE": "/etc/pve/pve-unifi-dns.icons.json",       # {vmid: device id} chosen in the page
    "FINGERPRINT_CACHE": "/var/lib/pve-unifi-dns/fingerprints.json",
}
BOOL_KEYS = ("VERIFY_TLS", "INCLUDE_STOPPED", "WRITE_NOTES", "PUBLISH_NODES",
             "SYNC_CLIENT_NAMES", "SYNC_CLIENT_ICONS")


# ---------------------------------------------------------------- config ----

def load_conf(explicit=None):
    conf = dict(DEFAULTS)
    paths = [explicit] if explicit else CONF_PATHS
    used = None
    for path in paths:
        if path and os.path.exists(path):
            used = path
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    conf[k.strip()] = v.strip().strip('"').strip("'")
            break
    for k in DEFAULTS:                      # environment overrides the file
        if k in os.environ:
            conf[k] = os.environ[k]
    for k in BOOL_KEYS:
        conf[k] = str(conf[k]).lower() in ("1", "true", "yes")
    if conf["UNIFI_API_KEY"] == "paste-your-key-here":     # placeholder left by version 0.1.x installs
        conf["UNIFI_API_KEY"] = ""
    conf["_path"] = used
    return conf


LOCK_FILE = "/run/pve-unifi-dns.lock"


@contextlib.contextmanager
def sync_lock(wait=180):
    """One writer at a time: timer, CLI and web page never change UniFi concurrently."""
    import time
    fh = open(LOCK_FILE, "w")
    deadline = time.time() + wait
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.time() > deadline:
                fh.close()
                raise RuntimeError("another pve-unifi-dns run is still busy")
            time.sleep(0.5)
    try:
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


SETTING_KEYS = ("UNIFI_HOST", "UNIFI_API_KEY", "UNIFI_CERT_SHA256", "UNIFI_SITE", "DOMAIN", "VERIFY_TLS", "INCLUDE_STOPPED",
                "ONLY_TAG", "WRITE_NOTES", "PUBLISH_NODES", "RUN_ON_NODE", "SYNC_CLIENT_NAMES",
                "SYNC_CLIENT_ICONS", "DEFAULT_ICON")


def save_conf(updates):
    """Write KEY=value settings into the config in use (or the root-only default), keeping comments."""
    path = load_conf()["_path"] or CONF_PATHS[0]
    key_re = re.compile(r"\s*([A-Z_]+)=")
    if os.path.exists(path):
        with open(path) as fh:
            lines = fh.read().splitlines()
    else:
        lines = ["# pve-unifi-dns settings, written by the UniFi DNS tab in Proxmox"]
    out, present = [], set()
    for ln in lines:
        m = key_re.match(ln)
        if m and m.group(1) in updates:
            ln = "%s=%s" % (m.group(1), updates[m.group(1)])
        if m:
            present.add(m.group(1))
        out.append(ln)
    for k, v in updates.items():
        if k not in present:
            out.append("%s=%s" % (k, v))
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as fh:
        fh.write("\n".join(out) + "\n")
    os.replace(tmp, path)
    return path


def log(msg, *a):
    print(msg % a if a else msg, file=sys.stderr)


# ----------------------------------------------------------------- unifi ----

class CertChanged(RuntimeError):
    pass


def fetch_fingerprint(host, timeout=10):
    """SHA-256 of the console's TLS certificate. Only a TLS handshake, nothing is sent."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, 443), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                return hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
    except OSError as e:
        raise RuntimeError("cannot reach the UniFi console at %s: %s" % (host, e)) from None


class UniFi:
    """
    UniFi Network API client. With VERIFY_TLS off (self-signed console), the
    certificate must match `pin` before the API key is sent.
    """

    def __init__(self, host, api_key, site, verify_tls, pin=None):
        self.host = host
        self.site = site
        self.verify_tls = verify_tls
        self.pin = (pin or "").lower() or None
        self.fingerprint = None
        self.headers = {
            "X-API-Key": api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self.ctx = ssl.create_default_context()
        if not verify_tls:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        conn = http.client.HTTPSConnection(self.host, 443, context=self.ctx, timeout=20)
        try:
            conn.connect()
            self.fingerprint = hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()
            if not self.verify_tls and self.pin and self.fingerprint != self.pin:
                raise CertChanged(
                    "the UniFi console certificate has changed (expected %s..., got %s...). "
                    "If you replaced or reset the console, enter the API key again in "
                    "Proxmox > Datacenter > UniFi DNS > Setup and save." % (self.pin[:16], self.fingerprint[:16]))
            conn.request(method, path, body=data, headers=self.headers)
            resp = conn.getresponse()
            raw = resp.read()
        except CertChanged:
            raise
        except (OSError, http.client.HTTPException) as e:
            raise RuntimeError("UniFi %s %s -> %s" % (method, path, e)) from None
        finally:
            conn.close()
        if resp.status >= 400:
            raise RuntimeError("UniFi %s %s -> HTTP %s: %s"
                               % (method, path, resp.status, raw.decode(errors="replace")[:300]))
        return json.loads(raw) if raw else None

    def _dns(self, suffix=""):
        return "/proxy/network/v2/api/site/%s/static-dns%s" % (self.site, suffix)

    def dns_list(self):
        return self._req("GET", self._dns()) or []

    def dns_create(self, rtype, name, value):
        body = {"record_type": rtype, "key": name, "value": value,
                "enabled": True, "ttl": 0}
        return self._req("POST", self._dns(), body)

    def dns_update(self, rec, value):
        body = dict(rec)
        body["value"] = value
        body["enabled"] = True
        return self._req("PUT", self._dns("/" + rec["_id"]), body)

    def dns_delete(self, rec_id):
        return self._req("DELETE", self._dns("/" + rec_id))

    def clients_by_mac(self):
        resp = self._req("GET", "/proxy/network/api/s/%s/stat/sta" % self.site) or {}
        out = {}
        for c in resp.get("data", []):
            mac = (c.get("mac") or "").lower()
            ip = c.get("ip")
            if mac and ip:
                out[mac] = ip
        return out

    # client list (known clients, online or not) ---------------------------
    def known_clients(self):
        resp = self._req("GET", "/proxy/network/api/s/%s/rest/user" % self.site) or {}
        return {(c.get("mac") or "").lower(): c for c in resp.get("data", []) if c.get("mac")}

    def set_client_name(self, client_id, name):
        return self._req("PUT", "/proxy/network/api/s/%s/rest/user/%s" % (self.site, client_id),
                         {"name": name})

    def set_client_icon(self, mac, dev_id):
        return self._req("PUT", "/proxy/network/v2/api/site/%s/station/%s/fingerprint_override"
                         % (self.site, mac), {"mac": mac, "dev_id_override": int(dev_id)})

    def fingerprint_devices(self):
        """UniFi's device catalogue: {id: name}."""
        data = self._req("GET", "/proxy/network/v2/api/fingerprint_devices/0") or {}
        return {k: v.get("name", "") for k, v in (data.get("dev_ids") or {}).items()}


def connect(conf):
    """UniFi client for the saved settings. Pins the console certificate on first contact."""
    pin = conf.get("UNIFI_CERT_SHA256") or None
    if not conf["VERIFY_TLS"] and not pin:
        pin = fetch_fingerprint(conf["UNIFI_HOST"])
        save_conf({"UNIFI_CERT_SHA256": pin})
        log("UniFi console certificate pinned: %s", pin)
    return UniFi(conf["UNIFI_HOST"], conf["UNIFI_API_KEY"], conf["UNIFI_SITE"], conf["VERIFY_TLS"], pin)

def fingerprints(conf, uni, max_age=86400):
    """Catalogue {id: name}, cached on disk for a day."""
    path = conf["FINGERPRINT_CACHE"]
    try:
        if os.path.exists(path) and os.path.getmtime(path) > __import__("time").time() - max_age:
            with open(path) as fh:
                return json.load(fh)
    except (OSError, ValueError):
        pass
    cat = uni.fingerprint_devices()
    write_json(path, cat)
    return cat


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=1)
    os.replace(tmp, path)


def read_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


# --------------------------------------------------------------- proxmox ----

def pvesh(verb, path, *extra):
    cmd = ["pvesh", verb, path] + list(extra)
    if verb == "get":
        cmd += ["--output-format", "json"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError("pvesh %s %s failed: %s" % (verb, path, res.stderr.strip()))
    return json.loads(res.stdout) if verb == "get" and res.stdout.strip() else None


MAC_RE = re.compile(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")


def guest_inventory():
    """One dict per guest: vmid, name, type, node, status, tags, mac, static_ip, description."""
    guests = []
    for r in pvesh("get", "/cluster/resources", "--type", "vm"):
        gtype = r.get("type")          # "qemu" or "lxc"
        if gtype not in ("qemu", "lxc") or r.get("template"):
            continue
        g = {
            "vmid": r["vmid"], "name": r.get("name") or str(r["vmid"]),
            "type": gtype, "node": r["node"], "status": r.get("status"),
            "tags": [t for t in (r.get("tags") or "").split(";") if t],
            "mac": None, "static_ip": None, "description": "",
        }
        cfg = pvesh("get", "/nodes/%s/%s/%s/config" % (g["node"], gtype, g["vmid"]))
        g["description"] = cfg.get("description") or ""
        net0 = cfg.get("net0", "")
        m = MAC_RE.search(net0)
        if m:
            g["mac"] = m.group(1).lower()
        if gtype == "lxc":
            m = re.search(r"(?:^|,)ip=(\d+\.\d+\.\d+\.\d+)/\d+", net0)
            if m:
                g["static_ip"] = m.group(1)
        guests.append(g)
    return guests


def ip_from_guest_agent(g):
    if g["type"] != "qemu" or g["status"] != "running":
        return None
    try:
        data = pvesh("get", "/nodes/%s/qemu/%s/agent/network-get-interfaces" % (g["node"], g["vmid"]))
    except RuntimeError:
        return None                    # no agent / not responding
    ifaces = data.get("result", data) if isinstance(data, dict) else data
    for iface in ifaces or []:
        mac = (iface.get("hardware-address") or "").lower()
        if g["mac"] and mac != g["mac"]:
            continue
        for addr in iface.get("ip-addresses", []):
            if addr.get("ip-address-type") == "ipv4" and not addr["ip-address"].startswith("127."):
                return addr["ip-address"]
    return None


def ip_from_lxc_interfaces(g):
    """LXC only: Proxmox reports the container's live addresses via /interfaces."""
    if g["type"] != "lxc" or g["status"] != "running":
        return None
    try:
        data = pvesh("get", "/nodes/%s/lxc/%s/interfaces" % (g["node"], g["vmid"]))
    except RuntimeError:
        return None
    for iface in data or []:
        if iface.get("name") == "lo":
            continue
        if g["mac"] and (iface.get("hwaddr") or "").lower() != g["mac"]:
            continue
        inet = iface.get("inet") or ""
        if inet:
            return inet.split("/")[0]
    return None


def node_inventory():
    """Online Proxmox nodes with the IPv4 of their default-route interface."""
    nodes = []
    try:
        res = pvesh("get", "/cluster/resources", "--type", "node")
    except RuntimeError:
        return nodes
    for r in res or []:
        if r.get("status") != "online":
            continue
        name = r["node"]
        ip = None
        try:
            nets = pvesh("get", "/nodes/%s/network" % name) or []
        except RuntimeError:
            nets = []
        with_addr = [n for n in nets if n.get("address")]
        pick = [n for n in with_addr if n.get("gateway")] or with_addr
        if pick:
            ip = pick[0]["address"].split("/")[0]
        nodes.append({"vmid": "node", "name": name, "type": "node", "node": name,
                      "status": "online", "tags": [], "mac": None,
                      "static_ip": ip, "description": ""})
    return nodes


def set_note(g, text, dry_run):
    """Replace / add our single status line in the guest's Notes, keep the rest."""
    old = g["description"]
    kept = [ln for ln in old.splitlines() if not ln.startswith(NOTE_PREFIX)]
    line = "%s %s" % (NOTE_PREFIX, text)
    new = "\n".join(kept + [line]).strip("\n") if kept else line
    if new == old.strip("\n"):
        return False
    if not dry_run:
        pvesh("set", "/nodes/%s/%s/%s/config" % (g["node"], g["type"], g["vmid"]),
              "--description", new)
    g["description"] = new
    return True


# -------------------------------------------------------------- planning ----

def by_name(records):
    """{name: [records]}. UniFi accepts several records with the same name."""
    out = {}
    for r in records:
        out.setdefault(r.get("key", "").lower(), []).append(r)
    return out

def dns_label(name):
    label = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return label[:63] or None


def plan(guests, conf, uni):
    """
    Decide what each guest should publish.
    Returns (desired, rows):
      desired: fqdn -> {"type": "A"|"CNAME", "value": ip-or-target, "vmid": n}
      rows:    per-guest status for --status / Notes
    """
    domain = conf["DOMAIN"].strip(".").lower()
    desired, rows = {}, []
    unifi_clients = None

    if conf["PUBLISH_NODES"]:
        for n in node_inventory():
            row = {"g": n, "fqdn": None, "ip": None, "source": None, "aliases": [], "note": None}
            rows.append(row)
            label = dns_label(n["name"])
            if not label or not n["static_ip"]:
                row["note"] = "node IP not found"
                continue
            fqdn = "%s.%s" % (label, domain)
            desired[fqdn] = {"type": "A", "value": n["static_ip"], "vmid": "node:" + n["name"]}
            row.update(fqdn=fqdn, ip=n["static_ip"], source="node")

    for g in guests:
        tags = g["tags"]
        row = {"g": g, "fqdn": None, "ip": None, "source": None, "aliases": [], "note": None}
        rows.append(row)

        if "nodns" in tags:
            row["note"] = "skipped (nodns tag)"
            continue
        if conf["ONLY_TAG"] and conf["ONLY_TAG"] not in tags:
            row["note"] = "skipped (no '%s' tag)" % conf["ONLY_TAG"]
            continue
        if g["status"] != "running" and not conf["INCLUDE_STOPPED"]:
            row["note"] = "not running"
            continue

        # name: dns-<name> tag wins over the VM name
        label = None
        for t in tags:
            if t.startswith("dns-") and not t.startswith("dns-alias-"):
                label = dns_label(t[4:])
        label = label or dns_label(g["name"])
        if not label:
            row["note"] = "no usable name"
            continue
        fqdn = "%s.%s" % (label, domain)

        # ip: static > guest agent > UniFi client table
        ip, source = g["static_ip"], "static"
        if not ip:
            ip, source = ip_from_guest_agent(g), "agent"
        if not ip:
            ip, source = ip_from_lxc_interfaces(g), "lxc"
        if not ip and g["mac"]:
            if unifi_clients is None:
                unifi_clients = uni.clients_by_mac()
            ip, source = unifi_clients.get(g["mac"]), "unifi"
        if not ip:
            row["note"] = "no IP known yet"
            continue
        if fqdn in desired:
            row["note"] = "name clash with vmid %s" % desired[fqdn]["vmid"]
            continue

        desired[fqdn] = {"type": "A", "value": ip, "vmid": g["vmid"]}
        row.update(fqdn=fqdn, ip=ip, source=source)

        for t in tags:
            if t.startswith("dns-alias-"):
                alias = dns_label(t[len("dns-alias-"):])
                if not alias:
                    continue
                afqdn = "%s.%s" % (alias, domain)
                if afqdn in desired:
                    log("warning: alias %s on vmid %s clashes, skipped", afqdn, g["vmid"])
                    continue
                desired[afqdn] = {"type": "CNAME", "value": fqdn, "vmid": g["vmid"]}
                row["aliases"].append(afqdn)

    return desired, rows


def note_text(row):
    if row["note"]:
        return row["note"]
    s = "%s -> %s (%s)" % (row["fqdn"], row["ip"], row["source"])
    if row["aliases"]:
        s += ", aliases: " + ", ".join(a.split(".")[0] for a in row["aliases"])
    return s


def print_status(rows, domain):
    hdr = ("VMID", "NAME", "STATE", "RECORD", "IP", "VIA", "NOTE")
    data = []
    for r in rows:
        g = r["g"]
        data.append((str(g["vmid"]), g["name"][:22], g["status"] or "",
                     (r["fqdn"] or "").replace("." + domain, ""),
                     r["ip"] or "", r["source"] or "",
                     (r["note"] or (", ".join(a.split(".")[0] for a in r["aliases"]) and
                                    "aliases: " + ", ".join(a.split(".")[0] for a in r["aliases"])) or "")))
    widths = [max(len(x[i]) for x in [hdr] + data) for i in range(len(hdr))]
    for line in [hdr] + data:
        print("  ".join(c.ljust(w) for c, w in zip(line, widths)).rstrip())
    print("\nrecords are published under .%s" % domain)


# ------------------------------------------------------------ manual ops ----

HOST_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
IPV4_RE = re.compile(r"^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}$")


def qualify(name, domain):
    name = name.strip().strip(".").lower()
    return name if "." in name else "%s.%s" % (name, domain)


def manual_change(uni, domain, owned, action, name, value=None, dry_run=False):
    """
    One hand-made record change, shared by the CLI and the web page.
    action: "add" (A), "add-cname", "remove". Returns (rc, message); rc 0 = ok.
    """
    all_recs = uni.dns_list()
    recs = {r["key"].lower(): r for r in all_recs}
    name = qualify(name or "", domain)
    if not HOST_RE.match(name):
        return 2, "'%s' is not a valid DNS name" % name

    if action in ("add", "add-cname"):
        rtype = "A" if action == "add" else "CNAME"
        value = (value or "").strip()
        if rtype == "CNAME":
            value = qualify(value, domain)
            if not HOST_RE.match(value):
                return 2, "'%s' is not a valid DNS name" % value
        elif not IPV4_RE.match(value):
            return 2, "'%s' is not an IPv4 address" % value
        if name in owned:
            return 2, "%s is managed by the sync; change the guest's tags instead" % name
        rec = recs.get(name)
        if rec and rec.get("record_type") != rtype:
            return 2, "%s exists as %s; remove it first" % (name, rec.get("record_type"))
        if rec and rec.get("value") == value and rec.get("enabled", True):
            return 0, "%s already %s -> %s" % (rtype, name, value)
        if rec:
            if not dry_run:
                uni.dns_update(rec, value)
            return 0, "UPDATE %s %s %s -> %s" % (rtype, name, rec.get("value"), value)
        if not dry_run:
            uni.dns_create(rtype, name, value)
        return 0, "ADD %s %s -> %s" % (rtype, name, value)

    if action == "remove":
        if name in owned:
            return 2, "%s is managed by the sync and would come back; tag the guest nodns instead" % name
        rec = recs.get(name)
        if not rec:
            return 1, "no record named %s" % name
        dependents = [r["key"] for r in recs.values()
                      if r.get("record_type") == "CNAME" and r.get("value", "").lower() == name]
        if dependents:
            return 2, "%s is the target of CNAME(s): %s. Remove those first." % (name, ", ".join(dependents))
        copies = [r for r in all_recs if r.get("key", "").lower() == name]
        if not dry_run:
            for r in copies:
                uni.dns_delete(r["_id"])
        return 0, "REMOVE %s %s (%s)%s" % (rec.get("record_type"), name, rec.get("value"),
                                           ", %d copies" % len(copies) if len(copies) > 1 else "")

    return 2, "unknown action %s" % action


def manage(args, uni, domain, owned):
    """--list / --add / --add-cname / --remove. Returns an exit code."""
    if args.list:
        rows = sorted(uni.dns_list(), key=lambda r: (r.get("record_type", ""), r["key"].lower()))
        w = max([len(r["key"]) for r in rows] + [4])
        print("  %-5s %-*s  %s" % ("TYPE", w, "NAME", "VALUE"))
        for r in rows:
            mark = "*" if r["key"].lower() in owned else " "
            off = "" if r.get("enabled", True) else "  (disabled)"
            print("%s %-5s %-*s  %s%s" % (mark, r.get("record_type", "?"), w, r["key"], r.get("value", ""), off))
        print("\n* = created by the sync (%d of %d)" % (sum(1 for r in rows if r["key"].lower() in owned), len(rows)))
        return 0

    if args.add:
        rc, msg = manual_change(uni, domain, owned, "add", args.add[0], args.add[1], args.dry_run)
    elif args.add_cname:
        rc, msg = manual_change(uni, domain, owned, "add-cname", args.add_cname[0], args.add_cname[1], args.dry_run)
    elif args.remove:
        rc, msg = manual_change(uni, domain, owned, "remove", args.remove, None, args.dry_run)
    else:
        return None
    log(msg)
    return rc


# -------------------------------------------------------- client entries ----

def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def auto_icon(name, catalogue):
    """Catalogue id whose name equals the guest name, or one of its words (5+ chars)."""
    index = {}
    for dev_id, dev_name in catalogue.items():
        index.setdefault(_norm(dev_name), dev_id)
    whole = _norm(name)
    if whole in index:
        return index[whole]
    for word in re.split(r"[^a-z0-9]+", (name or "").lower()):
        if len(word) >= 5 and word in index:
            return index[word]
    return None


def sync_clients(rows, conf, uni, dry_run):
    """
    Push guest name and icon into UniFi's client list. Fills row["client"] for
    the status file. Returns the number of changes.
    """
    if not (conf["SYNC_CLIENT_NAMES"] or conf["SYNC_CLIENT_ICONS"]):
        return 0
    clients = uni.known_clients()
    catalogue = fingerprints(conf, uni) if conf["SYNC_CLIENT_ICONS"] else {}
    chosen = {str(k): str(v) for k, v in read_json(conf["ICON_FILE"], {}).items()}
    default = str(conf["DEFAULT_ICON"]).strip()
    changes = 0

    for row in rows:
        g = row["g"]
        if g["type"] == "node" or not g["mac"]:
            continue
        c = clients.get(g["mac"])
        info = {"in_unifi": c is not None, "unifi_name": c and c.get("name"),
                "icon_id": None, "icon_name": None, "icon_source": None}
        row["client"] = info
        if c is None:
            continue

        if conf["SYNC_CLIENT_NAMES"] and c.get("name") != g["name"]:
            log("NAME   %-28s %s -> %s", g["mac"], c.get("name") or "(none)", g["name"])
            if not dry_run:
                uni.set_client_name(c["_id"], g["name"])
            info["unifi_name"] = g["name"]
            changes += 1

        if conf["SYNC_CLIENT_ICONS"]:
            want, src = chosen.get(str(g["vmid"])), "chosen"
            if not want:
                want, src = auto_icon(g["name"], catalogue), "matched"
            if not want and default:
                want, src = default, "default"
            if want:
                info.update(icon_id=int(want), icon_name=catalogue.get(str(want)), icon_source=src)
                if str(c.get("dev_id_override") or "") != str(want):
                    log("ICON   %-28s %s -> %s (%s, %s)", g["name"], c.get("dev_id_override") or "auto",
                        want, catalogue.get(str(want), "?"), src)
                    if not dry_run:
                        uni.set_client_icon(g["mac"], want)
                    changes += 1
    return changes


def write_status(conf, rows, domain, records, owned):
    out = {"time": int(__import__("time").time()), "domain": domain, "guests": [], "nodes": []}
    for r in rows:
        g = r["g"]
        item = {"vmid": g["vmid"], "name": g["name"], "type": g["type"], "node": g["node"],
                "status": g["status"], "tags": g["tags"], "mac": g["mac"],
                "record": r["fqdn"], "ip": r["ip"], "source": r["source"],
                "aliases": r["aliases"], "note": r["note"], "client": r.get("client")}
        (out["nodes"] if g["type"] == "node" else out["guests"]).append(item)
    out["owned"] = sorted(owned)
    out["records"] = len(records)
    write_json(conf["STATUS_FILE"], out)


def purge(uni, owned, conf, dry_run):
    """Remove everything the sync created: its DNS records and its Notes lines."""
    current = by_name(uni.dns_list())
    removed = 0
    first = lambda k: current.get(k, [{}])[0]
    for fqdn in sorted(owned, key=lambda k: (first(k).get("record_type") != "CNAME", k)):
        for rec in current.get(fqdn, []):
            log("REMOVE %-6s %s (%s)", rec.get("record_type"), fqdn, rec.get("value"))
            if not dry_run:
                uni.dns_delete(rec["_id"])
            removed += 1
    notes = 0
    for g in guest_inventory():
        if not any(ln.startswith(NOTE_PREFIX) for ln in g["description"].splitlines()):
            continue
        kept = "\n".join(ln for ln in g["description"].splitlines() if not ln.startswith(NOTE_PREFIX)).strip("\n")
        if not dry_run:
            path = "/nodes/%s/%s/%s/config" % (g["node"], g["type"], g["vmid"])
            if kept:
                pvesh("set", path, "--description", kept)
            else:
                pvesh("set", path, "--delete", "description")
        notes += 1
    if not dry_run:
        write_json(conf["STATE_FILE"], {"owned": []})
    log("%d record(s) and %d Notes line(s) removed%s", removed, notes, " (dry run)" if dry_run else "")
    return 0


# ------------------------------------------------------------------ main ----

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("-c", "--config", help="config file (default: %s)" % " or ".join(CONF_PATHS))
    ap.add_argument("-n", "--dry-run", action="store_true", help="show changes, write nothing")
    ap.add_argument("-s", "--status", action="store_true", help="print what each guest publishes, change nothing")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--force", action="store_true", help="run even if RUN_ON_NODE names another node")
    grp = ap.add_argument_group("manual records (no sync is run)")
    grp.add_argument("--list", action="store_true", help="show every record UniFi holds")
    grp.add_argument("--add", nargs=2, metavar=("NAME", "IP"), help="add/update an A record")
    grp.add_argument("--add-cname", nargs=2, metavar=("NAME", "TARGET"), help="add/update a CNAME")
    grp.add_argument("--remove", metavar="NAME", help="delete a hand-made record")
    ap.add_argument("--purge", action="store_true",
                    help="uninstall helper: delete every record this node's sync created and its Notes lines")
    args = ap.parse_args()
    manual = args.list or args.add or args.add_cname or args.remove or args.purge

    conf = load_conf(args.config)
    if not conf["UNIFI_API_KEY"]:
        log("not set up yet: open Proxmox > Datacenter > UniFi DNS > Setup (config: %s)",
            conf["_path"] or CONF_PATHS[0])
        return 2 if (manual or args.status) else 0

    me = socket.gethostname().split(".")[0]
    if conf["RUN_ON_NODE"] and conf["RUN_ON_NODE"] != me and not args.force and not (args.status or manual):
        if args.verbose:
            log("RUN_ON_NODE=%s, this is %s: nothing to do", conf["RUN_ON_NODE"], me)
        return 0

    if args.status or args.list:
        return run(args, conf)
    with sync_lock():
        return run(args, conf)


def run(args, conf):
    manual = args.list or args.add or args.add_cname or args.remove or args.purge
    uni = connect(conf)
    domain = conf["DOMAIN"].strip(".").lower()

    state_path = conf["STATE_FILE"]
    owned = set()
    if os.path.exists(state_path):
        with open(state_path) as fh:
            owned = set(json.load(fh).get("owned", []))

    if args.purge:
        return purge(uni, owned, conf, args.dry_run)
    if manual:
        return manage(args, uni, domain, owned)

    guests = guest_inventory()
    desired, rows = plan(guests, conf, uni)

    if args.status:
        print_status(rows, domain)
        return 0

    all_by_name = by_name(r for r in uni.dns_list() if r.get("record_type") in ("A", "CNAME"))
    current = {k: v[0] for k, v in all_by_name.items()}    # key -> first record

    changes = 0

    # extra copies of our own records (left by concurrent runs of older versions)
    for fqdn in sorted(owned & set(all_by_name)):
        for extra in all_by_name[fqdn][1:]:
            log("REMOVE %-6s %-40s (%s, duplicate)", extra.get("record_type"), fqdn, extra.get("value"))
            if not args.dry_run:
                uni.dns_delete(extra["_id"])
            changes += 1
        all_by_name[fqdn] = all_by_name[fqdn][:1]

    # A records first so CNAME targets exist
    order = sorted(desired.items(), key=lambda kv: (kv[1]["type"] != "A", kv[0]))
    for fqdn, want in order:
        rec = current.get(fqdn)
        if rec is None:
            log("ADD    %-6s %-40s -> %s", want["type"], fqdn, want["value"])
            if not args.dry_run:
                uni.dns_create(want["type"], fqdn, want["value"])
            owned.add(fqdn)
            changes += 1
        elif rec.get("record_type") != want["type"]:
            log("skip   %-40s exists as %s in UniFi, wanted %s (fix by hand)",
                fqdn, rec.get("record_type"), want["type"])
        elif rec.get("value") != want["value"] or not rec.get("enabled", True):
            log("UPDATE %-6s %-40s %s -> %s", want["type"], fqdn, rec.get("value"), want["value"])
            if not args.dry_run:
                uni.dns_update(rec, want["value"])
            owned.add(fqdn)
            changes += 1
        else:
            owned.add(fqdn)            # adopt an identical pre-existing record
            if args.verbose:
                log("ok     %-6s %-40s = %s", want["type"], fqdn, want["value"])

    # remove what we own but no longer want; CNAMEs before the A they point at
    stale = sorted(owned - set(desired),
                   key=lambda k: (current.get(k, {}).get("record_type") != "CNAME", k))
    for fqdn in stale:
        for rec in all_by_name.get(fqdn, []):
            log("REMOVE %-6s %-40s (%s)", rec.get("record_type"), fqdn, rec.get("value"))
            if not args.dry_run:
                uni.dns_delete(rec["_id"])
            changes += 1
        owned.discard(fqdn)

    notes = 0
    if conf["WRITE_NOTES"]:
        for row in rows:
            if row["g"]["type"] == "node":
                continue
            if set_note(row["g"], note_text(row), args.dry_run):
                notes += 1

    try:
        client_changes = sync_clients(rows, conf, uni, args.dry_run)
    except RuntimeError as e:            # never let the client list block DNS
        log("client name/icon sync failed: %s", e)
        client_changes = 0

    if not args.dry_run:
        write_json(state_path, {"owned": sorted(owned)})
        write_status(conf, rows, domain, desired, owned)

    log("%d guests, %d records desired, %d DNS change(s), %d note(s), %d client name/icon change(s)%s",
        len(guests), len(desired), changes, notes, client_changes, " (dry run)" if args.dry_run else "")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        log("error: %s", e)
        sys.exit(1)
