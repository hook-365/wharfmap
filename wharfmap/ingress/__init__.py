"""Reverse-proxy ingress: which public/LAN domain lands on which container.

Each provider fills `g.ingress` with dicts:
  {file, domains[], upstreams["host:port"], internal(bool), auth(bool)}
and this module resolves upstreams to containers and emits `ingress` edges.
"""
from __future__ import annotations

from ..discover import Graph
from . import npm, npm_api, traefik


def collect(g: Graph) -> None:
    """Pick a provider. `auto` is read-only by default; it upgrades to the NPM API only when
    an api_url is configured and credentials are present in the environment."""
    provider = g.cfg.ingress_provider
    if provider == "auto":
        if npm.detect(g) or g.cfg.ingress_api_url:
            provider = "npm-api" if (g.cfg.ingress_api_url and npm_api.creds_present()) else "npm"
        else:
            provider = "traefik" if traefik.detect(g) else "none"
    if provider == "npm-api":
        if not g.cfg.ingress_api_url:
            raise SystemExit("ingress.provider = npm-api needs ingress.api_url")
        provider = "done" if npm_api.collect(g) else "npm"  # degrade to the file reader rather than lose the domains
    if provider == "npm":
        npm.collect(g)
    elif provider == "traefik":
        traefik.collect(g)
    elif provider not in ("none", "done"):
        raise SystemExit(f"unknown ingress provider: {provider}")
    _resolve(g)


def _resolve(g: Graph) -> None:
    cfg = g.cfg
    for h in g.ingress:
        h["targets"] = []
        for u in h["upstreams"]:
            host, _, port = u.rpartition(":") if u.count(":") == 1 else (u, "", "")
            host = host or u
            t = g.resolve(host, port or None)
            h["targets"].append({"upstream": u, "container": t})
        h["public"] = any(cfg.is_public_domain(d) for d in h["domains"]) and not h["internal"]
        h.setdefault("kind", "proxy"); h.setdefault("enabled", True); h.setdefault("acl", None)
        h.setdefault("cert", None); h.setdefault("error", "")
        for t in h["targets"]:
            if t["container"] and h["domains"]:
                g.edge("dom:" + h["domains"][0], t["container"], "ingress", t["upstream"])
