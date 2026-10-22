"""Config-file references: wiring that lives in application config rather than env vars.

Declared in wharfmap.toml as `[[config_refs]]` entries. Files are read from the host
read-only; only host/port is extracted, credentials in URLs are dropped (see hostport_of).

  type = "regex"          any text file; `regex` must capture one URL or host[:port]
  type = "frigate"        mqtt broker + camera RTSP hosts from Frigate's config.yml
  type = "homeassistant"  integration hosts from HA's .storage/core.config_entries
  type = "prometheus"     scrape targets from prometheus.yml
"""
from __future__ import annotations

import fnmatch
import glob
import json
import pathlib
import re

from .apps import known_app
from .config import ConfigRef
from .discover import Graph

URL_RE = re.compile(r"^(?:[a-z0-9+.-]+://)?(?:[^/\s]*@)?(\[[^\]]+\]|[^:/?#\s]+)(?::(\d+))?")


def hostport_of(url: str) -> tuple[str | None, str | None]:
    """'https://user:pw@host:8443/path' -> ('host', '8443'). Userinfo is discarded, never kept."""
    m = URL_RE.match(url.strip())
    return (m.group(1).strip("[]"), m.group(2)) if m else (None, None)


def _files(ref: ConfigRef) -> list[pathlib.Path]:
    return [pathlib.Path(f) for f in sorted(glob.glob(str(pathlib.Path(ref.glob).expanduser()), recursive=True))]


def host_path(c: dict, container_path: str) -> str | None:
    """Translate a path inside the container to the host path via its bind mounts (longest prefix wins)."""
    best = None
    for dest, src in c.get("mounts", {}).items():
        d = dest.rstrip("/") or "/"
        if fnmatch.fnmatch(d, container_path):  # the config file itself is the mount (e.g. /etc/alloy/config.alloy)
            return src
        if container_path == d or container_path.startswith(d + "/"):
            if best is None or len(d) > len(best[0]):
                best = (d, src)
    if not best:
        return None
    d, src = best
    return src.rstrip("/") + container_path[len(d):]


def auto_refs(g: Graph) -> list[ConfigRef]:
    """Config refs implied by known apps: the parser knows where the app keeps its config inside the
    container, and Docker knows where that is on the host. Nothing to configure."""
    out = []
    for c in g.containers:
        app = known_app(c["image"], c["name"])
        for spec in (app or {}).get("refs", []):
            typ, cpath, label = spec[0], spec[1], spec[2]
            hp = host_path(c, cpath)
            if not hp:
                g.refs_skipped.append({"container": c["name"], "path": cpath, "why": "not a bind mount (or Wharfmap can't see the mount)"})
                continue
            ref = ConfigRef(container=c["name"], type=typ, glob=hp, label=label)
            if len(spec) > 3:
                ref.regex = spec[3]
            ref.auto = True
            out.append(ref)
    return out


def collect(g: Graph) -> None:
    refs = list(g.cfg.config_refs)
    if g.cfg.auto_refs:
        explicit = {(r.container, r.type) for r in refs}
        refs += [r for r in auto_refs(g) if (r.container, r.type) not in explicit]
    g.refs_used = []
    for ref in refs:
        if ref.container not in g.by_name:
            continue
        src = g.by_name[ref.container]["id"]
        fn = PARSERS.get(ref.type)
        if not fn:
            raise SystemExit(f"config_refs: unknown type {ref.type!r} (regex|frigate|homeassistant|prometheus)")
        files = _files(ref)
        before = len(g.edges)
        for f in files:
            try:
                fn(g, src, ref, f.read_text(errors="replace"))
            except OSError:
                continue
        if not files and getattr(ref, "auto", False):
            continue  # a known-app guess that isn't there; not worth a line
        g.refs_used.append({"container": ref.container, "type": ref.type, "glob": ref.glob, "files": len(files),
                            "edges": len(g.edges) - before, "auto": getattr(ref, "auto", False)})


def _regex(g: Graph, src: str, ref: ConfigRef, txt: str) -> None:
    for u in re.findall(ref.regex, txt, re.M):
        h, pt = hostport_of(u if isinstance(u, str) else u[0])
        g.ref_edge(src, h, pt, ref.label, lan_kind=ref.kind)


def _frigate(g: Graph, src: str, ref: ConfigRef, txt: str) -> None:
    m = re.search(r"^mqtt:\s*\n(?:[ \t]+.*\n)*?[ \t]+host:\s*(\S+)", txt, re.M)
    if m:
        g.ref_edge(src, m.group(1).strip("'\""), 1883, "mqtt.host")
    for u in set(re.findall(r"rtsps?://\S+", txt)):
        h, pt = hostport_of(u.split("#")[0])
        if h and h not in ("127.0.0.1", "localhost"):
            g.lan_labels.setdefault(h, f"camera {h}")
            g.ref_edge(src, h, pt or 554, "camera rtsp", lan_kind="device")


HA_DEFAULT_PORTS = {"mqtt": 1883, "transmission": 9091, "adguard": 8082, "wyoming": 10300, "frigate": 5000,
                    "plex": 32400, "esphome": 6052, "zha": None, "sonarr": 8989, "radarr": 7878}
HA_HOST_KEYS = ("host", "url", "base_url", "server", "broker", "address", "ip_address")


def _homeassistant(g: Graph, src: str, ref: ConfigRef, txt: str) -> None:
    try:
        entries = json.loads(txt)["data"]["entries"]
    except (ValueError, KeyError, TypeError):
        return
    for e in entries:
        data = e.get("data") or {}
        vals = [str(v) for k, v in data.items() if k in HA_HOST_KEYS and v]
        if e.get("domain") == "localtuya":  # devices keyed by id, each with a host
            for dev in (data.get("devices") or {}).values():
                if dev.get("host"):
                    vals.append(dev["host"])
                    g.lan_labels.setdefault(dev["host"], f"{dev.get('friendly_name', 'tuya')} (localtuya)")
        for v in vals:
            h, pt = hostport_of(v)
            if not h:
                continue
            pt = pt or HA_DEFAULT_PORTS.get(e.get("domain"))
            if g.cfg.is_lan_ip(h):
                g.lan_labels.setdefault(h, f"{e.get('title') or e.get('domain')} ({e.get('domain')})")
            g.ref_edge(src, h, pt, f"integration {e.get('domain')}", lan_kind="device")


def _prometheus(g: Graph, src: str, ref: ConfigRef, txt: str) -> None:
    for job, tgts in re.findall(r'job_name:\s*"?([\w-]+)"?[\s\S]*?targets:\s*\[([^\]]*)\]', txt):
        for t in re.findall(r"['\"]([^'\"]+)['\"]", tgts):
            h, pt = hostport_of(t)
            if h not in ("localhost", "127.0.0.1"):
                g.ref_edge(src, h, pt, f"scrape {job}")


PARSERS = {"regex": _regex, "frigate": _frigate, "homeassistant": _homeassistant, "prometheus": _prometheus}
