# debrid-media-stack

A self-hosted media pipeline built on Docker Compose: request a movie or show, it
gets pulled from **Real-Debrid** (no local seeding/storage of the torrent), mounted
as a filesystem, hard-linked into a clean library, and served by **Plex** / **Jellyfin**.
Every admin UI is exposed **only over your private Tailscale network** — nothing
listens on the public internet.

```
┌──────────────┐  request   ┌─────────────┐
│  Jellyseerr  │ ─────────► │ Sonarr /     │  ◄── Prowlarr (indexer manager)
│  (request UI)│            │ Radarr       │       feeds search results
└──────────────┘            └──────┬───────┘
                                   │ sends torrent/magnet
                                   ▼
                            ┌─────────────┐
                            │  Decypharr  │  qBittorrent-API shim → Real-Debrid
                            └──────┬──────┘
                                   │ adds hash to RD, RD caches it
                                   ▼
                            ┌─────────────┐   WebDAV
                            │    zurg     │ ─────────┐
                            │ (RD → DAV)  │          │
                            └─────────────┘          ▼
                                              ┌──────────────┐
                                              │   rclone     │ FUSE mount
                                              │  /mnt/zurg   │
                                              └──────┬───────┘
                                                     │ Sonarr/Radarr create symlinks
                                                     ▼
                                       /mnt/data/media/library/{tv,movies}
                                                     │
                                   ┌─────────────────┴─────────────────┐
                                   ▼                                   ▼
                              ┌─────────┐                        ┌──────────┐
                              │  Plex   │                        │ Jellyfin │   + Bazarr (subtitles)
                              └─────────┘                        └──────────┘
```

## Why this design

- **No local downloads.** Decypharr hands torrent hashes to Real-Debrid; RD does the
  downloading and caching. zurg exposes your RD library as WebDAV, rclone mounts it as
  a FUSE filesystem. Sonarr/Radarr only ever create **symlinks** into the library — so
  the library is tiny and "downloads" are instant once RD has the file cached.
- **Tailnet-only by default.** Each admin service runs behind its own Tailscale sidecar
  and is reachable at `https://<service>.<your-tailnet>.ts.net`. No reverse proxy, no
  open ports, no public exposure.

## Layout

| Compose project | File | Services |
|---|---|---|
| `media`  | `docker-compose.yml`      | zurg, rclone, alist, rclone-alist, plex, jellyfin |
| `arr`    | `docker-compose.arr.yml`  | prowlarr, sonarr, radarr, bazarr, jellyseerr, decypharr, flaresolverr |

Each service that needs external access has a `<service>-ts` Tailscale sidecar
(`network_mode: service:<service>-ts`). Internal service-to-service traffic stays on
the docker bridge using the sidecar's network **alias** = service name, so e.g.
`prowlarr → sonarr:8989` keeps working unchanged.

## Prerequisites

1. **Linux host** with Docker + Docker Compose v2, and `/dev/fuse` + `/dev/net/tun`
   available (standard on most distros).
2. **Real-Debrid** subscription and API token — <https://real-debrid.com/apitoken>.
3. **Tailscale**:
   - A tailnet (free tier is fine).
   - **MagicDNS enabled** and **HTTPS certificates enabled** (admin console → DNS).
     `${TS_CERT_DOMAIN}` in the `serve.json` files only resolves when HTTPS is on.
   - A **reusable** auth key (admin → Settings → Keys → Generate). One key onboards
     all 8 sidecar nodes.

## Setup

```bash
git clone https://github.com/s546126/debrid-media-stack.git && cd debrid-media-stack

# 1. Secrets / env
cp .env.example .env && chmod 600 .env
$EDITOR .env                       # set TS_AUTHKEY, PUID, PGID, TZ

# 2. zurg (Real-Debrid token + rclone remotes)
cp zurg/config.yml.example   zurg/config.yml      # set your RD token
cp zurg/rclone.conf.example  zurg/rclone.conf     # alist creds only if you use alist

# 3. decypharr (Real-Debrid token again)
cp decypharr-config/config.json.example decypharr-config/config.json

# 4. host mount points for the FUSE mounts (see "The /mnt chain" below)
sudo mkdir -p /mnt/zurg /mnt/alist /mnt/data/media/library/{tv,movies}
sudo chown -R "$PUID:$PGID" /mnt/data/media/library

# 5. bring up storage/playback first, then the arr stack
docker compose -f docker-compose.yml up -d
docker compose -p arr -f docker-compose.arr.yml up -d
```

After the sidecars register (`tailscale status` from the host should list 8 nodes),
the UIs are at `https://prowlarr.<tailnet>.ts.net`, `https://sonarr.<tailnet>.ts.net`,
etc. Plex is on the host network at `:32400`.

### Wiring the apps (one-time, in the UIs)

1. **Prowlarr** → add indexers → add Sonarr & Radarr under *Settings → Apps* (use
   `http://sonarr:8989` / `http://radarr:7878`, container-name URLs).
2. **Sonarr/Radarr** → *Settings → Download Clients* → add a **qBittorrent** client
   pointing at `decypharr:8282` → set Root Folder to `/mnt/data/media/library/tv`
   (Sonarr) and `/mnt/data/media/library/movies` (Radarr).
3. **Jellyseerr** → connect to Jellyfin/Plex + Sonarr + Radarr.
4. **Bazarr** → connect to Sonarr + Radarr for subtitles.
5. **Plex/Jellyfin** → add libraries pointing at `/mnt/data/media/library/{tv,movies}`.

## The `/mnt` chain (read this — it's the #1 thing that goes wrong)

rclone creates the FUSE mounts `/mnt/zurg` and `/mnt/alist` and exports them with
**`rshared`** propagation. Every consumer (plex, jellyfin, sonarr, radarr, bazarr,
decypharr) bind-mounts `/mnt` (or `/mnt/zurg`) back with **`rslave`** so they see the
mount appear *after* rclone creates it. Decypharr writes symlinks under `/mnt`, and the
arr apps + players must resolve them through the **same** propagated mount.

If you see **empty libraries or broken symlinks**, this propagation is almost always the
cause:
- The host mountpoint must allow shared propagation: `sudo mount --make-rshared /mnt`
  (and ideally make `/` shared so it survives reboots — distro-dependent).
- Start `media` (rclone) **before** `arr`, so the mount exists when consumers attach.
- All arr services here mount `/mnt:rslave` deliberately — keep it that way.

## Security notes

- **Nothing is published to the public internet.** Sidecars use Tailscale `serve`
  (tailnet-only), **not** `funnel`. zurg is bound to `127.0.0.1` only.
- The arr UIs ship with **no login** (`AuthenticationMethod=None`) because tailnet
  membership is the access boundary. If multiple people share your tailnet, enable
  app-level auth in each service.
- `.env`, the real `zurg/config.yml`, `zurg/rclone.conf`, and `decypharr-config/config.json`
  hold secrets and are **gitignored**. Only the `.example` templates are tracked.

## Not using Tailscale?

This stack assumes a tailnet. To run without it, drop every `*-ts` sidecar service,
remove the `network_mode: "service:*-ts"` lines, and publish each app's port directly
(e.g. `ports: ["8989:8989"]` on sonarr). Put it behind your own reverse proxy/VPN —
**do not** expose the arr UIs to the public internet without auth.

## License

MIT — see [LICENSE](LICENSE).
