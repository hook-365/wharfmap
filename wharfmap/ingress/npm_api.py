"""Nginx Proxy Manager / NPMPlus via the REST API: the file reader plus enabled flags, access-list
names and rules, certificate provider and expiry, redirection hosts and TCP/UDP streams.

Credentials come from environment variables only (never from wharfmap.toml):
  NPM_TOKEN                      a pre-issued API token, or
  NPM_EMAIL + NPM_PASSWORD       to log in (vanilla NPM returns a JSON token, NPMPlus sets a cookie;
                                 both are handled)
Any endpoint that a given NPM flavour does not have is skipped, and a failed login makes the caller
fall back to the file reader, so enabling the API can only add information.
"""
from __future__ import annotations

import datetime
import http.cookiejar
import ipaddress
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

from ..discover import Graph

PRIVATE = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
                                              "127.0.0.0/8", "fc00::/7", "fe80::/10", "::1/128")]


class Api:
    def __init__(self, url: str, verify_tls: bool = True, timeout: int = 10):
        self.url = url.rstrip("/")
        self.timeout = timeout
        ctx = ssl.create_default_context()
        if not verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                                  urllib.request.HTTPCookieProcessor(self.jar))
        self.token = ""

    def _req(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        with self.opener.open(req, timeout=self.timeout) as r:
            return json.loads(r.read() or b"null")

    def login(self) -> bool:
        tok = os.environ.get("NPM_TOKEN")
        if tok:
            self.token = tok
            return True
        email, pw = os.environ.get("NPM_EMAIL"), os.environ.get("NPM_PASSWORD")
        if not (email and pw):
            return False
        resp = self._req("POST", "/api/tokens", {"identity": email, "secret": pw})
        self.token = (resp or {}).get("token", "")
        return bool(self.token) or any("token" in c.name.lower() for c in self.jar)

    def get(self, path: str, expand: str = "") -> list | None:
        """GET a collection; retry without `expand` if this flavour rejects it; None if the endpoint is absent."""
        for q in ([f"?expand={expand}"] if expand else []) + [""]:
            try:
                return self._req("GET", path + q)
            except urllib.error.HTTPError as e:
                if e.code in (400, 404) and q:
                    continue
                if e.code == 404:
                    return None
                raise
        return None


def creds_present() -> bool:
    return bool(os.environ.get("NPM_TOKEN") or (os.environ.get("NPM_EMAIL") and os.environ.get("NPM_PASSWORD")))


def _is_internal(clients: list[dict]) -> bool:
    """An access list is 'internal' when every allowed client range is private."""
    addrs = [c.get("address", "") for c in clients if c.get("directive", "allow") == "allow"]
    if not addrs:
        return False
    for a in addrs:
        try:
            net = ipaddress.ip_network(a, strict=False)
        except ValueError:
            return False
        if not any(net.subnet_of(p) for p in PRIVATE if net.version == p.version):
            return False
    return True


def _cert(c: dict | None) -> dict | None:
    if not c or not c.get("expires_on"):
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    exp = datetime.datetime.fromisoformat(c["expires_on"].replace("Z", "+00:00"))
    days = (exp - now).days
    # Lifetime from the last renewal (modified_on). Let's Encrypt now issues 6-day certs next to 90-day
    # ones, so "expiring soon" has to be relative: warn once under a third of the lifetime remains.
    try:
        issued = datetime.datetime.fromisoformat(c["modified_on"].replace("Z", "+00:00"))
        lifetime = max(1, (exp - issued).days)
    except (KeyError, ValueError, TypeError):
        lifetime = 90
    return {"name": c.get("nice_name") or ",".join(c.get("domain_names") or []), "provider": c.get("provider"),
            "expires_on": exp.date().isoformat(), "days": days, "lifetime": lifetime,
            "warn": days < 0 or days < lifetime / 3}


def collect(g: Graph) -> bool:
    cfg = g.cfg
    api = Api(cfg.ingress_api_url, verify_tls=cfg.ingress_api_verify_tls)
    try:
        if not api.login():
            print("npm-api: no NPM_TOKEN or NPM_EMAIL/NPM_PASSWORD in the environment", file=sys.stderr)
            return False
        hosts = api.get("/api/nginx/proxy-hosts", "certificate,access_list") or []
        certs = {c["id"]: c for c in api.get("/api/nginx/certificates") or []}
        acls = {a["id"]: a for a in api.get("/api/nginx/access-lists", "clients,items") or []}
        streams = api.get("/api/nginx/streams") or []
        redirects = api.get("/api/nginx/redirection-hosts") or []
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"npm-api: {e}", file=sys.stderr)
        return False

    for h in hosts:
        acl_list = h.get("access_lists") or ([h["access_list"]] if h.get("access_list") else [])
        acl = next((acls.get(a["id"], a) for a in acl_list), None)
        if acl is None and h.get("access_list_id"):
            acl = acls.get(h["access_list_id"])
        cert = _cert(h.get("certificate") or certs.get(h.get("certificate_id") or 0))
        ups = [f'{h["forward_host"]}:{h["forward_port"]}']
        ups += [f'{l["forward_host"]}:{l["forward_port"]}' for l in h.get("locations") or [] if l.get("forward_host")]
        auth = bool(acl and acl.get("items")) or (h.get("npmplus_auth_request") not in (None, "", "none"))
        g.ingress.append({
            "file": f"proxy-host:{h['id']}", "id": h["id"], "kind": "proxy",
            "domains": h["domain_names"], "upstreams": ups,
            "internal": bool(acl and _is_internal(acl.get("clients") or [])), "auth": auth,
            "enabled": bool(h.get("enabled", True)),
            "acl": {"name": acl["name"], "clients": [c.get("address") for c in acl.get("clients") or []]} if acl else None,
            "cert": cert, "ssl_forced": bool(h.get("ssl_forced")),
            "error": h.get("npmplus_nginx_err") or ("" if h.get("npmplus_nginx_online", True) else "nginx offline"),
        })
    for r in redirects:
        g.ingress.append({"file": f"redirect:{r['id']}", "id": r["id"], "kind": "redirect",
                          "domains": r["domain_names"], "upstreams": [], "internal": False, "auth": False,
                          "enabled": bool(r.get("enabled", True)), "acl": None,
                          "cert": _cert(certs.get(r.get("certificate_id") or 0)),
                          "redirect_to": r.get("forward_domain_name"), "error": ""})
    for s in streams:
        proto = "/".join(p for p, on in (("tcp", s.get("tcp_forwarding")), ("udp", s.get("udp_forwarding"))) if on) or "tcp"
        g.ingress.append({"file": f"stream:{s['id']}", "id": s["id"], "kind": "stream",
                          "domains": [f":{s['incoming_port']}/{proto}"],
                          "upstreams": [f'{s["forwarding_host"]}:{s["forwarding_port"]}'],
                          "internal": False, "auth": False, "enabled": bool(s.get("enabled", True)),
                          "acl": None, "cert": None, "error": ""})
    return True
