"""Known applications: what Docker can't tell us but the image name gives away.

Each entry is matched against the image name (and, as a fallback, the container name).
  ports   default listen ports for `network_mode: host` containers (Docker shows none for those)
  refs    config files worth reading for wiring: (parser type, path inside the container, label).
          The path is translated to a host path through the container's bind mounts, so there is
          nothing to configure as long as the config is a bind mount and Wharfmap can read it.

Overrides and additions live in wharfmap.toml ([host_net_ports], [[config_refs]]). To add an app
here, keep the match specific to the image (it is a regex, case-insensitive) and only list ports
the app actually listens on by default. Pull requests welcome.
"""
from __future__ import annotations

import re

KNOWN_APPS: list[dict] = [
    {"name": "Home Assistant", "match": r"home-assistant/home-assistant|homeassistant/home-assistant|(^|/)home-?assistant(:|$)", "ports": [8123],
     "refs": [("homeassistant", "/config/.storage/core.config_entries", "integration")]},
    {"name": "Frigate", "match": r"frigate", "ports": [5000, 8554, 8555],
     "refs": [("frigate", "/config/config.yml", "frigate"), ("frigate", "/config/config.yaml", "frigate")]},
    {"name": "Plex", "match": r"plex", "ports": [32400]},
    {"name": "Jellyfin", "match": r"jellyfin", "ports": [8096]},
    {"name": "Emby", "match": r"emby", "ports": [8096]},
    {"name": "Music Assistant", "match": r"music-assistant|musicassistant", "ports": [8095]},
    {"name": "AdGuard Home", "match": r"adguardhome|adguard/", "ports": [53, 80, 3000]},
    {"name": "Pi-hole", "match": r"pihole", "ports": [53, 80]},
    {"name": "Technitium DNS", "match": r"technitium", "ports": [53, 5380]},
    {"name": "Unbound", "match": r"unbound", "ports": [53]},
    {"name": "ESPHome", "match": r"esphome", "ports": [6052]},
    {"name": "Netdata", "match": r"netdata", "ports": [19999]},
    {"name": "Nginx Proxy Manager", "match": r"nginx-proxy-manager|npmplus|jc21/nginx", "ports": [80, 443, 81]},
    {"name": "Traefik", "match": r"traefik", "ports": [80, 443, 8080],
     "refs": [("regex", "/etc/traefik/dynamic/*.y*ml", "traefik dynamic"), ("regex", "/etc/traefik/conf.d/*.y*ml", "traefik dynamic")]},
    {"name": "Caddy", "match": r"caddy", "ports": [80, 443, 2019]},
    {"name": "CrowdSec", "match": r"crowdsec", "ports": [8080]},
    {"name": "WireGuard", "match": r"wireguard|wg-easy", "ports": [51820]},
    {"name": "Tailscale", "match": r"tailscale", "ports": []},
    {"name": "Samba / Time Machine", "match": r"samba|timemachine|time-machine", "ports": [445]},
    {"name": "Mosquitto", "match": r"mosquitto", "ports": [1883, 9001]},
    {"name": "Zigbee2MQTT", "match": r"zigbee2mqtt", "ports": [8080]},
    {"name": "Node-RED", "match": r"node-red|nodered", "ports": [1880]},
    {"name": "Scrypted", "match": r"scrypted", "ports": [10443, 11080]},
    {"name": "UniFi", "match": r"unifi", "ports": [8443, 8080]},
    {"name": "Omada", "match": r"omada", "ports": [8043, 8088]},
    {"name": "Grafana", "match": r"grafana/grafana(-oss|-enterprise)?(:|$)", "ports": [3000],
     "refs": [("regex", "/etc/grafana/provisioning/datasources/*.y*ml", "datasource")]},
    {"name": "Prometheus", "match": r"prom/prometheus|prometheus:", "ports": [9090],
     "refs": [("prometheus", "/etc/prometheus/prometheus.yml", "scrape")]},
    {"name": "Alloy", "match": r"grafana/alloy", "ports": [12345],
     "refs": [("regex", "/etc/alloy/*.alloy", "push url", r'url\s*=\s*"([^"]+)"')]},
    {"name": "Promtail", "match": r"grafana/promtail", "ports": [9080],
     "refs": [("regex", "/etc/promtail/*.y*ml", "promtail push")]},
    {"name": "Loki", "match": r"grafana/loki", "ports": [3100]},
    {"name": "Uptime Kuma", "match": r"uptime-kuma", "ports": [3001]},
    {"name": "Homepage", "match": r"gethomepage/homepage", "ports": [3000],
     "refs": [("regex", "/app/config/services.yaml", "homepage widget", r"(?:href|url):\s*['\"]?(\S+?)['\"]?\s*$")]},
    {"name": "Vaultwarden", "match": r"vaultwarden", "ports": [80]},
    {"name": "Gitea / Forgejo", "match": r"gitea|forgejo", "ports": [3000, 22]},
    {"name": "Nextcloud", "match": r"nextcloud", "ports": [80, 443]},
    {"name": "Immich", "match": r"immich-server", "ports": [2283]},
    {"name": "AudiobookShelf", "match": r"audiobookshelf", "ports": [80]},
    {"name": "Transmission", "match": r"transmission", "ports": [9091]},
    {"name": "qBittorrent", "match": r"qbittorrent", "ports": [8080]},
    {"name": "Sonarr", "match": r"sonarr", "ports": [8989]},
    {"name": "Radarr", "match": r"radarr", "ports": [7878]},
    {"name": "Lidarr", "match": r"lidarr", "ports": [8686]},
    {"name": "Prowlarr", "match": r"prowlarr", "ports": [9696]},
    {"name": "Bazarr", "match": r"bazarr", "ports": [6767]},
    {"name": "Seerr / Overseerr / Jellyseerr", "match": r"seerr", "ports": [5055]},
    {"name": "Portainer", "match": r"portainer", "ports": [9000, 9443]},
    {"name": "Dockge", "match": r"dockge", "ports": [5001]},
    {"name": "What's Up Docker", "match": r"getwud/wud|fmartinou/whats-up-docker", "ports": [3000]},
    {"name": "Ollama", "match": r"ollama", "ports": [11434]},
    {"name": "Open WebUI", "match": r"open-webui", "ports": [8080]},
    {"name": "Wyoming Whisper", "match": r"wyoming-whisper|wyoming-faster-whisper", "ports": [10300]},
    {"name": "Wyoming Piper", "match": r"wyoming-piper", "ports": [10200]},
    {"name": "Syncthing", "match": r"syncthing", "ports": [8384, 22000]},
    {"name": "Paperless-ngx", "match": r"paperless", "ports": [8000]},
    {"name": "Wharfmap", "match": r"wharfmap", "ports": [3008]},
]

_COMPILED = [(re.compile(a["match"], re.I), a) for a in KNOWN_APPS]


def known_app(image: str, name: str = "") -> dict | None:
    """First known app whose pattern matches the image. Compose-built images are named
    <project>-<service> and say nothing useful, so the container name is tried as a whole-word fallback."""
    for rx, app in _COMPILED:
        if rx.search(image):
            return app
    for rx, app in _COMPILED:
        if name and rx.fullmatch(name):
            return app
    return None
