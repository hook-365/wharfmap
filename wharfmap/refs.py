"""Config-file references: wiring that lives in application config rather than env vars.

Declared in wharfmap.toml as `[[config_refs]]` entries. Files are read from the host
read-only; only host/port is extracted, credentials in URLs are dropped (see hostport_of).

  type = "regex"          any text file; `regex` must capture one URL or host[:port]
  type = "frigate"        mqtt broker + camera RTSP hosts from Frigate's config.yml
  type = "homeassistant"  integration hosts from HA's .storage/core.config_entries
  type = "prometheus"     scrape targets from prometheus.yml
"""
from __future__ import annotations

import glob
import json
import pathlib
import re

from .config import ConfigRef
from .discover import Graph

URL_RE = re.compile(r"^(?:[a-z0-9+.-]+://)?(?:[^/\s]*@)?(\[[^\]]+\]|[^:/?#\s]+)(?::(\d+))?")


def hostport_of(url: str) -> tuple[str | None, str | None]:
    """'https://user:pw@host:8443/path' -> ('host', '8443'). Userinfo is discarded, never kept."""
    m = URL_RE.match(url.strip())
    return (m.group(1).strip("[]"), m.group(2)) if m else (None, None)


def _files(ref: ConfigRef) -> list[pathlib.Path]:
    return [pathlib.Path(f) for f in sorted(glob.glob(str(pathlib.Path(ref.glob).expanduser()), recursive=True))]


def collect(g: Graph) -> None:
    for ref in g.cfg.config_refs:
        if ref.container not in g.by_name:
            continue
        src = g.by_name[ref.container]["id"]
        fn = PARSERS.get(ref.type)
        if not fn:
            raise SystemExit(f"config_refs: unknown type {ref.type!r} (regex|frigate|homeassistant|prometheus)")
        for f in _files(ref):
            try:
                fn(g, src, ref, f.read_text(errors="replace"))
            except OSError:
                continue


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
