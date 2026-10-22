"""wharfmap: chart a Docker host.

  wharfmap generate [-c wharfmap.toml] [-o out]      write out/graph.json + out/index.html
  wharfmap serve    [-c ...] [--port 3008] [--every 300]   generate, then serve the output directory
  wharfmap render -i graph.json [-o out]            index.html from a saved graph.json (no Docker needed)
  wharfmap init                                      print a starter config built from this host
"""
from __future__ import annotations

import argparse
import functools
import http.server
import json
import pathlib
import sys
import threading
import time

from . import build, config


def _cfg(args) -> config.Config:
    path = args.config
    if path is None and pathlib.Path("wharfmap.toml").exists():
        path = "wharfmap.toml"
    cfg = config.load(path)
    if args.out:
        cfg.output_dir = pathlib.Path(args.out)
    return cfg


def cmd_generate(args) -> int:
    cfg = _cfg(args)
    graph = build.build(cfg)
    out = build.write(cfg, graph)
    print(f"{len(graph['containers'])} containers, {len(graph['networks'])} networks, "
          f"{len(graph['ingress'])} proxy hosts, {len(graph['edges'])} edges -> {out / 'index.html'}")
    for h in build.secret_scan(graph):
        print("WARNING possible credential in output:", h, file=sys.stderr)
    return 0


def cmd_serve(args) -> int:
    cfg = _cfg(args)

    def regen():
        try:
            build.write(cfg, build.build(cfg))
            print(time.strftime("%H:%M:%S"), "regenerated", flush=True)
        except Exception as e:  # keep serving the last good copy
            print("regenerate failed:", e, file=sys.stderr, flush=True)

    regen()
    if args.every:
        def loop():
            while True:
                time.sleep(args.every)
                regen()
        threading.Thread(target=loop, daemon=True).start()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_POST(self):
            if self.path.rstrip("/") == "/regenerate":
                regen()
                self.send_response(204)
                self.end_headers()
            else:
                self.send_error(404)

        def log_message(self, *a):
            pass

    handler = functools.partial(Handler, directory=str(cfg.output_dir))
    srv = http.server.ThreadingHTTPServer((args.bind, args.port), handler)
    print(f"serving {cfg.output_dir} on http://{args.bind}:{args.port}  (POST /regenerate to refresh)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_render(args) -> int:
    """index.html from a saved graph.json: demos, screenshots, or viewing a graph from another host."""
    graph = json.loads(pathlib.Path(args.input).read_text())
    out = pathlib.Path(args.out or "out")
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(build.render(graph))
    (out / "graph.json").write_text(json.dumps(graph, indent=1))
    print(f"rendered {args.input} -> {out / 'index.html'}")
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
    common.add_argument("-c", "--config", dest="config")
    common.add_argument("-o", "--out", dest="out")
    sub.add_parser("generate", parents=[common], help="write graph.json + index.html").set_defaults(fn=cmd_generate)
    s = sub.add_parser("serve", parents=[common], help="generate and serve the output directory")
    s.add_argument("--bind", default="127.0.0.1")
    s.add_argument("--port", type=int, default=3008)
    s.add_argument("--every", type=int, default=0, help="regenerate every N seconds (0 = only on POST /regenerate)")
    s.set_defaults(fn=cmd_serve)
    r = sub.add_parser("render", parents=[common], help="render index.html from an existing graph.json (no Docker needed)")
    r.add_argument("-i", "--input", required=True, help="graph.json to render")
    r.set_defaults(fn=cmd_render)
    sub.add_parser("init", parents=[common], help="print a starter wharfmap.toml for this host").set_defaults(fn=cmd_init)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
