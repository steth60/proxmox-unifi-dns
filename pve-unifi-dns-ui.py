#!/usr/bin/env python3
"""
pve-unifi-dns-ui: the "UniFi DNS" page shown inside the Proxmox web UI.

Serves a small JSON API on https://<node>:8009, using the node's
own Proxmox certificate. Every API call is checked against Proxmox itself:
the browser's PVEAuthCookie is sent to the local Proxmox API, and the user
needs Sys.Audit on / to read and Sys.Modify on / to change anything.

Reads and writes go through the same code as pve-unifi-dns.py, so the page
and the CLI have the same guard rails. Stdlib only.
"""

import hashlib
import http.server
import importlib.util
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

SYNC_SCRIPT = os.environ.get("PVE_UNIFI_DNS", "/usr/local/bin/pve-unifi-dns.py")
PORT = int(os.environ.get("UI_PORT", "8009"))  # 8007 belongs to Proxmox Backup Server
PVE_API = "https://127.0.0.1:8006/api2/json"
ICON_URL = "https://static.ui.com/fingerprint/0/%s_257x257.png"

spec = importlib.util.spec_from_file_location("pud", SYNC_SCRIPT)
pud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pud)

_local_ctx = ssl.create_default_context()
_local_ctx.check_hostname = False
_local_ctx.verify_mode = ssl.CERT_NONE

_auth_cache = {}
_auth_lock = threading.Lock()
_write_lock = threading.Lock()


def log(msg, *a):
    print(msg % a if a else msg, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ auth ----

def pve_privileges(cookie):
    """Privileges on / for the Proxmox session behind this cookie, or None."""
    key = hashlib.sha256(cookie.encode()).hexdigest()
    now = time.time()
    with _auth_lock:
        hit = _auth_cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    req = urllib.request.Request(PVE_API + "/access/permissions?path=%2F",
                                 headers={"Cookie": "PVEAuthCookie=" + cookie})
    try:
        with urllib.request.urlopen(req, context=_local_ctx, timeout=10) as r:
            data = json.loads(r.read()).get("data") or {}
    except (urllib.error.URLError, OSError, ValueError):
        return None
    privs = data.get("/", data) if isinstance(data, dict) else {}
    with _auth_lock:
        _auth_cache[key] = (now + 30, privs)
        for k in [k for k, v in _auth_cache.items() if v[0] < now]:
            _auth_cache.pop(k, None)
    return privs


# ----------------------------------------------------------------- state ----

class NotConfigured(RuntimeError):
    pass


def context():
    conf = pud.load_conf()
    if not conf["UNIFI_API_KEY"]:
        raise NotConfigured("not set up yet")
    uni = pud.connect(conf)
    domain = conf["DOMAIN"].strip(".").lower()
    owned = set(pud.read_json(conf["STATE_FILE"], {}).get("owned", []))
    return conf, uni, domain, owned


def sync_running():
    r = subprocess.run(["systemctl", "is-active", "pve-unifi-dns.service"],
                       capture_output=True, text=True)
    return r.stdout.strip() in ("activating", "active", "reloading")


def start_sync():
    subprocess.run(["systemctl", "start", "--no-block", "pve-unifi-dns.service"],
                   capture_output=True, text=True)


def api_state():
    try:
        conf, uni, domain, owned = context()
    except NotConfigured:
        return {"configured": False}
    status = pud.read_json(conf["STATUS_FILE"], {})
    chosen = {str(k): int(v) for k, v in pud.read_json(conf["ICON_FILE"], {}).items()}
    records = []
    for r in uni.dns_list():
        key = r.get("key", "").lower()
        records.append({"type": r.get("record_type"), "name": key, "value": r.get("value"),
                        "enabled": r.get("enabled", True), "owned": key in owned})
    records.sort(key=lambda r: (not r["owned"], r["type"] or "", r["name"]))
    return {"configured": True, "domain": domain, "time": status.get("time"), "sync_running": sync_running(),
            "guests": status.get("guests", []), "nodes": status.get("nodes", []),
            "records": records, "chosen": chosen, "icon_url": ICON_URL,
            "default_icon": conf["DEFAULT_ICON"], "names": conf["SYNC_CLIENT_NAMES"],
            "icons": conf["SYNC_CLIENT_ICONS"]}


def api_icons(query):
    conf, uni, _, _ = context()
    cat = pud.fingerprints(conf, uni)
    q = pud._norm(query)
    if not q:
        return []
    hits = [(k, v) for k, v in cat.items() if q in pud._norm(v)]
    hits.sort(key=lambda kv: (not pud._norm(kv[1]).startswith(q), len(kv[1]), kv[1]))
    return [{"id": int(k), "name": v} for k, v in hits[:60]]


def api_record(body):
    action = body.get("action")
    if action not in ("add", "add-cname", "remove"):
        return 400, {"ok": False, "message": "unknown action"}
    conf, uni, domain, owned = context()
    with _write_lock, pud.sync_lock(wait=60):
        owned = set(pud.read_json(conf["STATE_FILE"], {}).get("owned", []))
        rc, msg = pud.manual_change(uni, domain, owned, action,
                                    str(body.get("name") or ""), str(body.get("value") or ""))
    log("record %s: %s", action, msg)
    return (200 if rc == 0 else 400), {"ok": rc == 0, "message": msg}


def api_icon(body):
    try:
        vmid = int(body.get("vmid"))
    except (TypeError, ValueError):
        return 400, {"ok": False, "message": "vmid missing"}
    icon = body.get("icon")
    conf, uni, _, _ = context()
    status = pud.read_json(conf["STATUS_FILE"], {})
    guest = next((g for g in status.get("guests", []) if g.get("vmid") == vmid), None)
    if guest is None:
        return 404, {"ok": False, "message": "no guest %s in the last sync" % vmid}
    with _write_lock:
        chosen = {str(k): v for k, v in pud.read_json(conf["ICON_FILE"], {}).items()}
        if icon in (None, "", "auto"):
            chosen.pop(str(vmid), None)
            msg = "%s: icon back to automatic" % guest["name"]
        else:
            try:
                icon = int(icon)
            except (TypeError, ValueError):
                return 400, {"ok": False, "message": "icon must be a number"}
            if str(icon) not in pud.fingerprints(conf, uni):
                return 400, {"ok": False, "message": "unknown icon %s" % icon}
            chosen[str(vmid)] = icon
            if guest.get("mac") and (guest.get("client") or {}).get("in_unifi"):
                uni.set_client_icon(guest["mac"], icon)
            msg = "%s: icon set" % guest["name"]
        pud.write_json(conf["ICON_FILE"], chosen)
    log(msg)
    start_sync()
    return 200, {"ok": True, "message": msg}


# ----------------------------------------------------------------- setup ----

NODE = os.uname().nodename.split(".")[0]
BOOL_SETTINGS = ("VERIFY_TLS", "INCLUDE_STOPPED", "WRITE_NOTES", "PUBLISH_NODES",
                 "SYNC_CLIENT_NAMES", "SYNC_CLIENT_ICONS")
SETTING_RULES = {
    "UNIFI_HOST": lambda v: bool(pud.HOST_RE.match(v) or pud.IPV4_RE.match(v)),
    "UNIFI_SITE": lambda v: bool(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", v)),
    "DOMAIN": lambda v: bool(pud.HOST_RE.match(v)),
    "ONLY_TAG": lambda v: bool(re.fullmatch(r"[a-z0-9_.+-]{0,64}", v)),
    "DEFAULT_ICON": lambda v: bool(re.fullmatch(r"\d{0,7}", v)),
    "RUN_ON_NODE": lambda v: bool(re.fullmatch(r"[A-Za-z0-9-]{0,63}", v)),
    "UNIFI_API_KEY": lambda v: bool(re.fullmatch(r"[A-Za-z0-9._~+/=-]{8,256}", v)),
    "UNIFI_CERT_SHA256": lambda v: bool(re.fullmatch(r"[0-9a-f]{64}|", v)),
}


def run(cmd, timeout=60):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def is_cluster():
    return os.path.exists("/etc/pve/corosync.conf")


def api_config_get():
    conf = pud.load_conf()
    out = {k: conf[k] for k in pud.SETTING_KEYS if k != "UNIFI_API_KEY"}
    out["UNIFI_API_KEY_SET"] = bool(conf["UNIFI_API_KEY"])
    out["path"] = conf["_path"] or pud.CONF_PATHS[0]
    out["node"] = NODE
    out["cluster"] = is_cluster()
    return out


def clean_settings(body, require_key):
    """Validate the posted settings. Returns (updates, error)."""
    updates = {}
    for k in pud.SETTING_KEYS:
        if k not in body:
            continue
        v = body[k]
        if k in BOOL_SETTINGS:
            updates[k] = "true" if v in (True, "true", "on", 1, "1") else "false"
            continue
        v = str(v if v is not None else "").strip()
        if k in ("UNIFI_HOST", "DOMAIN"):
            v = v.lower().strip(".")
        if k == "UNIFI_API_KEY" and not v:
            continue                                   # blank = keep the saved key
        if "\n" in v or "\r" in v or not SETTING_RULES.get(k, lambda _: True)(v):
            return None, "invalid value for %s" % k
        updates[k] = v
    if require_key and "UNIFI_API_KEY" not in updates and not pud.load_conf()["UNIFI_API_KEY"]:
        return None, "the UniFi API key is required"
    for k in ("UNIFI_HOST", "DOMAIN"):
        if k in body and not updates.get(k):
            return None, "%s is required" % k
    return updates, None


def api_test(body):
    """
    Test the console. The saved key is only sent to the saved console with the
    pinned certificate; anything else needs the key typed in again.
    """
    updates, err = clean_settings(body, require_key=False)
    if err:
        return 400, {"ok": False, "message": err}
    conf = pud.load_conf()
    host = updates.get("UNIFI_HOST", conf["UNIFI_HOST"])
    site = updates.get("UNIFI_SITE", conf["UNIFI_SITE"])
    verify = updates["VERIFY_TLS"] == "true" if "VERIFY_TLS" in updates else conf["VERIFY_TLS"]
    key = updates.get("UNIFI_API_KEY")
    pin = None
    if not key:
        if not conf["UNIFI_API_KEY"]:
            return 200, {"ok": False, "message": "Enter the API key"}
        if host != conf["UNIFI_HOST"] or verify != conf["VERIFY_TLS"]:
            return 200, {"ok": False, "message": "Enter the API key again to test a different console or certificate setting"}
        key = conf["UNIFI_API_KEY"]
        pin = conf["UNIFI_CERT_SHA256"] or None
        if not verify and not pin:
            pin = pud.fetch_fingerprint(host)
    uni = pud.UniFi(host, key, site, verify, pin)
    try:
        info = uni._req("GET", "/proxy/network/integration/v1/info") or {}
        records = uni.dns_list()
    except pud.CertChanged:
        return 200, {"ok": False, "message": "The console certificate has changed. Enter the API key again to trust the new certificate."}
    except RuntimeError as e:
        msg = str(e)
        if "HTTP 401" in msg or "HTTP 403" in msg:
            msg = "UniFi refused the API key"
        elif "HTTP 404" in msg:
            msg = "UniFi Network not found at %s (or site '%s' does not exist)" % (host, site)
        return 200, {"ok": False, "message": msg}
    return 200, {"ok": True,
                 "message": "Connected to UniFi Network %s, %d local DNS records" % (
                     info.get("applicationVersion", "?"), len(records)),
                 "fingerprint": uni.fingerprint or ""}


def api_config_save(body):
    updates, err = clean_settings(body, require_key=True)
    if err:
        return 400, {"ok": False, "message": err}
    conf = pud.load_conf()
    key_given = "UNIFI_API_KEY" in updates
    new_host = updates.get("UNIFI_HOST", conf["UNIFI_HOST"])
    new_verify = updates["VERIFY_TLS"] == "true" if "VERIFY_TLS" in updates else conf["VERIFY_TLS"]
    if conf["UNIFI_API_KEY"] and not key_given and (new_host != conf["UNIFI_HOST"] or new_verify != conf["VERIFY_TLS"]):
        return 400, {"ok": False, "message": "Enter the API key again when changing the console address or certificate setting"}
    if key_given:
        # pin the certificate before the new key is ever used
        updates["UNIFI_CERT_SHA256"] = updates.get("UNIFI_CERT_SHA256") or pud.fetch_fingerprint(new_host)
    else:
        updates.pop("UNIFI_CERT_SHA256", None)      # the pin only changes together with the key
    with _write_lock:
        path = pud.save_conf(updates)
    log("settings saved to %s (%s)", path, ", ".join(sorted(k for k in updates if k != "UNIFI_API_KEY"))
        + (", API key" if key_given else ""))
    code, _ = run(["systemctl", "is-enabled", "pve-unifi-dns.timer"])
    if code != 0:
        run(["systemctl", "enable", "--now", "pve-unifi-dns.timer"])
    start_sync()
    return 200, {"ok": True, "message": "Settings saved, sync started"}


def api_node():
    lldp_installed = run(["dpkg-query", "-W", "-f=${Status}", "lldpd"])[1].endswith("installed")
    return {
        "node": NODE,
        "cluster": is_cluster(),
        "lldpd": {"installed": lldp_installed,
                  "active": run(["systemctl", "is-active", "lldpd"])[1] == "active"},
        "timer": {"enabled": run(["systemctl", "is-enabled", "pve-unifi-dns.timer"])[1] == "enabled",
                  "active": run(["systemctl", "is-active", "pve-unifi-dns.timer"])[1] == "active"},
    }


def api_node_lldpd():
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    with _write_lock:
        r = subprocess.run(["apt-get", "install", "-y", "-q", "lldpd"], capture_output=True, text=True,
                           timeout=600, env=env)
        if r.returncode != 0:
            subprocess.run(["apt-get", "update", "-q"], capture_output=True, timeout=600, env=env)
            r = subprocess.run(["apt-get", "install", "-y", "-q", "lldpd"], capture_output=True, text=True,
                               timeout=600, env=env)
    if r.returncode != 0:
        return 500, {"ok": False, "message": "apt-get install lldpd failed: " + (r.stderr or r.stdout)[-300:]}
    log("lldpd installed")
    return 200, {"ok": True, "message": "lldpd installed. UniFi shows the guests under this host within a few minutes."}


def api_node_timer(body):
    on = body.get("enabled") in (True, "true", 1, "1")
    code, out = run(["systemctl", "enable" if on else "disable", "--now", "pve-unifi-dns.timer"])
    if code != 0:
        return 500, {"ok": False, "message": out[-300:]}
    log("timer %s", "enabled" if on else "disabled")
    return 200, {"ok": True, "message": "Automatic sync %s" % ("on" if on else "off")}


# ---------------------------------------------------------------- server ----

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "pve-unifi-dns-ui"
    sys_version = ""
    timeout = 30            # covers the TLS handshake too, see Server.get_request

    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/state"):
            log("%s %s", self.address_string(), fmt % args)

    def _allowed_origin(self):
        """The Origin header if it is this node's Proxmox UI (port 8006) or this service."""
        origin = self.headers.get("Origin")
        if not origin:
            return None
        u = urllib.parse.urlparse(origin)
        if u.scheme != "https" or u.hostname is None:
            return False
        if u.hostname.lower() != (self._host().strip("[]").lower()):
            return False
        if (u.port or 443) not in (8006, PORT):
            return False
        return origin

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        origin = self._allowed_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _host(self):
        host = self.headers.get("Host") or ""
        if host.startswith("["):
            return host.rsplit(":", 1)[0] if "]:" in host else host
        return host.split(":")[0]

    def do_OPTIONS(self):
        origin = self._allowed_origin()
        if not origin:
            return self._send(403, {"ok": False, "message": "origin not allowed"})
        self._send(204, b"", "text/plain", {
            "Access-Control-Allow-Methods": "GET, POST",
            "Access-Control-Allow-Headers": "Content-Type, X-UniFi-DNS",
            "Access-Control-Max-Age": "600",
        })

    def _auth(self, need):
        cookies = {}
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                cookies[k] = v
        ticket = cookies.get("PVEAuthCookie")
        if not ticket:
            self._send(401, {"ok": False, "message": "not logged in to Proxmox"})
            return False
        privs = pve_privileges(ticket)
        if privs is None:
            self._send(401, {"ok": False, "message": "Proxmox session expired, log in again"})
            return False
        if not privs.get(need):
            self._send(403, {"ok": False, "message": "needs %s on /" % need})
            return False
        return True

    def _same_origin(self):
        if self.headers.get("X-UniFi-DNS") != "1":
            return False
        return self._allowed_origin() is not False

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 65536:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        try:
            if url.path == "/":
                ui = "https://%s:8006/" % self._host()
                page = ("<!doctype html><meta charset=utf-8><title>UniFi DNS service</title>"
                        "<body style='font:14px sans-serif;padding:24px'>"
                        "<p>UniFi DNS service for Proxmox is running.</p>"
                        "<p>Open <a href='%s'>Proxmox</a>, then Datacenter &gt; UniFi DNS.</p>" % ui).encode()
                return self._send(200, page, "text/html; charset=utf-8",
                                  {"Content-Security-Policy": "default-src 'none'"})
            if url.path == "/api/state":
                if self._auth("Sys.Audit"):
                    self._send(200, api_state())
                return
            if url.path == "/api/config":
                if self._auth("Sys.Audit"):
                    self._send(200, api_config_get())
                return
            if url.path == "/api/node":
                if self._auth("Sys.Audit"):
                    self._send(200, api_node())
                return
            if url.path == "/api/icons":
                if self._auth("Sys.Audit"):
                    q = urllib.parse.parse_qs(url.query).get("q", [""])[0]
                    self._send(200, api_icons(q))
                return
            self._send(404, {"ok": False, "message": "not found"})
        except NotConfigured:
            self._send(409, {"ok": False, "message": "not set up yet: use Setup"})
        except RuntimeError as e:
            self._send(502, {"ok": False, "message": str(e)})

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        if not self._same_origin():
            return self._send(403, {"ok": False, "message": "cross-origin request refused"})
        if not self._auth("Sys.Modify"):
            return
        try:
            body = self._body()
            if url.path == "/api/record":
                code, out = api_record(body)
                return self._send(code, out)
            if url.path == "/api/icon":
                code, out = api_icon(body)
                return self._send(code, out)
            if url.path == "/api/config":
                code, out = api_config_save(body)
                return self._send(code, out)
            if url.path == "/api/test":
                code, out = api_test(body)
                return self._send(code, out)
            if url.path == "/api/node/lldpd":
                code, out = api_node_lldpd()
                return self._send(code, out)
            if url.path == "/api/node/timer":
                code, out = api_node_timer(body)
                return self._send(code, out)
            if url.path == "/api/sync":
                start_sync()
                return self._send(200, {"ok": True, "message": "sync started"})
            self._send(404, {"ok": False, "message": "not found"})
        except (ValueError, RuntimeError) as e:
            self._send(400 if isinstance(e, ValueError) else 502, {"ok": False, "message": str(e)})


CERT_PAIRS = (("/etc/pve/local/pveproxy-ssl.pem", "/etc/pve/local/pveproxy-ssl.key"),
              ("/etc/pve/local/pve-ssl.pem", "/etc/pve/local/pve-ssl.key"))


class CertReloader:
    """Uses the same certificate as Proxmox and picks up a new one (ACME renewal) within a minute."""

    def __init__(self):
        self.ctx, self.stamp, self.checked = None, None, 0.0
        self.lock = threading.Lock()

    def current(self):
        now = time.time()
        with self.lock:
            if self.ctx is not None and now - self.checked < 60:
                return self.ctx
            self.checked = now
            for cert, key in CERT_PAIRS:
                if os.path.exists(cert) and os.path.exists(key):
                    stamp = (cert, os.path.getmtime(cert), os.path.getmtime(key))
                    if stamp != self.stamp:
                        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
                        ctx.load_cert_chain(cert, key)
                        self.ctx, self.stamp = ctx, stamp
                        log("using certificate %s", cert)
                    return self.ctx
            if self.ctx is None:
                raise SystemExit("no Proxmox certificate found in /etc/pve/local")
            return self.ctx


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    certs = CertReloader()

    def get_request(self):
        sock, addr = self.socket.accept()
        # handshake happens in the request thread, so a slow client cannot block accept()
        return self.certs.current().wrap_socket(sock, server_side=True,
                                                do_handshake_on_connect=False), addr

    def handle_error(self, request, client_address):
        err = sys.exc_info()[1]
        if isinstance(err, (ssl.SSLError, ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def main():
    srv = Server(("0.0.0.0", PORT), Handler)
    srv.certs.current()
    log("pve-unifi-dns-ui listening on https://0.0.0.0:%d", PORT)
    srv.serve_forever()


if __name__ == "__main__":
    main()
