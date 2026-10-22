"""Homepage (gethomepage.dev) as a source of context, so nothing has to be written twice.

  settings.yaml  `layout:` lists the dashboard's groups in display order. When wharfmap.toml has no
                 [zones], every Homepage group becomes a zone in that order. When it has [zones.groups],
                 groups that aren't mapped still get Homepage's order instead of landing in "Other".
  services.yaml  For dashboards that don't use labels: name, description, href and group per service.
                 Entries are matched to a container by `container:` (the Docker widget) or by the
                 href's host:port, and fill in whatever the labels didn't.

The config dir is found automatically through the Homepage container's /app/config bind mount, or set
[homepage] config_dir in wharfmap.toml. Both files are read with a small indentation scan unless
PyYAML happens to be installed; Homepage's own files are simple enough for that.
"""
from __future__ import annotations

import pathlib
import re

from . import dockerfs
from .discover import Graph
from .refs import host_path, hostport_of


def _yaml(text: str):
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except ImportError:
        return None
    except Exception:
        return {}


def _unq(s: str) -> str:
    return s.strip().strip("'\"")


def layout_groups(settings: pathlib.Path, text: str | None = None) -> list[str]:
    """Group names under `layout:` in order."""
    if text is None:
        if not settings.exists():
            return []
        text = settings.read_text(encoding="utf-8", errors="replace")
    doc = _yaml(text)
    if isinstance(doc, dict):
        lay = doc.get("layout") or {}
        return [str(k) for k in (lay.keys() if isinstance(lay, dict) else [list(d)[0] for d in lay if isinstance(d, dict)])]
    out, inside = [], False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            inside = line.strip() == "layout:"
            continue
        if inside and indent == 2:
            m = re.match(r"^\s*(?:- )?(\"[^\"]+\"|'[^']+'|[^:]+):", line)
            if m:
                out.append(_unq(m.group(1)))
    return out


def services(path: pathlib.Path, text: str | None = None) -> list[dict]:
    """[{group, name, href, description, container}] from services.yaml."""
    if text is None:
        if not path.exists():
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
    doc = _yaml(text)
    out: list[dict] = []
    if isinstance(doc, list):
        for grp in doc:
            if not isinstance(grp, dict):
                continue
            for gname, items in grp.items():
                for it in items or []:
                    if not isinstance(it, dict):
                        continue
                    for sname, props in it.items():
                        props = props or {}
                        out.append({"group": str(gname), "name": str(sname), "href": props.get("href"),
                                    "description": props.get("description"), "container": props.get("container")})
        return out
    if doc is not None:
        return out
    group = cur = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        body = line.strip()
        if indent == 0 and body.startswith("- ") and body.endswith(":"):
            group, cur = _unq(body[2:-1]), None
        elif group and indent == 4 and body.startswith("- ") and body.endswith(":"):
            cur = {"group": group, "name": _unq(body[2:-1]), "href": None, "description": None, "container": None}
            out.append(cur)
        elif cur and indent >= 8:
            m = re.match(r"^(href|description|container):\s*(.+)$", body)
            if m:
                cur[m.group(1)] = _unq(m.group(2))
    return out


def apply(g: Graph) -> dict:
    """Fill container titles/groups/descriptions/hrefs from services.yaml and return the group order."""
    cfg = g.cfg
    cdir = cfg.homepage_config_dir
    hp = next((c for c in g.containers if c.get("app") == "Homepage" and not c.get("ghost")), None)
    if not cdir:
        cdir = host_path(hp, "/app/config") if hp else None
    d = pathlib.Path(cdir) if cdir else None
    how = "host"
    if d and (d / "settings.yaml").exists():
        groups = layout_groups(d / "settings.yaml")
        svcs = services(d / "services.yaml")
    elif hp:  # no reachable mount: fetch the two files through the Docker API
        got = dict(dockerfs.read_glob(hp["name"], "/app/config/*.yaml"))
        if not got:
            return {"groups": [], "services": 0, "dir": None}
        groups = layout_groups(pathlib.Path("settings.yaml"), got.get("/app/config/settings.yaml", ""))
        svcs = services(pathlib.Path("services.yaml"), got.get("/app/config/services.yaml", ""))
        d, how = pathlib.Path(f"{hp['name']}:/app/config"), "docker-api"
    else:
        return {"groups": [], "services": 0, "dir": None}
    matched = 0
    for s in svcs:
        c = None
        if s.get("container") and s["container"] in g.by_name:
            c = g.by_name[s["container"]]
        elif s.get("href"):
            h, p = hostport_of(s["href"])
            t = g.resolve(h, p or (443 if str(s["href"]).startswith("https") else 80))
            c = g.by_name.get(t) if t else None
        if not c:
            continue
        matched += 1
        if c["title"] == c["name"]:
            c["title"] = s["name"]
        c["group"] = c["group"] or s["group"]
        c["desc"] = c["desc"] or (s.get("description") or "")
        c["href"] = c["href"] or s.get("href")
    return {"groups": groups, "services": matched, "dir": str(d), "how": how}
