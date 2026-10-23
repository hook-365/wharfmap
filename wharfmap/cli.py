"""wharfmap: chart a Docker host.

  wharfmap generate [-c wharfmap.toml] [-o out]      write out/graph.json + out/index.html
  wharfmap serve    [-c ...] [--port 3008] [--every N]     generate, then serve the output directory
                                                     (also accepts POST /report from agents when WHARFMAP_TOKEN is set)
  wharfmap agent    [-c ...] [--url http://server:3008]   discover this host and POST the graph to a server, forever
  wharfmap render -i graph.json [-o out]            index.html from a saved graph.json (no Docker needed)
  wharfmap doctor   [-c ...]                         what was discovered automatically and what still needs config
  wharfmap init                                      print a starter config built from this host
"""
from __future__ import annotations

import argparse
import functools
import hmac
import http.server
import json
import os
import pathlib
import sys
import threading
import time
import urllib.error
import urllib.request

from . import build, config, merge

MAX_REPORT = 32 * 1024 * 1024
REPORT_EXPIRE = 24 * 3600   # a host that has not reported for a day is dropped from the map


def _cfg(args) -> config.Config:
    path = args.config
    if path is None and pathlib.Path("wharfmap.toml").is_file():
        path = "wharfmap.toml"
    cfg = config.load(path)
    out = getattr(args, "out", None) or os.environ.get("WHARFMAP_OUT")
    if out:
        cfg.output_dir = pathlib.Path(out)
    return cfg


def cmd_generate(args) -> int:
    cfg = _cfg(args)
    graph = build.build(cfg)
    out = build.write(cfg, graph)
    live = sum(1 for c in graph["containers"] if not c.get("ghost"))
    print(f"{live} containers, {len(graph['networks'])} networks, "
          f"{len(graph['ingress'])} proxy hosts, {len(graph['edges'])} edges -> {out / 'index.html'}")
    for h in build.secret_scan(graph):
        print("WARNING possible credential in output:", h, file=sys.stderr)
    return 0


def cmd_serve(args) -> int:
    cfg = _cfg(args)
    token = os.environ.get("WHARFMAP_TOKEN", "")
    hosts_dir = cfg.output_dir / "hosts"
    lock = threading.Lock()
    state = {"local": None, "remotes": {}}   # remotes: host name -> last reported graph
    if hosts_dir.is_dir():                      # reports survive a restart
        for f in hosts_dir.glob("*.json"):
            try:
                g = json.loads(f.read_text())
                if merge.age_seconds(g.get("generated", "")) < REPORT_EXPIRE:
                    state["remotes"][merge.safe_name(g["host"]["name"])] = g
            except (ValueError, KeyError, OSError):
                pass

    def publish():
        """Write the merged graph: this host plus every report that is less than a day old."""
        with lock:
            remotes = [g for g in state["remotes"].values() if merge.age_seconds(g.get("generated", "")) < REPORT_EXPIRE]
            build.write(cfg, merge.merge(state["local"], remotes))

    def regen():
        try:
            local = build.build(cfg)
            with lock:
                state["local"] = local
            publish()
            n = len(state["remotes"])
            print(time.strftime("%H:%M:%S"), "regenerated" + (f" (+{n} remote host{'s'[:n^1]})" if n else ""), flush=True)
        except Exception as e:  # keep serving the last good copy
            print("regenerate failed:", e, file=sys.stderr, flush=True)

    def accept_report(handler) -> tuple[int, str]:
        if not token:
            return 403, "this server has no WHARFMAP_TOKEN set, so it does not accept reports"
        auth = handler.headers.get("Authorization", "")
        if not (auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], token)):
            return 401, "bad token"
        length = int(handler.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_REPORT:
            return 413, "report too large or empty"
        try:
            g = json.loads(handler.rfile.read(length))
            name = merge.safe_name(g["host"]["name"])
            assert isinstance(g["containers"], list) and isinstance(g["edges"], list) and isinstance(g["networks"], list)
        except (ValueError, KeyError, TypeError, AssertionError):
            return 400, "not a wharfmap graph.json"
        if name == merge.safe_name(cfg.host_name):
            return 409, f"a report named like this server's own host ({name}); give the agent WHARFMAP_HOST=<its name>"
        g.setdefault("received", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        with lock:
            state["remotes"][name] = g
            hosts_dir.mkdir(parents=True, exist_ok=True)
            (hosts_dir / f"{name}.json").write_text(json.dumps(g))
        if state["local"] is not None:
            publish()
        live = sum(1 for c in g["containers"] if not c.get("ghost"))
        print(time.strftime("%H:%M:%S"), f"report from {name} ({g['host'].get('ip', '?')}): {live} containers", flush=True)
        return 204, ""

    every = cfg.refresh_seconds if args.every is None else args.every
    regen()
    if every > 0:
        def loop():
            while True:
                time.sleep(every)
                regen()
        threading.Thread(target=loop, daemon=True).start()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_POST(self):
            path = self.path.rstrip("/")
            if path == "/regenerate":
                regen()
                self.send_response(204)
                self.end_headers()
            elif path == "/report":
                code, msg = accept_report(self)
                self.send_response(code)
                if code != 204:
                    body = (msg + "\n").encode()
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.end_headers()
            else:
                self.send_error(404)

        def log_message(self, *a):
            pass

    handler = functools.partial(Handler, directory=str(cfg.output_dir))
    srv = http.server.ThreadingHTTPServer((args.bind, args.port), handler)
    print(f"serving {cfg.output_dir} on http://{args.bind}:{args.port}  (regenerate every {every} s" + ("" if every else " = only on POST /regenerate") + ")"
          + ("; accepting agent reports on POST /report" if token else "; set WHARFMAP_TOKEN to accept agent reports"), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_agent(args) -> int:
    """Discover this host and hand the graph to a server running `wharfmap serve` elsewhere."""
    cfg = _cfg(args)
    url = (args.url or cfg.report_url).rstrip("/")
    token = os.environ.get("WHARFMAP_TOKEN", "")
    if not url:
        raise SystemExit("agent: tell me where to report: --url, [report] url in wharfmap.toml, or WHARFMAP_REPORT_URL")
    if not token:
        raise SystemExit("agent: WHARFMAP_TOKEN is not set (the same token the server was started with)")
    if not url.endswith("/report"):
        url += "/report"
    if cfg.host_name == cfg.host_name.lower() and len(cfg.host_name) in (12, 64) and all(ch in "0123456789abcdef" for ch in cfg.host_name):
        print(f"agent: host name {cfg.host_name!r} looks like a container id; set WHARFMAP_HOST (and WHARFMAP_HOST_IP)", file=sys.stderr)
    every = cfg.refresh_seconds if args.every is None else args.every

    def report() -> bool:
        graph = build.build(cfg)
        if args.out:
            build.write(cfg, graph)
        body = json.dumps(graph).encode()
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                r.read()
            live = sum(1 for c in graph["containers"] if not c.get("ghost"))
            print(time.strftime("%H:%M:%S"), f"reported {live} containers as {cfg.host_name} ({cfg.host_ip}) to {url}", flush=True)
            return True
        except urllib.error.HTTPError as e:
            print(time.strftime("%H:%M:%S"), f"server refused the report: {e.code} {e.read().decode(errors='replace').strip()[:200]}", file=sys.stderr, flush=True)
        except (urllib.error.URLError, OSError) as e:
            print(time.strftime("%H:%M:%S"), f"could not reach {url}: {e}", file=sys.stderr, flush=True)
        return False

    def attempt() -> bool:
        try:
            return report()
        except Exception as e:  # Docker not up yet, socket missing, ...: log and try again next round
            print(time.strftime("%H:%M:%S"), "discovery failed:", str(e).strip()[:300], file=sys.stderr, flush=True)
            return False

    ok = attempt()
    if args.once:
        return 0 if ok else 1
    while True:
        time.sleep(max(30, every))
        attempt()


def cmd_render(args) -> int:
    """index.html from a saved graph.json: demos, screenshots, or viewing a graph from another host."""
    graph = json.loads(pathlib.Path(args.input).read_text())
    out = pathlib.Path(args.out or "out")
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(build.render(graph))
    (out / "graph.json").write_text(json.dumps(graph, indent=1))
    print(f"rendered {args.input} -> {out / 'index.html'}")
    return 0


def cmd_doctor(args) -> int:
    """What discovery could and could not work out, and the few lines of config that would fix it."""
    from . import homepage, ingress, refs
    from .discover import discover
    cfg = _cfg(args)
    g = discover(cfg)
    hp = homepage.apply(g) if cfg.homepage_config_dir != "none" else {"groups": [], "services": 0, "dir": None}
    ingress.collect(g)
    refs.collect(g)
    out = []
    if hp.get("dir"):
        out.append(f"Homepage config at {hp['dir']}: {len(hp['groups'])} groups in layout order"
                   + (" (used as zone order since [zones] order is not set)" if not cfg.zone_order else " ([zones] order in wharfmap.toml wins)")
                   + f", {hp['services']} services.yaml entries matched to containers")
        out.append("")
    hn = [c for c in g.containers if c["hostnet"]]
    if hn:
        out.append("Host-network containers (Docker can't see their ports; source -> ports):")
        for c in sorted(hn, key=lambda c: c["name"]):
            ports = " ".join(str(p["host_port"]) for p in c["ports"]) or "-"
            note = {"config": "", "proc": "  (read from the container's sockets)", "known": "  (known-app default; override in [host_net_ports] if wrong)",
                    "expose": "  (image EXPOSE; may be generous)", "none": "  <- unknown: add to [host_net_ports]"}[c.get("ports_source", "none")]
            out.append(f"  {c['name']:24} {c.get('ports_source','none'):7} {ports}{note}")
    out.append("")
    out.append("Config files read for wiring (auto = known app; api = fetched through the Docker API, no mount needed):")
    for r in g.refs_used:
        flag = ("auto  " if r["auto"] else "toml  ") + ("api " if r.get("how") == "docker-api" else "    ")
        state = f"{r['files']} file(s), {r['edges']} edge(s)" if r["files"] else "NO FILES MATCHED"
        out.append(f"  {r['container']:24} {r['type']:14} {flag} {state}  {r['glob']}")
    if not g.refs_used:
        out.append("  none")
    unresolved = [(h["domains"][0] + ("" if h.get("enabled", True) else "  (disabled)"), t["upstream"]) for h in g.ingress for t in h["targets"] if not t["container"]]
    if unresolved:
        out.append("")
        out.append("Proxy hosts whose upstream is not a container here (fine if it's another machine; otherwise a port is missing above):")
        for d, u in unresolved:
            out.append(f"  {d:40} -> {u}")
    groups = sorted({c["group"] for c in g.containers if c["group"] and c["group"] not in cfg.zone_of_group})
    if groups and not hp.get("groups"):
        out.append("")
        out.append('Label groups not mapped to a zone (they land in "Other"; add them under [zones.groups]):')
        out += [f"  {x}" for x in groups]
    lan = sorted(ip for ip in {e["target"][4:] for e in g.edges if e["target"].startswith("lan:")} if ip not in g.lan_labels)
    if lan:
        out.append("")
        out.append("LAN hosts referenced but unnamed (optional: name them under [lan_hosts]):")
        out += [f"  {ip}" for ip in lan]
    ghosts = [c for c in g.containers if c.get("ghost")]
    if ghosts:
        out.append("")
        out.append("Compose services with no container (shown as 'not created' when stopped containers are on):")
        for c in ghosts:
            out.append(f"  {c['stack']:24} {c['service']:24} {c['desc']}")
    if g.compose_unreachable:
        out.append("")
        out.append("Compose files Wharfmap can't read (mount them read-only at the same path to see undeployed services):")
        out += [f"  {p}" for p in sorted(g.compose_unreachable)]
    hosts = sum(1 for h in g.ingress)
    out.append("")
    out.append(f"{len(g.containers) - len(ghosts)} containers, {len(g.networks)} networks, {hosts} proxy hosts, {len(g.edges)} edges. "
               f"Ingress provider: {'none' if not hosts else 'npm-api' if any(h.get('cert') is not None or h.get('acl') for h in g.ingress) else cfg.ingress_provider}.")
    print("\n".join(out))
    return 0


def cmd_init(args) -> int:
    cfg = config.Config()
    from .discover import discover
    g = discover(cfg)
    hostnet = [c["name"] for c in g.containers if c["hostnet"]]
    groups = sorted({c["group"] for c in g.containers if c["group"]})
    print(f'''title = "{cfg.host_name} map"

[host]
name = "{cfg.host_name}"
ip = "{cfg.host_ip}"
aliases = ["{cfg.host_name}.lan"]       # other names containers use for this host
# lan_cidrs = ["10.0.0.0/24"]           # default: the /24 around host.ip

[output]
dir = "out"
every = 300                             # serve: regenerate every N seconds (0 = only on POST /regenerate)

# Docker cannot see what a host-network container listens on. List them here.
[host_net_ports]
{chr(10).join(f'{n} = []' for n in hostnet) or '# homeassistant = [8123]'}

# Friendly names for other machines on the LAN that show up in env vars / configs.
[lan_hosts]
# "10.0.0.24" = "ADS-B feeder Pi"

[ingress]
provider = "auto"                       # auto | npm | traefik | none
# container = "npmplus"                 # npm: container to read rendered configs from
public_suffixes = []                    # e.g. [".example.com"] -> those domains are marked public

# Page layout: homepage.group / wharfmap.group labels -> zone bands, in this order.
[zones]
order = {json.dumps(build.DEFAULT_ZONE_ORDER)}
[zones.groups]
{chr(10).join(f'"{g}" = "Other"' for g in groups) or '# "Media Services" = "Media & downloads"'}

# Wiring that lives in app config files rather than env vars (optional).
# [[config_refs]]
# container = "frigate"
# type = "frigate"
# glob = "/opt/stacks/iot/frigate/config.yml"
''')
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="wharfmap", description="Chart a Docker host.")
    ap.add_argument("-c", "--config", help="wharfmap.toml (default: ./wharfmap.toml if present)")
    ap.add_argument("-o", "--out", help="output directory (overrides config)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)  # so flags work before or after the subcommand
    common.add_argument("-c", "--config", dest="config", default=argparse.SUPPRESS)
    common.add_argument("-o", "--out", dest="out", default=argparse.SUPPRESS)
    sub.add_parser("generate", parents=[common], help="write graph.json + index.html").set_defaults(fn=cmd_generate)
    s = sub.add_parser("serve", parents=[common], help="generate and serve the output directory")
    # The image sets WHARFMAP_BIND=0.0.0.0 and WHARFMAP_OUT=/out, so a bare `command: serve` works in a container.
    s.add_argument("--bind", default=os.environ.get("WHARFMAP_BIND", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("WHARFMAP_PORT", "3008")))
    s.add_argument("--every", type=int, default=None, help="regenerate every N seconds; overrides [output] every (default 300, 0 = only on POST /regenerate)")
    s.set_defaults(fn=cmd_serve)
    a = sub.add_parser("agent", parents=[common], help="discover this host and POST the graph to a wharfmap server (agent mode)")
    a.add_argument("--url", default="", help="the server, e.g. http://wharfmap.lan or http://10.0.0.10:3008 (or [report] url / WHARFMAP_REPORT_URL)")
    a.add_argument("--every", type=int, default=None, help="report every N seconds (default: [output] every, 300; minimum 30)")
    a.add_argument("--once", action="store_true", help="report once and exit (exit 1 if the server did not take it)")
    a.set_defaults(fn=cmd_agent)
    r = sub.add_parser("render", parents=[common], help="render index.html from an existing graph.json (no Docker needed)")
    r.add_argument("-i", "--input", required=True, help="graph.json to render")
    r.set_defaults(fn=cmd_render)
    sub.add_parser("doctor", parents=[common], help="show what discovery figured out on its own and what still needs a line of config").set_defaults(fn=cmd_doctor)
    sub.add_parser("init", parents=[common], help="print a starter wharfmap.toml for this host").set_defaults(fn=cmd_init)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
