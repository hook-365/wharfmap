"""Agent mode: combine the graphs of several Docker hosts into one map.

Each host runs `wharfmap agent`, which discovers locally and POSTs its finished graph.json to the
server's /report. Only the graph crosses the network (env values are already reduced to names and
ports), and the server never reaches back into any host.

The server keeps its own graph plus the last report from each host and merges them here. With a
single host nothing is touched, so graph.json stays exactly what it was. With several, every id is
prefixed with its host name (`nas/redis`), networks and stacks too, and references to another
host's LAN IP are resolved to the container listening on that host's port when one is known.
"""
from __future__ import annotations

import copy
import datetime
import re

SAFE = re.compile(r"[^A-Za-z0-9._-]")


def safe_name(s: str) -> str:
    return SAFE.sub("-", s or "")[:64] or "host"


def age_seconds(generated: str) -> float:
    try:
        t = datetime.datetime.fromisoformat(generated)
        if t.tzinfo is None:
            t = t.astimezone()
        return (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds()
    except (ValueError, TypeError):
        return float("inf")


def is_stale(graph: dict) -> bool:
    """A report is stale once it has missed three refresh cycles (and at least 15 minutes)."""
    return age_seconds(graph.get("generated", "")) > max(3 * int(graph.get("refresh") or 300), 900)


def _pfx(h: str, id_: str | None) -> str | None:
    if not id_:
        return id_
    if id_.startswith(("lan:", "dom:")):
        return id_
    if id_.startswith("net:"):
        return "net:" + h + "/" + id_[4:]
    return h + "/" + id_


def prefixed(graph: dict, h: str) -> dict:
    """A deep copy of `graph` with every host-local identifier namespaced under host `h`."""
    g = copy.deepcopy(graph)
    for c in g.get("containers", []):
        c["id"] = _pfx(h, c["id"])
        c["netns"] = _pfx(h, c.get("netns"))
        c["stack"] = h + "/" + c["stack"]
        c["host"] = h
        c["networks"] = [h + "/" + n for n in c.get("networks", [])]
        c["ips"] = {h + "/" + k: v for k, v in (c.get("ips") or {}).items()}
        for p in c.get("ports", []):
            if p.get("owner"):
                p["owner"] = _pfx(h, p["owner"])
    for n in g.get("networks", []):
        n["name"] = h + "/" + n["name"]
        n["members"] = [h + "/" + m for m in n.get("members", [])]
        n["host"] = h
    for e in g.get("edges", []):
        e["source"] = _pfx(h, e["source"])
        e["target"] = _pfx(h, e["target"])
    for i in g.get("ingress", []):
        i["host"] = h
        for t in i.get("targets", []):
            t["container"] = _pfx(h, t.get("container"))
    return g


def _split_hostport(u: str) -> tuple[str, int | None]:
    u = u.strip().rstrip("/")
    if u.count(":") == 1:
        host, _, port = u.rpartition(":")
        return host, int(port) if port.isdigit() else None
    return u, None


def merge(local: dict, remotes: list[dict]) -> dict:
    """The server's own graph plus the reports of other hosts. With no remotes, `local` is returned as is."""
    if not remotes:
        return local
    graphs = [(local, True)] + [(r, False) for r in remotes]
    host_ip: dict[str, str] = {}   # LAN ip -> host name, for cross-host references
    for g, _ in graphs:
        host_ip[g["host"]["ip"]] = safe_name(g["host"]["name"])

    out = dict(local)
    out.update({"containers": [], "networks": [], "edges": [], "ingress": [], "lan_hosts": {}, "hosts": []})
    seen_edges: set[tuple] = set()
    for g, is_local in graphs:
        h = safe_name(g["host"]["name"])
        stale = (not is_local) and is_stale(g)
        live = sum(1 for c in g.get("containers", []) if not c.get("ghost"))
        out["hosts"].append({"name": h, "ip": g["host"]["ip"], "generated": g.get("generated", ""),
                             "local": is_local, "stale": stale, "containers": live, "version": g.get("version", "")})
        p = prefixed(g, h)
        for c in p["containers"]:
            c["stale"] = stale
        out["containers"] += p["containers"]
        out["networks"] += p["networks"]
        out["ingress"] += p["ingress"]
        for e in p["edges"]:
            key = (e["source"], e["target"], e["kind"], e.get("label", ""))
            if key not in seen_edges:
                seen_edges.add(key)
                out["edges"].append(e)
        for ip, v in (g.get("lan_hosts") or {}).items():
            if ip not in host_ip:
                out["lan_hosts"].setdefault(ip, v)

    # (host, published port) -> container id, for resolving another host's ip:port. Bind address is
    # ignored on purpose, like the single-host resolver: a reference to a loopback-only port from
    # another machine is a misconfiguration worth seeing on the map, not something to hide.
    portmap: dict[tuple[str, int], str] = {}
    for c in out["containers"]:
        for pt in c.get("ports", []):
            if pt.get("owner"):
                portmap.setdefault((c["host"], pt["host_port"]), pt["owner"])

    for e in out["edges"]:
        if e["target"].startswith("lan:") and e["target"][4:] in host_ip:
            h = host_ip[e["target"][4:]]
            t = portmap.get((h, e["port"])) if e.get("port") else None
            if t and t != e["source"]:
                e["target"] = t
            else:
                out["lan_hosts"].setdefault(e["target"][4:], {"label": h, "kind": "host"})
    for i in out["ingress"]:
        for t in i.get("targets", []):
            if t.get("container"):
                continue
            ip, port = _split_hostport(t["upstream"])
            if ip in host_ip and port:
                c = portmap.get((host_ip[ip], port))
                if c:
                    t["container"] = c
                    if i.get("domains"):
                        key = ("dom:" + i["domains"][0], c, "ingress", t["upstream"])
                        if key not in seen_edges:
                            seen_edges.add(key)
                            out["edges"].append({"source": key[0], "target": c, "kind": "ingress", "label": t["upstream"]})
    # edges may now point at the same container twice (lan:ip resolved to what a name match already found)
    deduped, seen_edges = [], set()
    for e in out["edges"]:
        key = (e["source"], e["target"], e["kind"], e.get("label", ""))
        if key not in seen_edges:
            seen_edges.add(key)
            deduped.append(e)
    out["edges"] = deduped
    return out
