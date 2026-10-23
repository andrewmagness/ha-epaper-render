# ha-epaper-render

Turns a Home Assistant dashboard into an image for a colour e-paper display.
This container screenshots the dashboard with headless Chromium, dithers the
screenshot to the panel's 6-colour palette with ImageMagick, and serves it over
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
   `-remap`) and writes a 6-colour paletted PNG.
6. Publishes atomically (write a temp file, then rename), so the display never
   downloads a half-written file.

When a render fails, the last good image keeps being served and the container
retries after `RETRY_SECONDS`. If there has been no good render for
`STALE_ALERT_MINUTES`, it sends a Telegram alert, if one is configured, and
sends another message when renders recover.

**Text anti-aliasing is turned off on purpose** (`fonts-local.conf`). Grey
anti-aliasing pixels have no close match in a 6-colour palette, so the dither
scatters them into coloured specks around black text. With anti-aliasing off,
text stays pure black, red or blue.

### Endpoints (port 8080 in the container)

| Path | |
|---|---|
| `/display.png` | dithered image, what the display fetches |
| `/raw.png` | undithered screenshot, for debugging |
| `/health` | JSON: healthy, image age, last error, render counters |
| `/refresh` | render now |

## Install

1. In Home Assistant, create a long-lived access token: Profile → Security →
   Long-lived access tokens. A dedicated non-admin user is a good idea.
2. Build a dashboard sized for the panel. The kiosk-mode `?kiosk` URL
   parameter hides the header and sidebar.
3. Clone this repo, then copy `.env.example` to `.env` and fill in `HA_TOKEN`
   (and `TG_TOKEN` / `TG_CHAT` if you want alerts).
4. Edit `docker-compose.yml` and set `HA_BASE_URL`, `HA_DASHBOARD_PATH`,
   `WIDTH` / `HEIGHT`, and `TZ`.
5. Run `docker compose up -d --build`.
6. Open `http://<host>:6161/`. You should see the dithered image within a few
   seconds, and `/health` should report `"healthy": true`.
7. Point the display's `online_image` URL at `http://<host>:6161/display.png`.
   See `esphome/online-image-snippet.yaml`.

The image is built locally, so update it with `docker compose build --pull && docker compose up -d`.

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
| `PALETTE` | `#FFFFFF #000000 #FF0000 #008000 #0000FF #FFFF00` | space-separated panel colours |
| `DITHER_DIFFUSION` | 65% | Floyd-Steinberg diffusion amount (lower means less noise) |
| `MIN_STDDEV` | 0.05 | below this the screenshot counts as blank |
| `STALE_ALERT_MINUTES` | 30 | alert after this long without a good render |
| `TG_TOKEN` / `TG_CHAT` | unset | optional Telegram alerts |

For another panel, change `PALETTE` to its colours, for example
`#FFFFFF #000000 #FF0000` for a black/white/red panel.

## Troubleshooting

- **`stuck on the HA login page`**: the token is wrong, expired, or was revoked.
- **Timeout waiting for `ha-card`**: the dashboard path is wrong, or HA is down
  or slow to start. Try a larger `RENDER_TIMEOUT`.
- **Blank or half-drawn image**: increase `SETTLE_SECONDS`.
- **Missing emoji**: the image includes `fonts-noto-color-emoji`. Other
  symbols may need more font packages in the `Dockerfile`.
