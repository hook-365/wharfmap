"""Nginx Proxy Manager / NPMPlus: read the rendered nginx host configs inside the container.

Reading the rendered `.conf` files (rather than the API or the database) needs no credentials
and reflects exactly what nginx is serving. `deny all` in the file means an access list that
only allows LAN ranges, which we show as "internal".
"""
from __future__ import annotations

import pathlib
import re
import subprocess

from ..discover import Graph, sh

CANDIDATES = ("npmplus", "npm", "nginx-proxy-manager", "nginxproxymanager")
GREP = r'^\s*server_name|^\s*server [^ ]+ resolve|proxy_pass https?://[0-9\[]|deny all|auth_basic on|basic_auth|auth_request /'


def detect(g: Graph) -> bool:
    return bool(g.cfg.ingress_conf_dir) or bool(container(g))


def container(g: Graph) -> str | None:
    if g.cfg.ingress_container:
        return g.cfg.ingress_container
    for c in CANDIDATES:
        if c in g.by_name:
            return c
    for c in g.containers:
        if "nginx-proxy-manager" in c["image"] or "npmplus" in c["image"]:
            return c["name"]
    return None


LINE_RE = re.compile(GREP)


def _read(g: Graph) -> str:
    """Rendered host configs, as '== <file>' headers followed by the interesting lines."""
    if g.cfg.ingress_conf_dir:  # filesystem mode: works through a read-only socket proxy, no exec needed
        out = []
        for f in sorted(pathlib.Path(g.cfg.ingress_conf_dir).glob("*.conf")):
            out.append(f"== {f}")
            out += [l for l in f.read_text(errors="replace").splitlines() if LINE_RE.search(l)]
        return "\n".join(out)
    name = container(g)
    if not name:
        return ""
    try:
        return sh("docker", "exec", name, "sh", "-c",
                  'for f in /data/nginx/proxy_host/*.conf; do echo "== $f"; grep -E "' + GREP + '" "$f"; done')
    except subprocess.CalledProcessError:
        return ""


def collect(g: Graph) -> None:
    raw = _read(g)
    cur = None
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("== "):
            cur = {"file": line[3:].rsplit("/", 1)[-1], "domains": [], "upstreams": [], "internal": False, "auth": False}
            g.ingress.append(cur)
        elif cur is None:
            continue
        elif line.startswith("server_name"):
            cur["domains"] += [d.rstrip(";") for d in line.split()[1:]]
        elif line.startswith("server "):
            cur["upstreams"].append(line.split()[1])
        elif line.startswith("proxy_pass"):
            cur["upstreams"].append(line.split("//", 1)[1].rstrip(";").split("$")[0].rstrip("/"))
        elif line == "deny all;":
            cur["internal"] = True
        elif "auth" in line:
            cur["auth"] = True
