"""Traefik: routers declared as container labels.

  traefik.http.routers.<r>.rule = Host(`a.example.com`) || Host(`b.example.com`)
  traefik.http.routers.<r>.middlewares = lan-only@docker, ...   -> internal if an allowlist is attached

The upstream is the labelled container itself, so targets resolve directly.
Routers from the file provider are invisible here (they are not on any container).
"""
from __future__ import annotations

import re

from ..discover import Graph

HOST_RE = re.compile(r"Host(?:SNI)?\(\s*`([^`]+)`\s*\)")
ROUTER_RE = re.compile(r"traefik\.(?:http|tcp)\.routers\.([^.]+)\.(rule|middlewares|service)$")


def detect(g: Graph) -> bool:
    return any("traefik" in c["image"] for c in g.containers)


def collect(g: Graph) -> None:
    for c in g.containers:
        routers: dict[str, dict] = {}
        for k, v in c["labels"].items():
            m = ROUTER_RE.match(k)
            if m:
                routers.setdefault(m.group(1), {})[m.group(2)] = v
        for r, spec in routers.items():
            domains = HOST_RE.findall(spec.get("rule", ""))
            if not domains:
                continue
            mw = spec.get("middlewares", "")
            g.ingress.append({"file": f"{c['name']}:{r}", "domains": domains, "upstreams": [c["name"]],
                              "internal": bool(re.search(r"allow|whitelist|lan|internal", mw, re.I)),
                              "auth": bool(re.search(r"auth", mw, re.I))})
