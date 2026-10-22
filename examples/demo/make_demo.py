#!/usr/bin/env python3
"""Build examples/demo/graph.json: a fictional but realistic homelab ("harbor") used for the README
screenshot and for trying the viewer without Docker:  wharfmap render -i examples/demo/graph.json
Nothing here is real. Regenerate with:  python3 examples/demo/make_demo.py
"""
import json, pathlib, random

random.seed(7)
HOST = "harbor"; IP = "10.0.0.10"
NETS = {"seg_proxy": "10.90.0.0/24", "seg_media": "10.91.0.0/24", "seg_home": "10.92.0.0/24",
        "seg_monitoring": "10.93.0.0/24", "dockersock": "10.94.0.0/24", "downloads_vpn": "10.95.0.0/24", "ai_net": "10.96.0.0/24"}
containers, edges, ingress = [], [], []
ipn = {n: 2 for n in NETS}

def C(name, stack, group, title=None, image=None, nets=(), ports=(), hostnet=False, netns=None, deps=(), desc="", href=None, status="running", health=None, exposed=()):
    ips = {}
    for n in nets:
        ips[n] = NETS[n].rsplit(".", 1)[0] + f".{ipn[n]}"; ipn[n] += 1
    c = {"id": name, "name": name, "stack": stack, "service": name, "status": status, "health": health,
         "image": image or f"ghcr.io/example/{name}:latest", "networks": list(nets), "ips": ips,
         "ports": [{"host_ip": hi, "host_port": hp, "cport": cp, "proto": "tcp", "owner": name} for hi, hp, cp in ports],
         "hostnet": hostnet, "netns": netns, "depends_on": list(deps), "title": title or name.replace("-", " ").title(),
         "group": group, "desc": desc, "href": href, "created": "2026-03-14"}
    containers.append(c); return c

def E(s, t, kind, label=""): edges.append({"source": s, "target": t, "kind": kind, "label": label})
def H(domains, container, port, public=True, internal=False, cert=True, acl=None, enabled=True, days=61, lifetime=90):
    ingress.append({"file": f"proxy-host:{len(ingress)+1}", "id": len(ingress)+1, "kind": "proxy", "domains": domains,
                    "upstreams": [f"127.0.0.1:{port}"], "targets": [{"upstream": f"127.0.0.1:{port}", "container": container}],
                    "internal": internal, "auth": bool(acl and "auth" in acl), "public": public and not internal, "enabled": enabled,
                    "acl": {"name": acl, "clients": ["10.0.0.0/24"]} if acl else None,
                    "cert": {"name": domains[0], "provider": "letsencrypt", "expires_on": "2026-12-20", "days": days, "lifetime": lifetime, "warn": days < lifetime / 3} if cert else None,
                    "ssl_forced": cert, "error": ""})
    E("dom:" + domains[0], container, "ingress", f"127.0.0.1:{port}")

LO = "127.0.0.1"
# edge & infrastructure
C("npm", "edge", "Edge", "Nginx Proxy Manager", "jc21/nginx-proxy-manager:latest", hostnet=True, ports=[("0.0.0.0", 80, 80), ("0.0.0.0", 443, 443), ("0.0.0.0", 81, 81)], desc="Reverse proxy + Let's Encrypt")
C("crowdsec", "edge", "Edge", "CrowdSec", "crowdsecurity/crowdsec:latest", hostnet=True, ports=[("0.0.0.0", 8080, 8080)], desc="Behaviour-based IPS")
C("adguard", "core", "Core", "AdGuard Home", "adguard/adguardhome:latest", hostnet=True, ports=[("0.0.0.0", 53, 53), ("0.0.0.0", 3000, 3000)], desc="DNS + DHCP")
C("wireguard", "core", "Core", "WireGuard", "lscr.io/linuxserver/wireguard:latest", hostnet=True, ports=[("0.0.0.0", 51820, 51820)], desc="Road-warrior VPN")
# sites
C("blog", "sites", "Sites", "Blog", "nginx:alpine", nets=("seg_proxy",), ports=[(LO, 8180, 80)], desc="Hugo site")
C("nextcloud", "sites", "Sites", "Nextcloud", "nextcloud:29", nets=("seg_proxy",), ports=[(LO, 11000, 80)], deps=["nextcloud-db", "nextcloud-redis"])
C("nextcloud-db", "sites", "Sites", "Nextcloud DB", "postgres:16", nets=("seg_proxy",))
C("nextcloud-redis", "sites", "Sites", "Nextcloud Redis", "redis:7", nets=("seg_proxy",))
C("vaultwarden", "sites", "Sites", "Vaultwarden", "vaultwarden/server:latest", nets=("seg_proxy",), ports=[(LO, 8222, 80)])
# home & iot
C("homeassistant", "home", "Home", "Home Assistant", "ghcr.io/home-assistant/home-assistant:stable", hostnet=True, ports=[("0.0.0.0", 8123, 8123)], href="http://ha.lan")
C("mosquitto", "home", "Home", "Mosquitto", "eclipse-mosquitto:2", nets=("seg_home",), ports=[("0.0.0.0", 1883, 1883)])
C("zigbee2mqtt", "home", "Home", "Zigbee2MQTT", "koenkk/zigbee2mqtt:latest", nets=("seg_home",), ports=[(LO, 8081, 8080)], deps=["mosquitto"])
C("frigate", "home", "Home", "Frigate", "ghcr.io/blakeblackshear/frigate:stable", nets=("seg_home",), ports=[(LO, 5000, 5000), ("0.0.0.0", 8554, 8554)], deps=["mosquitto"], desc="NVR with object detection")
C("esphome", "home", "Home", "ESPHome", "ghcr.io/esphome/esphome:latest", hostnet=True, ports=[("0.0.0.0", 6052, 6052)])
C("node-red", "home", "Home", "Node-RED", "nodered/node-red:latest", nets=("seg_home",), ports=[(LO, 1880, 1880)])
# media
C("plex", "media", "Media", "Plex", "plexinc/pms-docker:latest", hostnet=True, ports=[("0.0.0.0", 32400, 32400)])
C("immich", "media", "Media", "Immich", "ghcr.io/immich-app/immich-server:release", nets=("seg_media",), ports=[(LO, 2283, 2283)], deps=["immich-db", "immich-redis", "immich-ml"])
C("immich-ml", "media", "Media", "Immich ML", "ghcr.io/immich-app/immich-machine-learning:release", nets=("seg_media",))
C("immich-db", "media", "Media", "Immich DB", "tensorchord/pgvecto-rs:pg16", nets=("seg_media",))
C("immich-redis", "media", "Media", "Immich Redis", "redis:7", nets=("seg_media",))
C("audiobookshelf", "media", "Media", "AudiobookShelf", "ghcr.io/advplyr/audiobookshelf:latest", nets=("seg_media", "downloads_vpn"), ports=[(LO, 13378, 80)])
C("calibre-web", "media", "Media", "Calibre-Web", "lscr.io/linuxserver/calibre-web:latest", nets=("seg_media", "downloads_vpn"), ports=[(LO, 8083, 8083)])
# downloads (VPN netns)
C("vpn-client", "downloads", "Downloads", "VPN Gateway", "ghcr.io/example/wireguard-client:latest", nets=("downloads_vpn",),
  ports=[("0.0.0.0", 9091, 9091), (LO, 8989, 8989), (LO, 7878, 7878), (LO, 9696, 9696)], health="healthy", desc="All downloaders share this netns")
for n, t, img, port in [("transmission", "Transmission", "lscr.io/linuxserver/transmission", 9091), ("sonarr", "Sonarr", "lscr.io/linuxserver/sonarr", 8989),
                        ("radarr", "Radarr", "lscr.io/linuxserver/radarr", 7878), ("prowlarr", "Prowlarr", "lscr.io/linuxserver/prowlarr", 9696)]:
    C(n, "downloads", "Downloads", t, img + ":latest", netns="vpn-client", exposed=[port]); E(n, "vpn-client", "netns", "shares netns")
C("seerr", "downloads", "Downloads", "Seerr", "ghcr.io/seerr-team/seerr:latest", nets=("downloads_vpn",), ports=[(LO, 5055, 5055)], desc="Requests")
# ai
C("ollama", "ai", "AI", "Ollama", "ollama/ollama:latest", nets=("ai_net",), ports=[(LO, 11434, 11434)])
C("open-webui", "ai", "AI", "Open WebUI", "ghcr.io/open-webui/open-webui:main", nets=("ai_net",), ports=[(LO, 3005, 8080)], deps=["ollama"])
C("whisper", "ai", "AI", "Whisper STT", "rhasspy/wyoming-whisper:latest", nets=("ai_net",), ports=[("0.0.0.0", 10300, 10300)])
C("piper", "ai", "AI", "Piper TTS", "rhasspy/wyoming-piper:latest", nets=("ai_net",), ports=[("0.0.0.0", 10200, 10200)])
# monitoring
C("homepage", "monitoring", "Monitoring", "Homepage", "ghcr.io/gethomepage/homepage:latest", nets=("seg_monitoring", "dockersock"), ports=[("0.0.0.0", 3002, 3000)])
C("dockerproxy", "monitoring", "Monitoring", "Docker Socket Proxy", "ghcr.io/tecnativa/docker-socket-proxy:latest", nets=("dockersock",))
C("netdata", "monitoring", "Monitoring", "Netdata", "netdata/netdata:stable", hostnet=True, ports=[("0.0.0.0", 19999, 19999)])
C("loki", "monitoring", "Monitoring", "Loki", "grafana/loki:3.0.0", nets=("seg_monitoring",))
C("alloy", "monitoring", "Monitoring", "Alloy", "grafana/alloy:latest", nets=("seg_monitoring", "dockersock"), deps=["loki"])
C("grafana", "monitoring", "Monitoring", "Grafana", "grafana/grafana:latest", nets=("seg_monitoring",), ports=[(LO, 3001, 3000)], deps=["loki"])
C("wud", "monitoring", "Monitoring", "What's Up Docker", "getwud/wud:latest", nets=("dockersock",), ports=[(LO, 3003, 3000)])
C("wharfmap", "monitoring", "Monitoring", "Wharfmap", "ghcr.io/hook-365/wharfmap:latest", nets=("seg_monitoring", "dockersock"), ports=[(LO, 3008, 3008)], desc="This map")
C("old-dashboard", "monitoring", "Monitoring", "Old Dashboard", "ghcr.io/example/dashboard:1.2", nets=("seg_monitoring",), status="exited")
g = C("ai-llamacpp", "ai", "AI", "llamacpp", "ghcr.io/ggml-org/llama.cpp:server", status="not created", desc="profile: local-llm"); g["ghost"] = True; g["profiles"] = ["local-llm"]; g["group"] = None

# structural edges
for c in containers:
    for d in c["depends_on"]: E(c["id"], d, "depends", "depends_on")
# env refs
for s, t, k in [("open-webui", "ollama", "OLLAMA_BASE_URL"), ("homepage", "dockerproxy", "DOCKER_HOST"), ("alloy", "dockerproxy", "DOCKER_HOST"),
                ("wud", "dockerproxy", "WUD_WATCHER_LOCAL_HOST"), ("wharfmap", "dockerproxy", "DOCKER_HOST"), ("zigbee2mqtt", "mosquitto", "MQTT_SERVER"),
                ("seerr", "sonarr", "SONARR_URL :8989"), ("seerr", "radarr", "RADARR_URL :7878"), ("sonarr", "transmission", "TRANSMISSION_URL :9091"),
                ("radarr", "transmission", "TRANSMISSION_URL :9091"), ("sonarr", "prowlarr", "PROWLARR_URL :9696"), ("radarr", "prowlarr", "PROWLARR_URL :9696"),
                ("immich", "immich-db", "DB_HOSTNAME"), ("immich", "immich-redis", "REDIS_HOSTNAME"), ("immich", "immich-ml", "IMMICH_MACHINE_LEARNING_URL"),
                ("nextcloud", "nextcloud-db", "POSTGRES_HOST"), ("nextcloud", "nextcloud-redis", "REDIS_HOST"), ("grafana", "loki", "GF_DATASOURCE_LOKI")]:
    E(s, t, "env", k)
# config refs (HA integrations, Frigate cameras, Alloy -> Loki)
for t, l in [("mosquitto", "integration mqtt"), ("frigate", "integration frigate"), ("plex", "integration plex"), ("whisper", "integration wyoming"),
             ("piper", "integration wyoming"), ("adguard", "integration adguard"), ("transmission", "integration transmission"), ("lan:10.0.0.41", "integration hue"),
             ("lan:10.0.0.60", "integration cast"), ("lan:10.0.0.61", "integration cast")]:
    E("homeassistant", t, "config", l)
for ip in ("10.0.0.50", "10.0.0.51", "10.0.0.52"): E("frigate", "lan:" + ip, "config", "camera rtsp")
E("frigate", "mosquitto", "config", "mqtt.host"); E("alloy", "loki", "config", "loki push"); E("grafana", "loki", "config", "datasource")
E("crowdsec", "npm", "config", "acquisition npm logs")
lan_hosts = {"10.0.0.41": {"label": "Hue bridge (hue)", "kind": "device"}, "10.0.0.50": {"label": "camera 10.0.0.50", "kind": "device"},
             "10.0.0.51": {"label": "camera 10.0.0.51", "kind": "device"}, "10.0.0.52": {"label": "camera 10.0.0.52", "kind": "device"},
             "10.0.0.60": {"label": "Living room TV (cast)", "kind": "device"}, "10.0.0.61": {"label": "Kitchen display (cast)", "kind": "device"}}
# ingress
H(["blog.example.com"], "blog", 8180); H(["cloud.example.com"], "nextcloud", 11000); H(["vault.example.com"], "vaultwarden", 8222, days=9)
H(["photos.example.com"], "immich", 2283); H(["books.example.com"], "audiobookshelf", 13378); H(["read.example.com"], "calibre-web", 8083, acl="family basic auth")
H(["requests.example.com"], "seerr", 5055); H(["home.example.com"], "homeassistant", 8123); H(["chat.example.com"], "open-webui", 3005, enabled=False)
for d, c, p in [("ha.lan", "homeassistant", 8123), ("frigate.lan", "frigate", 5000), ("z2m.lan", "zigbee2mqtt", 8081), ("grafana.lan", "grafana", 3001),
                ("homepage.lan", "homepage", 3002), ("netdata.lan", "netdata", 19999), ("wud.lan", "wud", 3003), ("wharfmap.lan", "wharfmap", 3008),
                ("nodered.lan", "node-red", 1880), ("sonarr.lan", "sonarr", 8989), ("radarr.lan", "radarr", 7878), ("prowlarr.lan", "prowlarr", 9696),
                ("transmission.lan", "transmission", 9091), ("adguard.lan", "adguard", 3000), ("plex.lan", "plex", 32400)]:
    H([d], c, p, public=False, internal=True, cert=False, acl="Internal")
# networks
networks = [{"name": n, "driver": "bridge", "subnet": s, "gateway": s.rsplit(".", 1)[0] + ".1", "ip_range": s.rsplit(".", 1)[0] + ".128/25",
             "members": sorted(c["id"] for c in containers if n in c["networks"])} for n, s in NETS.items()]
networks.append({"name": "host", "driver": "host", "subnet": None, "gateway": None, "ip_range": None, "members": sorted(c["id"] for c in containers if c["hostnet"])})
graph = {"generated": "2026-10-22T12:00-05:00", "title": "Harbor Homelab Map", "host": {"name": HOST, "ip": IP},
         "zones": {"order": ["Edge & infrastructure", "Sites & hosting", "Home & IoT", "Media & downloads", "AI & voice", "Ops & monitoring"],
                   "groups": {"Edge": "Edge & infrastructure", "Core": "Edge & infrastructure", "Sites": "Sites & hosting", "Home": "Home & IoT",
                              "Media": "Media & downloads", "Downloads": "Media & downloads", "AI": "AI & voice", "Monitoring": "Ops & monitoring"}},
         "links": {"buy me coffee": "https://buymeacoffee.com/you"},
         "containers": containers, "networks": networks, "ingress": ingress, "edges": edges, "lan_hosts": lan_hosts}
out = pathlib.Path(__file__).with_name("graph.json"); out.write_text(json.dumps(graph, indent=1))
print(f"{len(containers)} containers, {len(networks)} networks, {len(ingress)} proxy hosts, {len(edges)} edges -> {out}")
