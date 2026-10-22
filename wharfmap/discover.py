"""Live discovery from the Docker engine. Needs the docker CLI and socket access, nothing else.

Produces the Graph: containers, networks, the host-port -> owner map, and the edges that can
be inferred without reading any application config (netns sharing, depends_on, env-var refs).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field

from .apps import known_app
from .config import Config


def sh(*argv: str) -> str:
    return subprocess.run(argv, capture_output=True, text=True, check=True).stdout


@dataclass
class Graph:
    cfg: Config
    containers: list[dict] = field(default_factory=list)
    networks: list[dict] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)
    ingress: list[dict] = field(default_factory=list)
    by_id: dict[str, dict] = field(default_factory=dict)
    by_name: dict[str, dict] = field(default_factory=dict)
    hostport: dict[int, str] = field(default_factory=dict)   # published host port -> owning container id
    ip_owner: dict[str, str] = field(default_factory=dict)   # docker IP -> container id
    lan_labels: dict[str, str] = field(default_factory=dict)
    lan_kind: dict[str, str] = field(default_factory=dict)
    refs_used: list[dict] = field(default_factory=list)
    refs_skipped: list[dict] = field(default_factory=list)
    compose_unreachable: set = field(default_factory=set)
    _seen: set = field(default_factory=set)

    # ---- edges ----
    def edge(self, src: str | None, dst: str | None, kind: str, label: str = "") -> None:
        if not src or not dst or src == dst:
            return
        key = (src, dst, kind, label)
        if key in self._seen:
            return
        self._seen.add(key)
        self.edges.append({"source": src, "target": dst, "kind": kind, "label": label})

    def resolve(self, host: str | None, port: int | str | None = None) -> str | None:
        """Map a host[:port] reference to a node id: a container, or `lan:<ip>` for another LAN machine."""
        if not host:
            return None
        if host in self.by_name:
            return self.by_name[host]["id"]
        if host in self.cfg.host_names():
            return self.hostport.get(int(port)) if port else None
        if host in self.ip_owner:
            return self.ip_owner[host]
        if self.cfg.is_lan_ip(host):
            return "lan:" + host
        return None

    def ref_edge(self, src: str, host: str | None, port, label: str, kind: str = "config", lan_kind: str = "host") -> None:
        t = self.resolve(host, port)
        if t and t != src:
            if t.startswith("lan:"):
                self.lan_kind.setdefault(t[4:], lan_kind)
            self.edge(src, t, kind, label)


def _label(labels: dict, key: str) -> str | None:
    """`wharfmap.<key>` wins over `homepage.<key>` so the map can be tuned without touching dashboards."""
    return labels.get(f"wharfmap.{key}") or labels.get(f"homepage.{key}")


def discover(cfg: Config) -> Graph:
    g = Graph(cfg)
    ids = sh("docker", "ps", "-aq").split()
    raw = json.loads(sh("docker", "inspect", *ids)) if ids else []
    net_ids = sh("docker", "network", "ls", "-q").split()
    raw_nets = json.loads(sh("docker", "network", "inspect", *net_ids)) if net_ids else []

    for c in raw:
        L = c["Config"].get("Labels") or {}
        name = c["Name"].lstrip("/")
        nm = c["HostConfig"]["NetworkMode"]
        ports = []
        for k, v in (c["NetworkSettings"].get("Ports") or {}).items():
            cport, proto = k.split("/")
            for b in v or []:
                ports.append({"host_ip": b["HostIp"], "host_port": int(b["HostPort"]), "cport": int(cport), "proto": proto})
        exposed = sorted({int(k.split("/")[0]) for k in (c["Config"].get("ExposedPorts") or {})})
        deps = [d.split(":")[0] for d in filter(None, (L.get("com.docker.compose.depends_on") or "").split(","))]
        project = L.get("com.docker.compose.project")
        n = {
            "id": name, "name": name,
            "stack": project or _guess_stack(name),
            "service": L.get("com.docker.compose.service") or name,
            "status": c["State"]["Status"],
            "health": (c["State"].get("Health") or {}).get("Status"),
            "image": c["Config"]["Image"],
            "networks": sorted(c["NetworkSettings"]["Networks"].keys()),
            "ips": {k: v.get("IPAddress") for k, v in c["NetworkSettings"]["Networks"].items() if v.get("IPAddress")},
            "ports": sorted(ports, key=lambda p: p["host_port"]),
            "exposed": exposed,
            "hostnet": nm == "host",
            "netns": nm[len("container:"):] if nm.startswith("container:") else None,
            "depends_on": deps,
            "title": _label(L, "name") or name,
            "group": _label(L, "group"),
            "desc": _label(L, "description") or "",
            "href": _label(L, "href"),
            "env": c["Config"].get("Env") or [],
            "labels": L,
            "mounts": {m["Destination"]: m["Source"] for m in c.get("Mounts") or [] if m.get("Source") and m.get("Destination")},
            "pid": (c.get("State") or {}).get("Pid") or 0,
            "app": (known_app(c["Config"]["Image"], name) or {}).get("name"),
            "compose_files": [f for f in (L.get("com.docker.compose.project.config_files") or "").split(",") if f],
            "created": c["Created"][:10],
        }
        if n["hostnet"]:
            listen, n["ports_source"] = _host_net_ports(cfg, n, c)
            n["ports"] = [{"host_ip": "0.0.0.0", "host_port": p, "cport": p, "proto": "tcp"} for p in listen]
        g.containers.append(n)
        g.by_id[c["Id"]] = n
        g.by_id[c["Id"][:12]] = n
        g.by_name[name] = n
        g.by_name.setdefault(n["service"], n)

    # netns parents may be referenced by id or by name
    for n in g.containers:
        if n["netns"]:
            p = g.by_id.get(n["netns"]) or g.by_name.get(n["netns"])
            n["netns"] = p["id"] if p else None

    # host port -> owner. Inside a shared netns the publisher is the parent, but the real
    # listener is whichever dependent EXPOSEs that container port.
    for n in g.containers:
        for p in n["ports"]:
            owner = n
            if not n["hostnet"]:
                for d in g.containers:
                    if d["netns"] == n["id"] and p["cport"] in d["exposed"]:
                        owner = d
                        break
            g.hostport.setdefault(p["host_port"], owner["id"])
            p["owner"] = owner["id"]
        for ip in n["ips"].values():
            g.ip_owner[ip] = n["id"]

    for nw in raw_nets:
        if nw["Name"] == "none":
            continue
        ipam = ((nw.get("IPAM") or {}).get("Config") or [{}])[0]
        g.networks.append({"name": nw["Name"], "driver": nw["Driver"], "subnet": ipam.get("Subnet"),
                           "gateway": ipam.get("Gateway"), "ip_range": ipam.get("IPRange"),
                           "members": sorted(v["Name"] for v in (nw.get("Containers") or {}).values())})

    if cfg.ghosts:
        _ghost_services(g)
    _structural_edges(g)
    _env_edges(g)
    g.lan_labels.update(cfg.lan_hosts)
    return g


def _host_net_ports(cfg: Config, n: dict, raw: dict) -> tuple[list[int], str]:
    """Listen ports of a host-network container, best source first:
    wharfmap.toml > the container's own listening sockets (/proc, when visible) > known app > image EXPOSE."""
    listed = cfg.host_net_ports.get(n["name"]) or cfg.host_net_ports.get(n["service"])
    if listed:
        return sorted(set(listed)), "config"
    if n["status"] == "running" and n["pid"]:
        ports = [p for p in _listening_ports(n["pid"], raw.get("Id", "")) if not _ephemeral(p)]
        if ports:
            return ports, "proc"
    app = known_app(n["image"], n["name"])
    if app and app.get("ports"):
        return sorted(set(app["ports"])), "known"
    exposed = sorted({int(k.split("/")[0]) for k in (raw["Config"].get("ExposedPorts") or {}) if k.endswith("/tcp")})
    if exposed:
        return exposed, "expose"
    return [], "none"


def _ephemeral(port: int) -> bool:
    """Ports in the kernel's dynamic range are transient listeners (transcoders, helpers), not services."""
    try:
        lo, hi = map(int, open("/proc/sys/net/ipv4/ip_local_port_range").read().split())
    except (OSError, ValueError):
        lo, hi = 32768, 60999
    return lo <= port <= hi


def _listening_ports(pid: int, cid: str = "") -> list[int]:
    """TCP ports in LISTEN state owned by the container's own processes.

    A host-network container's /proc/<pid>/net/tcp lists every socket on the host, so we can't
    just read it: we collect the socket inodes held by processes in the container's cgroup and
    keep only the listening sockets with those inodes. Needs the host pid namespace and enough
    privilege to read /proc/<pid>/fd (root, or `pid: host` in the container). Otherwise: []."""
    import os
    pids = set()
    try:
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                if cid and cid[:12] in open(f"/proc/{d}/cgroup").read():
                    pids.add(int(d))
            except OSError:
                continue
    except OSError:
        return []
    if not pids:
        pids = {pid}
    inodes = set()
    for p in pids:
        try:
            for fd in os.listdir(f"/proc/{p}/fd"):
                try:
                    link = os.readlink(f"/proc/{p}/fd/{fd}")
                except OSError:
                    continue
                if link.startswith("socket:["):
                    inodes.add(link[8:-1])
        except OSError:
            continue
    if not inodes:
        return []
    ports = set()
    for f in (f"/proc/{pid}/net/tcp", f"/proc/{pid}/net/tcp6"):
        try:
            with open(f) as fh:
                next(fh)
                for line in fh:
                    parts = line.split()
                    if len(parts) > 9 and parts[3] == "0A" and parts[9] in inodes:
                        ports.add(int(parts[1].rsplit(":", 1)[1], 16))
        except OSError:
            continue
    return sorted(ports)


def _compose_services(path: str) -> dict[str, dict]:
    """Service names (+ image, profiles) from a compose file. Uses PyYAML if it happens to be installed,
    otherwise a small indentation scan that only looks at the top-level `services:` block."""
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return {}
    try:
        import yaml  # type: ignore
        doc = yaml.safe_load(text) or {}
        return {k: {"image": (v or {}).get("image", ""), "profiles": list((v or {}).get("profiles") or [])}
                for k, v in (doc.get("services") or {}).items()}
    except ImportError:
        pass
    except Exception:
        return {}
    out: dict[str, dict] = {}
    in_services, cur, in_profiles = False, None, False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            in_services = line.strip() == "services:"
            cur = None
            continue
        if not in_services:
            continue
        m = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)
        if m and indent == 2:
            cur = m.group(1)
            out[cur] = {"image": "", "profiles": []}
            in_profiles = False
            continue
        if cur and indent >= 4:
            m = re.match(r"^\s+image:\s*['\"]?([^'\"]+?)['\"]?\s*$", line)
            if m:
                out[cur]["image"] = m.group(1)
            m = re.match(r"^\s+profiles:\s*\[([^\]]*)\]", line)
            if m:
                out[cur]["profiles"] = [p.strip(" '\"") for p in m.group(1).split(",") if p.strip()]
            elif re.match(r"^\s+profiles:\s*$", line):
                in_profiles = True
                continue
            elif in_profiles and re.match(r"^\s+-\s*", line):
                out[cur]["profiles"].append(line.split("-", 1)[1].strip(" '\""))
                continue
            in_profiles = False
    return out


def _ghost_services(g: Graph) -> None:
    """Compose services that exist in the file but have no container (profiles, commented-out deploys,
    never-started services). Shown as 'not created' chips so the map matches the compose files, not just Docker."""
    seen: set[tuple[str, str]] = set()
    for c in list(g.containers):
        project = c["stack"]
        for path in c["compose_files"]:
            if (project, path) in seen:
                continue
            seen.add((project, path))
            services = _compose_services(path)
            if not services:
                if path and not os.path.exists(path):
                    g.compose_unreachable.add(path)
                continue
            have = {x["service"] for x in g.containers if x["stack"] == project}
            for svc, meta in services.items():
                if svc in have:
                    continue
                name = f"{project}-{svc}"
                g.containers.append({
                    "id": name, "name": name, "stack": project, "service": svc, "status": "not created", "health": None,
                    "image": meta.get("image") or "", "networks": [], "ips": {}, "ports": [], "exposed": [],
                    "hostnet": False, "netns": None, "depends_on": [], "title": svc, "group": None,
                    "desc": ("profile: " + ", ".join(meta["profiles"])) if meta.get("profiles") else "defined in compose, no container",
                    "href": None, "env": [], "labels": {}, "mounts": {}, "pid": 0, "app": None, "compose_files": [path],
                    "created": "", "ghost": True, "profiles": meta.get("profiles", []),
                })
                g.by_name.setdefault(name, g.containers[-1])


def _guess_stack(name: str) -> str:
    if name.startswith("nextcloud-aio"):
        return "nextcloud-aio"
    return "standalone"


def _structural_edges(g: Graph) -> None:
    for n in g.containers:
        if n["netns"]:
            g.edge(n["id"], n["netns"], "netns", "shares netns")
        for d in n["depends_on"]:
            t = next((m for m in g.containers if m["stack"] == n["stack"] and m["service"] == d), None)
            if t:
                g.edge(n["id"], t["id"], "depends", "depends_on")


def _env_edges(g: Graph) -> None:
    """Runtime references: env values naming another container, or <host>:<port> on this host."""
    cfg = g.cfg
    names = sorted(map(re.escape, g.by_name), key=len, reverse=True)
    names_re = re.compile(r"(?<![\w.-])(" + "|".join(names) + r")(?![\w-])") if names else None
    hosts = sorted((re.escape(h) for h in cfg.host_names()), key=len, reverse=True)
    hp_re = re.compile(r"(?:" + "|".join(hosts) + r")[:/]+(\d{2,5})")
    keyish_re = re.compile(r"HOST|URL|ADDR|ENDPOINT|SERVER|BROKER|URI|DSN|FEED|TARGET|UPSTREAM|API", re.I)
    ip_re = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
    local = set()  # one edge per (container, target, env key) however it was matched
    def add(src, dst, k, label):
        if dst and (src, dst, k) not in local:
            local.add((src, dst, k))
            g.edge(src, dst, "env", label)
    for n in g.containers:
        for e in n["env"]:
            k, _, v = e.partition("=")
            if not v or k in cfg.env_skip_keys:
                continue
            # the broad match produced garbage (MYSQL_DATABASE=homeassistant is not a link): gate on key or scheme
            keyish = keyish_re.search(k) or "://" in v
            if keyish and names_re:
                for m in names_re.finditer(v):
                    add(n["id"], g.by_name[m.group(1)]["id"], k, k)
            for m in hp_re.finditer(v):
                port = int(m.group(1))
                t = None
                if n["netns"] and re.search(r"localhost|127\.0\.0\.1", m.group(0)):
                    sibs = [s for s in g.containers if s["netns"] == n["netns"] or s["id"] == n["netns"]]
                    t = next((s["id"] for s in sibs if port in s["exposed"]), None)
                add(n["id"], t or g.hostport.get(port), k, f"{k} :{port}")
            for ip in ip_re.findall(v):
                if cfg.is_lan_ip(ip):
                    add(n["id"], "lan:" + ip, k, k)
