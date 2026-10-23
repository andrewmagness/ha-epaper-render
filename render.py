#!/usr/bin/env python3
"""Render a Home Assistant dashboard for the fridge ePaper display.

Every RENDER_INTERVAL seconds: launch a fresh headless Chromium, authenticate to
HA with a long-lived token, screenshot the dashboard, sanity-check it, dither it
to the Spectra E6 palette with ImageMagick, and atomically publish it. A bad
render is discarded and the last good image keeps serving.

Serves:
  /display.png  dithered image the ESPHome device fetches
  /raw.png      undithered screenshot (debugging)
  /health       JSON status; healthy = a good render within STALE_ALERT_MINUTES
  /refresh      trigger a render now
"""
import json
import os
import subprocess
import threading
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from playwright.sync_api import sync_playwright

HA_BASE_URL = os.environ["HA_BASE_URL"].rstrip("/")
HA_DASHBOARD_PATH = os.environ["HA_DASHBOARD_PATH"]
HA_TOKEN = os.environ["HA_TOKEN"]
WIDTH = int(os.environ.get("WIDTH", "800"))
HEIGHT = int(os.environ.get("HEIGHT", "480"))
RENDER_INTERVAL = int(os.environ.get("RENDER_INTERVAL", "300"))
RENDER_TIMEOUT = int(os.environ.get("RENDER_TIMEOUT", "45"))
SETTLE_SECONDS = float(os.environ.get("SETTLE_SECONDS", "5"))
RETRY_SECONDS = int(os.environ.get("RETRY_SECONDS", "60"))
DIFFUSION = os.environ.get("DITHER_DIFFUSION", "65%")
PALETTE = os.environ.get("PALETTE", "#FFFFFF #000000 #FF0000 #008000 #0000FF #FFFF00").split()
MIN_STDDEV = float(os.environ.get("MIN_STDDEV", "0.05"))
STALE_ALERT_MINUTES = int(os.environ.get("STALE_ALERT_MINUTES", "30"))
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")
OUT = os.environ.get("OUT_DIR", "/data")
PORT = 8080

state = {
    "last_success": None,   # epoch seconds of last good publish
    "last_attempt": None,
    "last_error": None,
    "consecutive_failures": 0,
    "renders_ok": 0,
    "renders_failed": 0,
    "alerted": False,
}
lock = threading.Lock()
refresh_now = threading.Event()


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def telegram(text):
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data, timeout=20)
    except Exception as e:
        log(f"telegram send failed: {e}")


def magick(*args):
    return subprocess.run(["convert", *args], capture_output=True, text=True, timeout=60, check=True)


def screenshot(path):
    url = HA_BASE_URL + HA_DASHBOARD_PATH
    origin = urllib.parse.urlsplit(HA_BASE_URL)
    origin = f"{origin.scheme}://{origin.netloc}"
    tokens = json.dumps({"hassUrl": HA_BASE_URL, "access_token": HA_TOKEN, "token_type": "Bearer"})
    with sync_playwright() as p:
        # Fresh browser every render: no long-lived Chromium to wedge, leak or go stale
        browser = p.chromium.launch(args=["--disable-gpu", "--disable-dev-shm-usage"])
        try:
            ctx = browser.new_context(
                viewport={"width": WIDTH, "height": HEIGHT},
                device_scale_factor=1,
                # HA's service worker swallowed puppeteer's navigation events (March 2026)
                service_workers="block",
                locale="en-US",
            )
            # Seed the auth token before HA's own scripts run, on HA's origin only
            ctx.add_init_script(
                f"if (location.origin === {json.dumps(origin)}) {{"
                f" localStorage.setItem('hassTokens', {json.dumps(tokens)});"
                f" localStorage.setItem('selectedLanguage', '\"en\"'); }}"
            )
            page = ctx.new_page()
            page.set_default_timeout(RENDER_TIMEOUT * 1000)
            page.goto(url, wait_until="domcontentloaded")
            # Playwright CSS pierces shadow DOM, so this waits for real dashboard cards
            try:
                page.wait_for_selector("hui-view ha-card, hui-panel-view ha-card, hui-sections-view ha-card",
                                       state="visible")
            except Exception:
                if "/auth/" in page.url:
                    raise RuntimeError("stuck on the HA login page: HA_TOKEN rejected or expired") from None
                raise
            page.evaluate("document.fonts.ready")
            # Calendar/weather cards fill in after their websocket data arrives
            page.wait_for_timeout(SETTLE_SECONDS * 1000)
            page.screenshot(path=path, full_page=False)
        finally:
            browser.close()


def validate(path):
    out = magick(path, "-format", "%w %h %[fx:standard_deviation]", "info:").stdout.split()
    w, h, sd = int(out[0]), int(out[1]), float(out[2])
    if (w, h) != (WIDTH, HEIGHT):
        raise RuntimeError(f"screenshot is {w}x{h}, expected {WIDTH}x{HEIGHT}")
    if sd < MIN_STDDEV:
        raise RuntimeError(f"screenshot looks blank (stddev {sd:.3f} < {MIN_STDDEV})")


def render_once():
    raw_tmp = f"{OUT}/raw.tmp.png"
    disp_tmp = f"{OUT}/display.tmp.png"
    t0 = time.time()
    screenshot(raw_tmp)
    validate(raw_tmp)
    magick(raw_tmp, "-dither", "FloydSteinberg", "-define", f"dither:diffusion-amount={DIFFUSION}",
           "-remap", f"{OUT}/palette.png", f"PNG8:{disp_tmp}")
    # Atomic publish: the ESP never fetches a half-written file
    os.replace(raw_tmp, f"{OUT}/raw.png")
    os.replace(disp_tmp, f"{OUT}/display.png")
    return time.time() - t0


def render_loop():
    magick("-size", "1x1", *[f"xc:{c}" for c in PALETTE], "+append", f"{OUT}/palette.png")
    while True:
        with lock:
            state["last_attempt"] = time.time()
        try:
            secs = render_once()
            with lock:
                recovered = state["alerted"]
                state.update(last_success=time.time(), last_error=None, consecutive_failures=0, alerted=False)
                state["renders_ok"] += 1
            log(f"rendered OK in {secs:.1f}s")
            if recovered:
                telegram("✅ Fridge ePaper: rendering recovered, display image is fresh again")
            wait = RENDER_INTERVAL
        except Exception as e:
            err = f"{type(e).__name__}: {e}".splitlines()[0][:300]
            with lock:
                state["last_error"] = err
                state["consecutive_failures"] += 1
                state["renders_failed"] += 1
            log(f"render FAILED ({state['consecutive_failures']} in a row), keeping last good image: {err}")
            if state["consecutive_failures"] == 1:
                traceback.print_exc()
            wait = RETRY_SECONDS
        check_stale()
        refresh_now.wait(wait)
        refresh_now.clear()


def stale_minutes():
    last = state["last_success"]
    return None if last is None else (time.time() - last) / 60


def check_stale():
    with lock:
        age = stale_minutes()
        stale = age is None or age > STALE_ALERT_MINUTES
        if not stale or state["alerted"]:
            return
        state["alerted"] = True
        err = state["last_error"]
    since = "since startup" if age is None else f"for {age:.0f} min"
    telegram(f"⚠️ Fridge ePaper: no good render {since}. The display is showing a stale image.\nLast error: {err}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if not self.path.startswith("/health"):
            log(f"{self.client_address[0]} {fmt % args}")

    def send_file(self, name):
        try:
            with open(f"{OUT}/{name}", "rb") as f:
                body = f.read()
        except FileNotFoundError:
            return self.send_error(503, "no image rendered yet")
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, code=200):
        body = json.dumps(obj, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/display.png", "/raw.png"):
            return self.send_file(path[1:])
        if path == "/health":
            with lock:
                age = stale_minutes()
                s = dict(state)
            fmt = lambda t: None if t is None else datetime.fromtimestamp(t).isoformat(timespec="seconds")
            return self.send_json({
                "healthy": age is not None and age <= STALE_ALERT_MINUTES,
                "image_age_minutes": None if age is None else round(age, 1),
                "last_success": fmt(s["last_success"]),
                "last_attempt": fmt(s["last_attempt"]),
                "last_error": s["last_error"],
                "consecutive_failures": s["consecutive_failures"],
                "renders_ok": s["renders_ok"],
                "renders_failed": s["renders_failed"],
                "dashboard": HA_DASHBOARD_PATH,
            })
        if path == "/refresh":
            refresh_now.set()
            return self.send_json({"refresh": "queued"})
        if path == "/":
            body = (b"<html><body><h3>epaper-render</h3><p><a href=/display.png>display.png</a> | "
                    b"<a href=/raw.png>raw.png</a> | <a href=/health>health</a> | <a href=/refresh>refresh</a></p>"
                    b"<img src=/display.png></body></html>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        self.send_error(404)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    if os.path.exists(f"{OUT}/display.png"):
        # Serve the previous good image immediately after a restart
        state["last_success"] = os.path.getmtime(f"{OUT}/display.png")
    threading.Thread(target=render_loop, daemon=True).start()
    log(f"serving on :{PORT}, rendering {HA_BASE_URL}{HA_DASHBOARD_PATH} every {RENDER_INTERVAL}s")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
