"""Assemble graph.json and render index.html from the bundled viewer template."""
from __future__ import annotations

import datetime
import json
import pathlib
import re

from . import homepage, ingress, refs
from .config import Config
from .discover import discover

VIEWER = pathlib.Path(__file__).with_name("viewer.html")
ANCHOR = "/*__DATA__*/null"
SECRET_RE = re.compile(r"passw|token|secret|api[_-]?key", re.I)

DEFAULT_ZONE_ORDER = ["Edge & infrastructure", "Sites & hosting", "Home & IoT", "Media & downloads",
                      "Radio & aircraft", "AI & voice", "Ops & monitoring", "Other"]


def build(cfg: Config) -> dict:
    g = discover(cfg)
    hp = homepage.apply(g) if cfg.homepage_config_dir != "none" else {"groups": []}
    ingress.collect(g)
    refs.collect(g)
    # zone order: wharfmap.toml wins; otherwise Homepage's layout order (one zone per group); otherwise the default
    if cfg.zone_order:
        order = cfg.zone_order
    elif hp["groups"]:
        order = [cfg.zone_of_group.get(x, x) for x in hp["groups"]]
        order = [z for i, z in enumerate(order) if z not in order[:i]]
    else:
        order = DEFAULT_ZONE_ORDER

    lan_ips = {e["target"][4:] for e in g.edges if e["target"].startswith("lan:")}
    lan_hosts = {ip: {"label": g.lan_labels.get(ip, ip), "kind": g.lan_kind.get(ip, "host")} for ip in sorted(lan_ips)}

    containers = []
    for n in g.containers:
        n = dict(n)
        for k in ("env", "exposed", "labels", "mounts", "pid", "compose_files"):
            n.pop(k, None)
        containers.append(n)

    return {
        "generated": datetime.datetime.now().astimezone().isoformat(timespec="minutes"),
        "title": cfg.title,
        "host": {"name": cfg.host_name, "ip": cfg.host_ip},
        "zones": {"order": order, "groups": cfg.zone_of_group},
        "links": cfg.links,
        "credit": cfg.credit,
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
