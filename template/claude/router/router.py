#!/usr/bin/env python3
"""Local model router for Claude Code: main model on Kimi (K3), subagents on DeepSeek.

Claude Code sends every request to one ANTHROPIC_BASE_URL. Point it here; this router
forwards each request by its "model" field:
  * models matching a route prefix (default "deepseek") -> that route's upstream, with the
    route's own API key (read from the cc-switch provider card of the same name);
  * everything else -> the default upstream (Kimi); if "default_key_from_card" is set, its key
    from that cc-switch card replaces the incoming one (so the card pointing here needs no real key).
Responses (including SSE streams) are relayed byte for byte. No request or response bodies
and no keys are logged; one line per request goes to the log file.

Usage: python3 router.py [--config router.json]
Health check: GET http://127.0.0.1:<port>/__router/health
"""
import argparse
import base64
import copy
import fnmatch
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS = {
    "listen": "127.0.0.1",
    "port": 8240,
    "default_upstream": "https://api.kimi.com/coding",
    "routes": [
        {"prefix": "deepseek", "upstream": "https://api.deepseek.com/anthropic",
         "key_from_card": "DeepSeek", "force_effort": "max"}
    ],
    "default_key_from_card": "",
    "kimi_effort_map": {},
    "ccswitch_db": "~/.cc-switch/cc-switch.db",
    "log": "~/.claude/router/logs/router.log",
    "timeout_s": 900,
    "follow_windows_proxy": False,
}
HOP = {"host", "content-length", "connection", "keep-alive", "proxy-connection",
       "proxy-authorization", "proxy-authenticate", "transfer-encoding", "te", "trailer", "upgrade"}
WINDOWS_POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
WINDOWS_PROXY_QUERY = base64.b64encode(r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$settings = Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
[Console]::WriteLine((ConvertTo-Json -InputObject @{
    enabled = ($settings.ProxyEnable -eq 1)
    server = [string]$settings.ProxyServer
    pac = [string]$settings.AutoConfigURL
    override = [string]$settings.ProxyOverride
} -Compress))
""".encode("utf-16-le")).decode("ascii")
_key_cache = {}
_key_lock = threading.Lock()
WINDOWS_PROXY_TTL = 5
_FAKE_IP_NETWORKS = (ipaddress.ip_network("198.18.0.0/15"), ipaddress.ip_network("2001:2::/48"))
_proxy_cache = None
_proxy_lock = threading.Lock()
_log_lock = threading.Lock()


def default_config_path():
    primary = os.path.join(HERE, "router.json")
    if os.path.exists(primary):
        return primary
    platform_cfg = os.path.join(HERE, "router.windows.json" if os.name == "nt" else "router.wsl.json")
    return platform_cfg if os.path.exists(platform_cfg) else primary


def load_config(path):
    cfg = copy.deepcopy(DEFAULTS)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            cfg.update(json.load(f))
    if not isinstance(cfg["follow_windows_proxy"], bool):
        raise ValueError("follow_windows_proxy must be a boolean")
    return cfg


def _read_windows_proxy_settings():
    """Read manual registry settings without exposing subprocess output on failure."""
    powershell = shutil.which("powershell.exe") or WINDOWS_POWERSHELL
    try:
        result = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", WINDOWS_PROXY_QUERY],
            capture_output=True, check=True, timeout=8,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Windows proxy lookup timed out") from None
    except (OSError, subprocess.CalledProcessError):
        raise RuntimeError("Windows proxy lookup failed") from None
    try:
        value = json.loads(result.stdout.decode("utf-8-sig"))
        if not isinstance(value, dict) or not isinstance(value.get("enabled"), bool):
            raise ValueError
        if not isinstance(value.get("override", ""), str):
            raise ValueError
        return value
    except ValueError:
        raise RuntimeError("Windows proxy returned an invalid endpoint") from None


def _windows_proxy_settings():
    """Cache registry snapshots and lookup failures; one thread refreshes each TTL."""
    global _proxy_cache
    with _proxy_lock:
        if _proxy_cache is None or time.monotonic() - _proxy_cache[2] >= WINDOWS_PROXY_TTL:
            try:
                value, error = _read_windows_proxy_settings(), None
            except RuntimeError as exc:
                value, error = None, str(exc)
            _proxy_cache = (value, error, time.monotonic())
        value, error, _ = _proxy_cache
        if error:
            raise RuntimeError(error)
        return value


def proxy_bypassed(hostname, overrides):
    """Match semicolon-separated host patterns, including IPv6 literals and <local>."""
    host = (hostname or "").strip("[]").lower()
    if not host:
        return False
    for pattern in overrides.split(";"):
        pattern = pattern.strip().strip("[]").lower()
        if pattern == "<local>":
            if "." not in host and ":" not in host:
                return True
        elif pattern and fnmatch.fnmatchcase(host, pattern):
            return True
    return False


def _is_loopback_host(hostname):
    """Recognize loopback literals and localhost without resolving arbitrary hosts."""
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _resolves_to_fake_ip(hostname):
    """Check benchmark ranges in IP literals or any TCP DNS answer."""
    if not hostname:
        return False
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        try:
            addresses = [answer[4][0] for answer in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)]
        except OSError:
            return False
    else:
        addresses = [hostname]
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if any(ip in network for network in _FAKE_IP_NETWORKS):
            return True
    return False


def _probe_proxy_port(hostname, port):
    """Open and immediately close a short TCP probe of the selected loopback port."""
    try:
        socket.create_connection((hostname, port), timeout=0.5).close()
        return True
    except OSError:
        return False


def windows_proxy_for(url):
    """Select a manual or live loopback Fake-IP proxy; never evaluate PAC or WPAD."""
    value = _windows_proxy_settings()
    if value.get("pac"):
        return None
    enabled = value["enabled"]
    if not enabled and (not isinstance(value.get("server"), str) or not value["server"].strip()):
        return None
    try:
        target = urlsplit(url)
        if proxy_bypassed(target.hostname, value.get("override", "")):
            return None
        endpoint = value.get("server")
        if not isinstance(endpoint, str):
            raise ValueError
        endpoint = endpoint.strip()
        if re.match(r"^[a-z][a-z0-9+.-]*\s*=", endpoint, re.I):
            per_scheme = {scheme.strip().lower(): address.strip() for part in endpoint.split(";")
                          if "=" in part for scheme, address in [part.split("=", 1)]}
            endpoint = per_scheme.get(target.scheme.lower(), "")
        if not endpoint:
            return None
        proxy = urlsplit(endpoint if "://" in endpoint else "http://" + endpoint)
        if not proxy.hostname or proxy.port == 0 or proxy.path not in ("", "/") or proxy.query or proxy.fragment:
            raise ValueError
    except ValueError:
        if not enabled:
            return None
        raise RuntimeError("Windows proxy returned an invalid endpoint") from None
    if proxy.scheme.lower() != "http":
        if not enabled:
            return None
        raise RuntimeError("Windows proxy must use the HTTP scheme")
    if proxy.username is not None or proxy.password is not None:
        if not enabled:
            return None
        raise RuntimeError("Windows proxy credentials in the endpoint are unsupported")
    if not enabled and not (
        _is_loopback_host(proxy.hostname)
        and _resolves_to_fake_ip(target.hostname)
        and _probe_proxy_port(proxy.hostname, proxy.port or http.client.HTTP_PORT)
    ):
        return None
    return proxy


def end_to_end_headers(headers):
    """Remove both standard hop headers and fields nominated by Connection (RFC 9110 7.6.1)."""
    pairs = list(headers)
    hop = HOP | {token.strip().lower() for key, value in pairs if key.lower() == "connection"
                 for token in value.split(",")}
    return [(key, value) for key, value in pairs if key.lower() not in hop]


def card_key(cfg, card_name):
    """API key of a cc-switch Claude provider card, read-only, cached for 60 s."""
    db = Path(cfg["ccswitch_db"]).expanduser().resolve()
    cache_id = (os.path.normcase(str(db)), card_name)
    with _key_lock:
        hit = _key_cache.get(cache_id)
        if hit and time.monotonic() - hit[1] < 60:
            return hit[0]
        try:
            con = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=5)
            try:
                rows = con.execute("select settings_config from providers where app_type='claude' and name=? limit 2",
                                   (card_name,)).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            raise RuntimeError("cc-switch database could not be read") from None
        if not rows:
            raise RuntimeError("cc-switch card %r not found" % card_name)
        if len(rows) > 1:
            raise RuntimeError("multiple cc-switch cards named %r" % card_name)
        row = rows[0]
        try:
            env = json.loads(row[0])["env"]
            key = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
        except (ValueError, TypeError, KeyError, AttributeError):
            raise RuntimeError("cc-switch card has invalid settings") from None
        if (not isinstance(key, str) or not key.strip() or key.strip().upper().startswith("PASTE_")
                or key.strip().lower() == "via-router" or "\r" in key or "\n" in key):
            raise RuntimeError("cc-switch card has no usable API key")
        key = key.strip()
        _key_cache[cache_id] = (key, time.monotonic())
        return key


def log_line(cfg, rec):
    path = os.path.abspath(os.path.expanduser(cfg["log"]))
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    with _log_lock:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > 5_000_000:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(line)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"     # stream until close; no chunk re-encoding needed
    server_version = "k3ds-router/1"

    def log_message(self, *args):     # silence default stderr access log
        pass

    def do_GET(self):
        if self.path.startswith("/__router/health"):
            body = json.dumps({"ok": True, "port": self.server.cfg["port"]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.forward(b"")

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0:
                raise ValueError
        except ValueError:
            self.send_error(400, "invalid Content-Length")
            return
        self.forward(self.rfile.read(n) if n else b"")

    do_PUT = do_POST
    do_DELETE = do_GET

    def forward(self, body):
        cfg = self.server.cfg
        t0 = time.time()
        rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "m": self.command,
               "path": self.path.split("?")[0], "transport": "direct"}
        upstream, route, data = cfg["default_upstream"], None, None
        ctype = self.headers.get("Content-Type", "")
        if body and "json" in ctype:
            try:
                data = json.loads(body)
            except ValueError:
                data = None
        model = data.get("model", "") if isinstance(data, dict) else ""
        if not isinstance(model, str):
            self.send_error(400, "model must be a string")
            return
        rec["model"] = model
        for r in cfg["routes"]:
            if model.lower().startswith(r["prefix"]):
                upstream, route = r["upstream"], r
                break
        rec["route"] = route["prefix"] if route else "default"
        headers = dict(end_to_end_headers(self.headers.items()))
        if isinstance(data, dict):
            oc = data.get("output_config") if isinstance(data.get("output_config"), dict) else {}
            rec["effort_in"] = oc.get("effort")
            rec["thinking"] = (data.get("thinking") or {}).get("type") if isinstance(data.get("thinking"), dict) else None
            if route:
                data["model"] = model.replace("[1m]", "").replace("[1M]", "")
                if route.get("force_effort"):
                    oc = dict(oc); oc["effort"] = route["force_effort"]; data["output_config"] = oc
            elif (cfg.get("kimi_effort_map") and isinstance(oc.get("effort"), str)
                  and oc["effort"] in cfg["kimi_effort_map"]):
                oc = dict(oc); oc["effort"] = cfg["kimi_effort_map"][oc["effort"]]; data["output_config"] = oc
            rec["effort_out"] = oc.get("effort")
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        key_card = route.get("key_from_card") if route else cfg.get("default_key_from_card")
        if key_card:
            try:
                key = card_key(cfg, key_card)
            except Exception as e:
                rec.update(status=502, err=str(e)); log_line(cfg, rec)
                self.send_error(502, "router: %s" % e)
                return
            for k in list(headers):
                if k.lower() in ("x-api-key", "authorization"):
                    del headers[k]
            headers["x-api-key"] = key
            headers["Authorization"] = "Bearer " + key
        u = urlsplit(upstream)
        path = u.path.rstrip("/") + self.path
        headers["Host"] = u.netloc
        headers["Content-Length"] = str(len(body))
        conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        conn = None
        sent, side, total = False, "upstream", 0
        try:
            proxy = None
            if cfg["follow_windows_proxy"]:
                absolute_url = f"{u.scheme}://{u.netloc}{path}"
                try:
                    proxy = windows_proxy_for(absolute_url)
                except RuntimeError:
                    rec["proxy_fallback"] = "lookup_failed_or_unsupported"
                rec["transport"] = "windows_proxy" if proxy else "direct"
            if proxy:
                conn = conn_cls(proxy.hostname, proxy.port or http.client.HTTP_PORT, timeout=cfg["timeout_s"])
                if u.scheme == "https":
                    conn.set_tunnel(u.hostname, u.port or http.client.HTTPS_PORT)
                else:
                    path = absolute_url
            else:
                conn = conn_cls(u.netloc, timeout=cfg["timeout_s"])
            conn.request(self.command, path, body=body if body else None, headers=headers)
            resp = conn.getresponse()
            rec["status"] = resp.status
            side = "client"
            self.send_response(resp.status, resp.reason)
            for k, v in end_to_end_headers(resp.getheaders()):
                self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            sent = True
            while True:
                side = "upstream"
                chunk = resp.read1(65536)
                if not chunk:
                    break
                side = "client"
                self.wfile.write(chunk)
                self.wfile.flush()
                total += len(chunk)
        except Exception as e:
            rec.update(status=rec.get("status") or 502, err="%s: %s" % (type(e).__name__, e), err_side=side)
            if not sent:      # after the response has started, an error page would corrupt the stream: just close
                try:
                    self.send_error(502, "router upstream error")
                except Exception:
                    pass
        finally:
            if conn is not None:
                conn.close()
            rec["bytes"] = total
            rec["ms"] = int((time.time() - t0) * 1000)
            log_line(cfg, rec)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=default_config_path())
    a = ap.parse_args()
    cfg = load_config(a.config)
    srv = ThreadingHTTPServer((cfg["listen"], cfg["port"]), Handler)
    srv.daemon_threads = True
    srv.cfg = cfg
    log_line(cfg, {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "event": "start", "port": cfg["port"], "pid": os.getpid()})
    srv.serve_forever()


if __name__ == "__main__":
    main()
