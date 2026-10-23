# ha-epaper-render

Turns a Home Assistant dashboard into an image for a color e-paper display.
This container screenshots the dashboard with headless Chromium, dithers the
screenshot to the panel's 6-color palette with ImageMagick, and serves it over
HTTP. An ESPHome device downloads the image with `online_image` and draws it.

Built for a Waveshare 7.3" Spectra E6 panel (800x480, black / white / red /
green / blue / yellow) on an ESP32-S3. The panel deep-sleeps and wakes every 30
minutes to fetch the image.

## How it works

Every `RENDER_INTERVAL` seconds (default 5 minutes), the container runs one
render cycle:

1. Launches a **fresh** headless Chromium. It never keeps a browser open between
   renders, so there is no long-lived browser to leak memory or get stuck.
2. Writes the long-lived access token into `localStorage` (`hassTokens`) before
   Home Assistant's own scripts run, so the page loads already logged in. It
   blocks service workers, because HA's service worker breaks headless page
   loads.
3. Opens the dashboard at the panel's resolution and waits until `ha-card`
   elements are visible. It waits for fonts to load, then pauses
   `SETTLE_SECONDS` so calendar and weather cards can fill in.
4. Checks the screenshot. It must be the right size and must not be blank. If
   the browser ended up on the HA login page, it logs that the token was
   rejected.
5. Dithers to the palette (`-dither FloydSteinberg`, `dither:diffusion-amount`,
   `-remap`) and writes a 6-color paletted PNG.
6. Publishes atomically (write a temp file, then rename), so the display never
   downloads a half-written file.

When a render fails, the last good image keeps being served and the container
retries after `RETRY_SECONDS`. If there has been no good render for
`STALE_ALERT_MINUTES`, it sends a Telegram alert, if one is configured, and
sends another message when renders recover.

**Text anti-aliasing is turned off on purpose** (`fonts-local.conf`). Gray
anti-aliasing pixels have no close match in a 6-color palette, so the dither
scatters them into colored specks around black text. With anti-aliasing off,
text stays pure black, red or blue.

### Endpoints (port 8080 in the container)

| Path | |
|---|---|
| `/display.png` | dithered image, what the display fetches |
| `/raw.png` | undithered screenshot, for debugging |
| `/health` | JSON: healthy, image age, last error, render counters |
| `/refresh` | render now |

## Install

Images are published to GitHub Container Registry for `linux/amd64` and
`linux/arm64`:

```
ghcr.io/andrewmagness/ha-epaper-render:latest     # newest release
ghcr.io/andrewmagness/ha-epaper-render:1          # newest 1.x
ghcr.io/andrewmagness/ha-epaper-render:1.0.0      # exact version
```

1. In Home Assistant, create a long-lived access token: Profile → Security →
   Long-lived access tokens. A dedicated non-admin user is a good idea.
2. Build a dashboard sized for the panel. The kiosk-mode `?kiosk` URL
   parameter hides the header and sidebar.
3. Create a folder with the `docker-compose.yml` below and a `.env` file:
   ```
   HA_TOKEN=your-long-lived-token
   # optional
   TG_TOKEN=
   TG_CHAT=
   ```
4. Set `HA_BASE_URL`, `HA_DASHBOARD_PATH`, `WIDTH` / `HEIGHT`, and `TZ` in the
   compose file.
5. Run `docker compose up -d`.
6. Open `http://<host>:6161/`. You should see the dithered image within a few
   seconds, and `/health` should report `"healthy": true`.
7. Point the display's `online_image` URL at `http://<host>:6161/display.png`.
   See `esphome/online-image-snippet.yaml`.

To update, run `docker compose pull && docker compose up -d`, or pin a version
tag and bump it yourself.

### Compose example

```yaml
services:
  epaper-render:
    image: ghcr.io/andrewmagness/ha-epaper-render:latest
    container_name: epaper-render
    restart: unless-stopped
    init: true
    shm_size: 512m                  # Chromium needs more than Docker's 64 MB default
    ports:
      - "6161:8080"                 # the display fetches http://<host>:6161/display.png
    environment:
      TZ: America/Chicago
      HA_BASE_URL: http://homeassistant.local:8123
      HA_DASHBOARD_PATH: /my-dashboard/0?kiosk
      HA_TOKEN: ${HA_TOKEN}         # long-lived access token, from .env
      WIDTH: "800"
      HEIGHT: "480"
      RENDER_INTERVAL: "300"
      DITHER_DIFFUSION: "65%"
      STALE_ALERT_MINUTES: "30"
      # Optional Telegram alert when no good render for STALE_ALERT_MINUTES
      TG_TOKEN: ${TG_TOKEN:-}
      TG_CHAT: ${TG_CHAT:-}
    volumes:
      - epaper-render-data:/data    # keeps the last good image across restarts

volumes:
  epaper-render-data:
```

With a three-color (black/white/red) panel at a different size, change these:

```yaml
    environment:
      WIDTH: "400"
      HEIGHT: "300"
      PALETTE: "#FFFFFF #000000 #FF0000"
```

### Building it yourself

Clone the repo, then either run `docker compose up -d --build` with the
`build: .` line uncommented, or build it directly:
`docker build -t local/epaper-render:latest .`

## Releasing

Pushing a tag such as `v1.2.3` runs `.github/workflows/release.yml`. It builds
both architectures and publishes `1.2.3`, `1.2`, `1` and `latest` to ghcr.io.
Pushes to `main` and pull requests only build, so a broken Dockerfile fails CI
before a release is cut.

## Configuration

| Variable | Default | |
|---|---|---|
| `HA_BASE_URL` | required | e.g. `http://homeassistant.local:8123` |
| `HA_DASHBOARD_PATH` | required | e.g. `/my-dashboard/0?kiosk` |
| `HA_TOKEN` | required | long-lived access token |
| `WIDTH` / `HEIGHT` | 800 / 480 | panel resolution |
| `RENDER_INTERVAL` | 300 | seconds between renders |
| `RENDER_TIMEOUT` | 45 | seconds to wait for the dashboard |
| `SETTLE_SECONDS` | 5 | extra wait after cards appear |
| `RETRY_SECONDS` | 60 | retry delay after a failed render |
| `PALETTE` | `#FFFFFF #000000 #FF0000 #008000 #0000FF #FFFF00` | space-separated panel colors |
| `DITHER_DIFFUSION` | 65% | Floyd-Steinberg diffusion amount (lower means less noise) |
| `MIN_STDDEV` | 0.05 | below this the screenshot counts as blank |
| `STALE_ALERT_MINUTES` | 30 | alert after this long without a good render |
| `TG_TOKEN` / `TG_CHAT` | unset | optional Telegram alerts |

For another panel, change `PALETTE` to its colors, for example
`#FFFFFF #000000 #FF0000` for a black/white/red panel.

## Troubleshooting

- **`stuck on the HA login page`**: the token is wrong, expired, or was revoked.
- **Timeout waiting for `ha-card`**: the dashboard path is wrong, or HA is down
  or slow to start. Try a larger `RENDER_TIMEOUT`.
- **Blank or half-drawn image**: increase `SETTLE_SECONDS`.
- **Missing emoji**: the image includes `fonts-noto-color-emoji`. Other
  symbols may need more font packages in the `Dockerfile`.

## License

[GPL-3.0](LICENSE)
