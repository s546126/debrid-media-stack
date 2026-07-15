# debrid-media-stack

A self-hosted media pipeline built on Docker Compose: request a movie or show, it
gets pulled from **Real-Debrid** (no local seeding/storage of the torrent), mounted
as a filesystem, symlinked into a clean library, and served by **Plex** / **Jellyfin**.
Every admin UI is exposed **only over your private Tailscale network**. The one
exception is Plex, which runs on the host network at `:32400` by design (Plex
account auth + Remote Access) — see the security notes below.

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
  open ports for the admin/arr UIs, no public exposure (Plex on `:32400` is the
  deliberate exception).

## Layout

| Compose project | File | Services |
|---|---|---|
| `media`  | `docker-compose.yml`      | zurg, rclone, plex, jellyfin — plus alist + rclone-alist (opt-in via the `alist` profile, off by default) |
| `arr`    | `docker-compose.arr.yml`  | prowlarr, sonarr, radarr, bazarr, jellyseerr, decypharr, flaresolverr |

Each service that needs external access has a `<service>-ts` Tailscale sidecar
(`network_mode: service:<service>-ts`). Internal service-to-service traffic stays on
the docker bridge using the sidecar's network **alias** = service name, so e.g.
`prowlarr → sonarr:8989` keeps working unchanged.

## Prerequisites

1. **Linux host** with Docker + Docker Compose v2 and `/dev/fuse` available
   (standard on most distros). The rclone containers mount with `--allow-other`,
   which requires `user_allow_other` in `/etc/fuse.conf`:

   ```bash
   grep -q '^user_allow_other' /etc/fuse.conf || echo user_allow_other | sudo tee -a /etc/fuse.conf
   ```
2. **Real-Debrid** subscription and API token — <https://real-debrid.com/apitoken>.
3. **Tailscale**:
   - A tailnet (free tier is fine).
   - **MagicDNS enabled** and **HTTPS certificates enabled** (admin console → DNS).
     `${TS_CERT_DOMAIN}` in the `serve.json` files only resolves when HTTPS is on.
   - A **reusable, non-ephemeral, tagged** auth key (admin → Settings → Keys →
     Generate). One key onboards all 7 sidecar nodes (8 with the `alist`
     profile); the tag (e.g. `tag:media`,
     defined in your tailnet policy) is strongly recommended because tagged nodes
     have node-key expiry disabled — untagged nodes all drop off the tailnet when
     their keys expire (~180 days). Don't tick "Ephemeral": node state is
     persisted, and ephemeral nodes are removed on disconnect. See `.env.example`
     for details.

## Setup

```bash
git clone https://github.com/s546126/debrid-media-stack.git && cd debrid-media-stack

# 1. Secrets / env
cp .env.example .env && chmod 600 .env
$EDITOR .env                       # set TS_AUTHKEY, PUID, PGID, TZ

# 2. zurg (Real-Debrid token + rclone remotes)
cp zurg/config.yml.example   zurg/config.yml      # set your RD token
cp zurg/rclone.conf.example  zurg/rclone.conf     # alist creds only if you use alist
chmod 600 zurg/config.yml zurg/rclone.conf

# 3. decypharr (Real-Debrid token again)
cp decypharr-config/config.json.example decypharr-config/config.json && chmod 600 decypharr-config/config.json

# 4. host mount points for the FUSE mounts (see "The /mnt chain" below),
#    and make /mnt propagation shared PERSISTENTLY (survives reboots — without
#    this, rclone cannot restart after a reboot on hosts where /mnt isn't shared)
sudo mkdir -p /mnt/zurg /mnt/alist /mnt/data/media/library/{tv,movies}
sudo chown -R "$(id -u):$(id -g)" /mnt/data/media/library   # must match PUID/PGID in .env
sudo cp mnt-rshared.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now mnt-rshared.service

# 5. bring up storage/playback FIRST (it also creates the shared media-shared
#    network that the arr project references), then the arr stack
docker compose -f docker-compose.yml up -d
docker compose -p arr -f docker-compose.arr.yml up -d

# (optional) also using alist? its services are profile-gated and off by default:
#   docker compose --profile alist -f docker-compose.yml up -d

# (optional) media-butler chatbot on the portal? fill LLM_BASE_URL / LLM_API_KEY /
# JELLYSEERR_KEY in .env first, then:
#   docker compose --profile agent -f docker-compose.yml up -d
```

After the sidecars register (`tailscale status` from the host should list 7 nodes,
or 8 with the `alist` profile),
the UIs are at `https://prowlarr.<tailnet>.ts.net`, `https://sonarr.<tailnet>.ts.net`,
etc. Plex is on the host network at `:32400`.

### Wiring the apps (one-time, in the UIs)

1. **Prowlarr** → add indexers → add Sonarr & Radarr under *Settings → Apps* (use
   `http://sonarr:8989` / `http://radarr:7878`, container-name URLs).
2. **Sonarr/Radarr** → *Settings → Download Clients* → add a **qBittorrent** client
   pointing at `decypharr:8282` → set Root Folder to `/mnt/data/media/library/tv`
   (Sonarr) and `/mnt/data/media/library/movies` (Radarr).
3. **Jellyseerr** → connect to Jellyfin at `http://jellyfin:8096` (works because
   `jellyseerr-ts` and `jellyfin-ts` are both attached to the `media-shared`
   network — tailnet `*.ts.net` names do **not** work from inside the userspace
   sidecars). For Plex (host network), use the arr bridge's gateway IP:
   `http://$(docker network inspect arr_default -f '{{(index .IPAM.Config 0).Gateway}}'):32400`.
   Then add Sonarr (`http://sonarr:8989`) + Radarr (`http://radarr:7878`).
4. **Bazarr** → connect to Sonarr + Radarr for subtitles.
5. **Plex/Jellyfin** → add libraries pointing at `/mnt/data/media/library/{tv,movies}`.

## The `/mnt` chain (read this — it's the #1 thing that goes wrong)

rclone creates the FUSE mounts `/mnt/zurg` and `/mnt/alist` and exports them with
**`rshared`** propagation. Every consumer (plex, jellyfin, sonarr, radarr, bazarr,
decypharr) bind-mounts `/mnt` back with **`rslave`** so they see the
mount appear *after* rclone creates it. Decypharr writes symlinks under `/mnt`, and the
arr apps + players must resolve them through the **same** propagated mount.

If you see **empty libraries or broken symlinks**, this propagation is almost always the
cause:
- The host mountpoint must allow shared propagation. The shipped
  `mnt-rshared.service` unit (Setup step 4) handles this persistently — it
  bind-mounts `/mnt` onto itself if it isn't already a mountpoint (plain
  `mount --make-rshared /mnt` fails on a plain directory), then marks it
  `rshared`, before Docker starts on every boot.
- Start `media` (rclone) **before** `arr`, so the mount exists when consumers attach.
  After a **reboot**, Docker's restart policies do *not* replay `depends_on`
  ordering across projects — rclone may crash-loop briefly until zurg is healthy
  (expected, self-heals), and arr consumers pick the mount up via `rslave`
  propagation once it appears.
- All arr services here mount `/mnt:rslave` deliberately — keep it that way.
- **Stale FUSE mount** ("transport endpoint is not connected" on `/mnt/zurg`):
  happens when rclone dies without a clean unmount (OOM, `docker kill`, an image
  update while a player held files open). The rclone container cleans this up
  itself on start (`fusermount3 -uz` in its entrypoint); the manual escape hatch,
  should you ever need it on the host, is `sudo fusermount3 -uz /mnt/zurg`.

## Sidecar/app coupling (restarts)

Every app shares its Tailscale sidecar's network namespace
(`network_mode: "service:<name>-ts"`). If a sidecar is restarted or recreated
**alone** — tailscaled crash/OOM, `docker compose up -d sonarr-ts`, or an
auto-updater pulling a new `tailscale/tailscale` image — the app keeps running
in the orphaned namespace: the `*.ts.net` URL returns **502**, bridge peers
can't reach the app, and the app itself has **no network at all** until it is
restarted. `depends_on` only orders startup; it does not restart the app for you.

Always recreate the pair together:

```bash
docker compose -p arr -f docker-compose.arr.yml up -d --force-recreate sonarr-ts sonarr   # etc. per pair
```

The portal is a **trio** once the media-butler agent is enabled (portal-ts +
portal + agent share one namespace). Set `COMPOSE_PROFILES=agent` in `.env` so
routine `docker compose up -d` runs see the agent, and recreate all three
together:

```bash
docker compose --profile agent up -d --force-recreate portal-ts portal agent
```

This is also the main reason not to blind-auto-update the `tailscale/tailscale`
`:latest` image (see the pinning note under Security).

## Unified portal (optional)

`https://media.<tailnet>.ts.net` — a single self-contained landing page (no
frameworks, no external requests) with cards for every service and live
reachability dots. Service links are **derived from the page's own hostname**
(`media.<tailnet>` → `sonarr.<tailnet>` …), so it works with zero configuration;
the design follows Apple's fluid-interface guidelines (system type with optical
tracking, translucent chrome, pointer-down feedback, `prefers-reduced-motion` /
`-transparency` / `-contrast` support, automatic light/dark).

The `portal` + `portal-ts` services in `docker-compose.yml` serve it. Optional:
set `PLEX_URL` at the top of `portal/index.html` to your host's tailnet address
(Plex runs on the host network, so it can't be auto-derived).

### Media-butler chatbot (optional, off by default)

The portal can host a small chat assistant that searches and requests media
through Jellyseerr (tool calling against its API). It is gated behind the
`agent` compose profile and a floating chat button that only appears when the
backend is actually running (`/agent/health`), so the base portal is unchanged
unless you opt in.

1. In `.env`, set `LLM_BASE_URL` + `LLM_API_KEY` (any OpenAI-compatible
   endpoint — OpenAI, LiteLLM, one-api, …) and `JELLYSEERR_KEY`
   (Jellyseerr → *Settings → General → API Key*).
2. `docker compose --profile agent -f docker-compose.yml up -d`

The backend (`portal-agent/agent.py`, stdlib-only Python) shares the portal
sidecar's network namespace, so the browser talks to it same-origin at
`https://media.<tailnet>.ts.net/agent/` — no extra tailnet node, cert, or
CORS. All keys stay server-side; the browser sends none, and tailnet
membership is the auth boundary. It adds no tailnet node, so the
`tailscale status` node count above is unchanged.

## Security notes

- **No admin/arr UI is published to the public internet.** Sidecars use Tailscale
  `serve` (tailnet-only), **not** `funnel`. zurg is bound to `127.0.0.1` only.
- **Plex is the exception.** It uses host networking so Plex Remote Access and
  local discovery (GDM/DLNA) work, and relies on Plex account authentication.
  On an internet-facing host, either restrict TCP 32400 with a host firewall
  (e.g. `ufw allow in on tailscale0 to any port 32400` and drop it elsewhere)
  if you only stream over the tailnet, or accept the exposure knowing Plex
  requires sign-in. **Caveat:** Jellyseerr reaches Plex through the docker
  bridge (Wiring step 3), and that traffic arrives on the bridge interface —
  if you lock 32400 down, also allow it from docker bridges (e.g.
  `ufw allow in on br-+ to any port 32400`) or from the `arr_default` subnet,
  otherwise you break your own Jellyseerr → Plex connection.
- **Sidecars run unprivileged.** `TS_USERSPACE=true` means no `NET_ADMIN`, no
  `/dev/net/tun` — a compromised sidecar has no special kernel access.
- Images track `:latest` for simplicity. For reproducible deployments, pin
  digests (`image: tailscale/tailscale@sha256:...`) or run Renovate/Watchtower
  deliberately — don't blind-auto-update a stack that holds FUSE mounts, and
  especially not the `tailscale/tailscale` image: recreating a sidecar alone
  strands its app in an orphaned network namespace (see "Sidecar/app coupling").
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
