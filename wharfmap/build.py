"""Assemble graph.json and render index.html from the bundled viewer template."""
from __future__ import annotations

import datetime
import json
import pathlib
import re

from . import ingress, refs
from .config import Config
from .discover import discover

VIEWER = pathlib.Path(__file__).with_name("viewer.html")
ANCHOR = "/*__DATA__*/null"
SECRET_RE = re.compile(r"passw|token|secret|api[_-]?key", re.I)

DEFAULT_ZONE_ORDER = ["Edge & infrastructure", "Sites & hosting", "Home & IoT", "Media & downloads",
                      "Radio & aircraft", "AI & voice", "Ops & monitoring", "Other"]


def build(cfg: Config) -> dict:
    g = discover(cfg)
    ingress.collect(g)
    refs.collect(g)

    lan_ips = {e["target"][4:] for e in g.edges if e["target"].startswith("lan:")}
    lan_hosts = {ip: {"label": g.lan_labels.get(ip, ip), "kind": g.lan_kind.get(ip, "host")} for ip in sorted(lan_ips)}

    containers = []
    for n in g.containers:
        n = dict(n)
        for k in ("env", "exposed", "labels", "mounts", "pid"):
            n.pop(k, None)
        containers.append(n)

    return {
        "generated": datetime.datetime.now().astimezone().isoformat(timespec="minutes"),
        "title": cfg.title,
        "host": {"name": cfg.host_name, "ip": cfg.host_ip},
        "zones": {"order": cfg.zone_order or DEFAULT_ZONE_ORDER, "groups": cfg.zone_of_group},
        "links": cfg.links,
        "refresh": cfg.refresh_seconds,
        "containers": containers,
        "networks": g.networks,
        "ingress": g.ingress,
        "edges": g.edges,
        "lan_hosts": lan_hosts,
    }


def render(graph: dict) -> str:
    tpl = VIEWER.read_text()
    if ANCHOR not in tpl:
        raise SystemExit("viewer.html is missing the data anchor")
    return tpl.replace(ANCHOR, json.dumps(graph))


def write(cfg: Config, graph: dict) -> pathlib.Path:
    out = cfg.output_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / "graph.json").write_text(json.dumps(graph, indent=1))
    (out / "index.html").write_text(render(graph))
    return out


def secret_scan(graph: dict) -> list[str]:
    """Edge labels and container fields that look like they carry credentials. Should be empty."""
    hits = []
    for e in graph["edges"]:
        if SECRET_RE.search(e.get("label", "")) and not re.search(r"_(HOST|URL|ADDR|SERVER|ENDPOINT)\b", e["label"], re.I):
            hits.append(f"edge {e['source']} -> {e['target']}: {e['label']}")
    return hits
