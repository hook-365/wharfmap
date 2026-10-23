"""Configuration: a small TOML file, every key optional.

Wharfmap discovers almost everything from Docker. The config only covers what Docker
cannot see (host-network listen ports), what it cannot name (other LAN machines), and
what is a matter of taste (zones, public domain suffixes, extra config-file parsers).
"""
from __future__ import annotations

import ipaddress
import os
import pathlib
import socket
import tomllib
from dataclasses import dataclass, field


def looks_like_container_id(name: str) -> bool:
    return name == name.lower() and len(name) in (12, 64) and all(ch in "0123456789abcdef" for ch in name)


def _default_host_name() -> str:
    """The machine's hostname. Inside a container that is the container id, but the Docker daemon
    knows the real one (`docker info` Name), so ask it before falling back."""
    name = socket.gethostname()
    if looks_like_container_id(name):
        try:
            import subprocess
            real = subprocess.run(["docker", "info", "--format", "{{.Name}}"], capture_output=True, text=True,
                                  timeout=10).stdout.strip()
            if real and not looks_like_container_id(real):
                return real
        except (OSError, subprocess.SubprocessError):
            pass
    return name


def _default_host_ip() -> str:
    """Best-effort LAN IP of this machine (the address other hosts use to reach it)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # no packets are sent; picks the default route's source IP
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


@dataclass
class ConfigRef:
    """A config-file parser. `type` is one of regex | frigate | homeassistant | prometheus."""
    container: str
    type: str = "regex"
    glob: str = ""
    regex: str = r"url:\s*['\"]?(\S+?)['\"]?\s*$"
    label: str = "config"
    kind: str = "host"  # how an unknown LAN target is classified: host | device
    auto: bool = False  # set on refs derived from the known-apps table
    cpath: str = ""     # path inside the container (auto refs); read via the Docker API when the host path is not reachable


@dataclass
class Config:
    title: str = "Wharfmap"
    host_name: str = field(default_factory=_default_host_name)
    host_ip: str = field(default_factory=_default_host_ip)
    host_ip_auto: bool = True       # ip was guessed (inside a container that guess is a Docker bridge address)
    host_aliases: list[str] = field(default_factory=list)
    lan_cidrs: list[str] = field(default_factory=list)  # defaults to the /24 around host_ip
    output_dir: pathlib.Path = pathlib.Path("out")
    refresh_seconds: int = 300      # serve: regenerate this often (0 = only on POST /regenerate); the page polls at the same rate
    host_net_ports: dict[str, list[int]] = field(default_factory=dict)
    lan_hosts: dict[str, str] = field(default_factory=dict)
    ingress_provider: str = "auto"  # auto | npm | npm-api | traefik | none
    ingress_container: str = ""     # npm: container to `docker exec` (default: autodetect npmplus/npm)
    ingress_conf_dir: str = ""      # npm: read rendered configs from this dir (a bind mount) instead of docker exec
    ingress_api_url: str = ""       # npm-api: e.g. https://127.0.0.1:81 (credentials come from NPM_TOKEN or NPM_EMAIL/NPM_PASSWORD env)
    ingress_api_verify_tls: bool = True
    public_suffixes: list[str] = field(default_factory=list)
    zone_order: list[str] = field(default_factory=list)
    zone_of_group: dict[str, str] = field(default_factory=dict)
    config_refs: list[ConfigRef] = field(default_factory=list)
    homepage_config_dir: str = ""   # Homepage config dir (auto: the Homepage container's /app/config mount); "" = auto, "none" = off
    ghosts: bool = True             # show compose services that are defined but have no container
    auto_refs: bool = True          # read known apps' config files via their bind mounts without being told where
    credit: bool = True             # the small "GitHub" link to the Wharfmap project in the header; credit = false hides it
    links: dict[str, str] = field(default_factory=dict)  # footer links, e.g. "buy me coffee" = "https://buymeacoffee.com/you"
    env_skip_keys: list[str] = field(default_factory=lambda: ["PATH", "HOME", "PUID", "PGID", "TZ"])
    report_url: str = ""            # agent: the server to POST this host's graph to (also WHARFMAP_REPORT_URL); token from WHARFMAP_TOKEN

    # ---- derived helpers ----
    def host_names(self) -> set[str]:
        return {"127.0.0.1", "localhost", "host.docker.internal", self.host_ip, self.host_name,
                self.host_name.lower(), *self.host_aliases}

    def lan_networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        cidrs = self.lan_cidrs or [str(ipaddress.ip_network(f"{self.host_ip}/24", strict=False))]
        return [ipaddress.ip_network(c, strict=False) for c in cidrs]

    def is_lan_ip(self, s: str) -> bool:
        try:
            ip = ipaddress.ip_address(s)
        except ValueError:
            return False
        return s != self.host_ip and any(ip in n for n in self.lan_networks())

    def is_public_domain(self, d: str) -> bool:
        return any(d == s.lstrip(".") or d.endswith(s if s.startswith(".") else "." + s) for s in self.public_suffixes)


def load(path: str | pathlib.Path | None) -> Config:
    cfg = Config()
    _env_overrides(cfg)
    if not path:
        return cfg
    p = pathlib.Path(path)
    if p.is_dir():
        # Docker creates an empty directory when a bind-mounted file does not exist on the host.
        raise SystemExit(f"{p} is a directory, not a config file. If it is a Docker bind mount, create the file "
                         f"first (wharfmap init > wharfmap.toml) or remove that volume line; the file is optional.")
    if not p.exists():
        raise SystemExit(f"config not found: {p}")
    with p.open("rb") as f:
        raw = tomllib.load(f)
    cfg.title = raw.get("title", cfg.title)
    host = raw.get("host", {})
    cfg.host_name = host.get("name", cfg.host_name)
    if "ip" in host:
        cfg.host_ip, cfg.host_ip_auto = host["ip"], False
    cfg.host_aliases = list(host.get("aliases", []))
    cfg.lan_cidrs = list(host.get("lan_cidrs", []))
    cfg.output_dir = pathlib.Path(raw.get("output", {}).get("dir", cfg.output_dir))
    cfg.refresh_seconds = int(raw.get("output", {}).get("every", cfg.refresh_seconds))
    cfg.host_net_ports = {k: [int(x) for x in v] for k, v in raw.get("host_net_ports", {}).items()}
    cfg.lan_hosts = dict(raw.get("lan_hosts", {}))
    ing = raw.get("ingress", {})
    cfg.ingress_provider = ing.get("provider", cfg.ingress_provider)
    cfg.ingress_container = ing.get("container", cfg.ingress_container)
    cfg.ingress_conf_dir = ing.get("conf_dir", cfg.ingress_conf_dir)
    cfg.ingress_api_url = ing.get("api_url", cfg.ingress_api_url)
    cfg.ingress_api_verify_tls = bool(ing.get("api_verify_tls", cfg.ingress_api_verify_tls))
    cfg.public_suffixes = list(ing.get("public_suffixes", []))
    zones = raw.get("zones", {})
    cfg.zone_order = list(zones.get("order", []))
    cfg.zone_of_group = dict(zones.get("groups", {}))
    cfg.config_refs = [ConfigRef(**r) for r in raw.get("config_refs", [])]
    cfg.auto_refs = bool(raw.get("auto_refs", cfg.auto_refs))
    cfg.ghosts = bool(raw.get("ghosts", cfg.ghosts))
    cfg.homepage_config_dir = str(raw.get("homepage", {}).get("config_dir", cfg.homepage_config_dir))
    cfg.links = {str(k): str(v) for k, v in raw.get("links", {}).items()}
    cfg.credit = bool(raw.get("credit", cfg.credit))
    cfg.env_skip_keys = list(raw.get("env", {}).get("skip_keys", cfg.env_skip_keys))
    cfg.report_url = str(raw.get("report", {}).get("url", cfg.report_url))
    return cfg


def _env_overrides(cfg: Config) -> None:
    """Inside a container the hostname is a random id and the default route is a Docker bridge, so an
    agent with no config file needs WHARFMAP_HOST / WHARFMAP_HOST_IP. A wharfmap.toml [host] still wins."""
    if os.environ.get("WHARFMAP_HOST"):
        cfg.host_name = os.environ["WHARFMAP_HOST"]
    if os.environ.get("WHARFMAP_HOST_IP"):
        cfg.host_ip, cfg.host_ip_auto = os.environ["WHARFMAP_HOST_IP"], False
    if os.environ.get("WHARFMAP_REPORT_URL"):
        cfg.report_url = os.environ["WHARFMAP_REPORT_URL"]
