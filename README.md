# Wharfmap

**Chart a Docker host.** Wharfmap discovers every container, network, published port,
reverse-proxy domain and the wiring between them straight from the Docker engine, and
renders it as a single self-contained HTML page you can actually read.

No agents, no database, no graph library, no dependencies beyond Python 3.11 and the
`docker` CLI. It is not a dashboard and it does not replace one; it is the map that
your dashboard, your compose files and your notes never quite add up to.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshot-dark.jpg">
  <img alt="Wharfmap showing a media stack and a VPN-namespaced download stack with env-var wiring drawn" src="docs/screenshot-light.jpg">
</picture>

*The demo data above is fictional (`examples/demo/`). Try the viewer without Docker:*
`python -m wharfmap render -i examples/demo/graph.json -o out && open out/index.html`

## What it finds, with zero configuration

| Source | Yields |
|---|---|
| `docker inspect` on every container | stacks (compose project), service names, status and health, image, networks and IPs, published ports with the host interface they bind to, `network_mode: service:` sharing, `depends_on` |
| container environment | runtime references: an env var whose value names another container, or `host:port` on this host, becomes an edge (`SONARR_URL=http://sonarr:8989`) |
| `docker network inspect` | subnets, gateways, dynamic IP ranges, membership |
| `homepage.*` / `wharfmap.*` labels | display name, description, group, link |
| Nginx Proxy Manager / NPMPlus (rendered configs, or the API if you give it credentials) or Traefik (router labels) | which domain lands on which container, LAN-only versus public; with the API also enabled flags, ACL names, certificate expiry, redirects and streams |

The page shows zones of stacks of containers at real text size. Containers sharing a
network namespace (the VPN pattern) are nested inside their parent. Host-network
containers, LAN-open ports, public domains and Docker network membership are badges.
Click anything to see its ports, networks, domains and every relationship, and to draw
its wiring. Double-click to isolate it. Search matches names, images, ports, IPs and
domains. Light and dark themes follow your system, or click the toggle in the header.

## Quick start

```bash
pipx install git+https://github.com/hook-365/wharfmap   # or: pip install git+..., or run from a clone with python -m wharfmap
wharfmap generate               # writes out/graph.json and out/index.html
wharfmap serve --port 3008      # same, then serves out/ (POST /regenerate to refresh)
```

Or as a container, next to the things it maps:

```yaml
services:
  wharfmap:
    image: ghcr.io/hook-365/wharfmap:latest
    ports: ["127.0.0.1:3008:3008"]
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./wharfmap.toml:/app/wharfmap.toml:ro
```

A read-only Docker socket proxy works too: set `DOCKER_HOST=tcp://dockerproxy:2375`
and give the proxy `CONTAINERS=1 NETWORKS=1`. Then use `conf_dir` for NPM (below)
instead of `docker exec`.

**Put a reverse proxy with an allow-list in front of it.** The page is a complete port
and topology inventory of your host. Wharfmap binds to localhost by default for that
reason.

## Configuration

Run `wharfmap init > wharfmap.toml` to get a starter file populated from your host.
Every key is optional. The things Docker cannot tell us are:

```toml
title = "Harbor Homelab Map"

[host]
name = "harbor"
ip = "10.0.0.10"           # how other machines reach this host; auto-detected if omitted
aliases = ["harbor.lan"]       # other names containers use for this host
lan_cidrs = ["10.0.0.0/24"] # what counts as "another LAN machine" (default: the /24 around ip)

[host_net_ports]               # network_mode: host containers have no port mappings
homeassistant = [8123]
plex = [32400]

[lan_hosts]                    # friendly names for LAN IPs that show up in env vars / configs
"10.0.0.24" = "ADS-B feeder Pi"

[ingress]
provider = "auto"              # auto | npm | npm-api | traefik | none
container = "npmplus"          # npm: container to docker exec into, or...
conf_dir = "/npm/proxy_host"   # ...a bind mount of its data/nginx/proxy_host (no exec needed)
api_url = "https://127.0.0.1:81"   # optional: NPM admin API for enabled flags, ACL names, cert expiry, redirects, streams
api_verify_tls = false         # NPM's :81 usually has a self-signed cert
public_suffixes = [".example.com"]

[zones]                        # page bands, mapped from homepage.group / wharfmap.group labels
order = ["Edge & infrastructure", "Media", "Home & IoT", "Ops"]
[zones.groups]
"Security Services" = "Edge & infrastructure"
"Media Services" = "Media"

[links]                        # footer links; anything mentioning coffee/ko-fi/sponsor gets the accent style
"buy me coffee" = "https://buymeacoffee.com/you"

[[config_refs]]                # wiring that only exists in app config files
container = "frigate"
type = "frigate"               # regex | frigate | homeassistant | prometheus
glob = "/opt/stacks/iot/frigate/config.yml"

[[config_refs]]
container = "grafana"
type = "regex"
glob = "/opt/stacks/monitoring/grafana/provisioning/datasources/*.yml"
regex = 'url:\s*(\S+)'
label = "datasource"
```

See [`examples/wharfmap.example.toml`](examples/wharfmap.example.toml) for a complete
config modelled on the ~110-container homelab Wharfmap was built on.

### NPM: files by default, API when you want more

The default NPM reader needs no credentials: it parses the nginx configs NPM has rendered,
which gives domains, upstreams, and whether an access list or auth is attached. That is
enough for the map.

Set `api_url` and put credentials in the **environment** (never in the config file) and
`auto` upgrades itself to the API, which adds per domain: enabled/disabled, the access
list's name and allowed ranges, certificate provider and expiry, plus redirection hosts
and TCP/UDP streams.

```
NPM_TOKEN=...                      # a pre-issued token, or
NPM_EMAIL=... NPM_PASSWORD=...     # to log in (works for vanilla NPM and NPMPlus)
```

If the API is unreachable or the login fails, Wharfmap logs one line and falls back to the
file reader, so turning the API on can only add information. Certificate warnings are
relative to each cert's lifetime (under a third remaining), because Let's Encrypt now
issues 6-day certificates alongside 90-day ones and a fixed "14 days left" rule would
flag every short-lived cert all the time.

### Labels

Wharfmap reads [Homepage](https://gethomepage.dev) labels so most people get names
and groups for free. To tune the map without touching your dashboard, use the same
keys under `wharfmap.`: `wharfmap.name`, `wharfmap.group`, `wharfmap.description`,
`wharfmap.href`. The `wharfmap.` key wins when both exist.

## Secrets

Nothing from env var *values* is written out except a port number or a container
name that matched. URLs in config files are reduced to host and port; userinfo is
discarded. `generate` warns if an edge label looks like it carries a credential.
Still: the output is an inventory of your attack surface. Keep it on the LAN.

## Known limits

- Ports of host-network containers must be listed by hand (`host_net_ports`).
- Traefik routers from the file provider are invisible (they are not on any container).
- Links stored only in application databases (the *arr apps' download clients, Grafana
  datasources added in the UI) are not found. Provisioning files are.
- Ingress targets that are not containers (another machine) show as LAN hosts.

## Development

```bash
git clone https://github.com/hook-365/wharfmap && cd wharfmap
python -m wharfmap generate -c examples/wharfmap.example.toml -o /tmp/out   # against your own Docker host
python -m wharfmap render -i examples/demo/graph.json -o /tmp/out            # the fictional demo, no Docker needed
python examples/demo/make_demo.py                                           # regenerate the demo data
```

`wharfmap/viewer.html` is the whole front end. It keeps `/*__DATA__*/null` as the
substitution anchor; `graph.json` is spliced in at build time so the page works from
`file://` as well as over HTTP.

## License

MIT.
