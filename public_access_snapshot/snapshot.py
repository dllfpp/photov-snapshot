"""Snapshot renderer for Public Access.

Opens the published dashboard in headless Chromium, as the owner would see it,
and writes a PNG the plugin serves at the public path. That gives pixel-perfect
fidelity, including custom HACS cards, which a re-implementation can never match.

Everything happens on the customer's own machine: the token never leaves it, and
neither does the image.

Rendering is on demand. The plugin writes a request file when someone opens the
public page and the cached image is stale; this service watches for it. A page
nobody visits costs nothing, which matters on a Raspberry Pi.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import aiohttp

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
_LOGGER = logging.getLogger("snapshot")

# As a Home Assistant add-on the settings arrive in /data/options.json; as a
# plain container they arrive as environment variables. One image serves both.
_OPTIONS: dict = {}
try:
    _OPTIONS = json.loads(Path("/data/options.json").read_text(encoding="utf-8"))
except (OSError, ValueError):
    _OPTIONS = {}


def setting(name: str, default: str = "") -> str:
    value = os.getenv(name.upper())
    if value not in (None, ""):
        return value
    option = _OPTIONS.get(name.lower())
    return default if option in (None, "") else str(option)


HA_URL = setting("ha_url", "http://homeassistant:8123").rstrip("/")
HA_TOKEN = setting("ha_token")
DASHBOARD = setting("dashboard")                 # e.g. "pv-public/pv"
OUTPUT_DIR = Path(setting("output_dir", "/config/public_access_snapshots"))
WIDTH = int(setting("width", "1280"))
MAX_HEIGHT = int(setting("max_height", "4000"))
SCALE = float(setting("scale", "1"))
SETTLE_SECONDS = float(setting("settle_seconds", "6"))
INTERVAL_SECONDS = int(setting("interval_seconds", "900"))
ON_DEMAND = setting("on_demand", "true").lower() in {"1", "true", "yes"}
THEME = setting("theme")                         # "", "dark" or "light"
# Which period the energy cards show: "" leaves Home Assistant's default (today),
# otherwise "today", "week", "month" or "year".
PERIOD = setting("period").lower()

# Selects the energy period the way the date-selection card would. The frontend
# keeps the selection in an energy collection cached on the websocket connection
# (`_energy`, or `_energy_<key>` per collection_key); each exposes setPeriod().
SET_PERIOD = r"""
((period) => {
  const ha = document.querySelector("home-assistant");
  const conn = ha && ha.hass && ha.hass.connection;
  if (!conn) return "no-connection";
  const keys = Object.keys(conn).filter((k) => k.startsWith("_energy"));
  if (!keys.length) return "no-energy-collection";
  const now = new Date();
  const dayEnd = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate(), 23, 59, 59, 999);
  let start, end;
  if (period === "week") {
    const monday = new Date(now); monday.setDate(now.getDate() - ((now.getDay() + 6) % 7));
    start = new Date(monday.getFullYear(), monday.getMonth(), monday.getDate());
    end = dayEnd(new Date(start.getFullYear(), start.getMonth(), start.getDate() + 6));
  } else if (period === "month") {
    start = new Date(now.getFullYear(), now.getMonth(), 1);
    end = dayEnd(new Date(now.getFullYear(), now.getMonth() + 1, 0));
  } else if (period === "year") {
    start = new Date(now.getFullYear(), 0, 1);
    end = dayEnd(new Date(now.getFullYear(), 11, 31));
  } else {
    start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    end = dayEnd(now);
  }
  let applied = 0;
  for (const key of keys) {
    const collection = conn[key];
    if (collection && typeof collection.setPeriod === "function") {
      collection.setPeriod(start, end);
      // setPeriod only moves the bounds; the date selector then calls refresh()
      // to refetch the statistics, and the cards redraw from that.
      if (typeof collection.refresh === "function") collection.refresh();
      applied += 1;
    }
  }
  return applied ? `set ${period} on ${applied} collection(s)` : "no-setPeriod";
})
"""

CHROME = shutil.which("chromium-browser") or shutil.which("chromium") or "chromium"
DEBUG_PORT = 9222

REQUEST_FILE = "render.request"
STATUS_FILE = "status.json"

# Runs inside the loaded dashboard. It hides everything that is not the view —
# the header with its tabs (which would reveal the names of private views), the
# search and edit controls, the sidebar — and returns the height of the content
# so the image is cropped to it. Written defensively, walking shadow roots, so a
# frontend release that moves an element degrades to "not hidden" rather than
# to a crash.
PREPARE_PAGE = r"""
(() => {
  const deep = (selector, root = document) => {
    const stack = [root];
    while (stack.length) {
      const node = stack.pop();
      const found = node.querySelector && node.querySelector(selector);
      if (found) return found;
      const all = node.querySelectorAll ? node.querySelectorAll("*") : [];
      for (const element of all) if (element.shadowRoot) stack.push(element.shadowRoot);
    }
    return null;
  };
  const hide = (element) => { if (element) element.style.setProperty("display", "none", "important"); };

  hide(deep("ha-sidebar"));
  const root = deep("hui-root");
  const shadow = root && root.shadowRoot;
  if (!shadow) return 0;
  hide(shadow.querySelector(".header"));
  hide(shadow.querySelector(".toolbar"));

  const view = shadow.querySelector("#view");
  if (!view) return 0;
  view.style.setProperty("padding-top", "0", "important");
  view.style.setProperty("margin-top", "0", "important");
  view.style.setProperty("height", "auto", "important");
  view.style.setProperty("min-height", "0", "important");
  view.style.setProperty("overflow", "visible", "important");

  // The view's child is the layout (masonry, sections, panel). Its scrollHeight
  // is the true content height, whatever the shell's viewport.
  const layout = view.firstElementChild || view;
  const top = layout.getBoundingClientRect().top + window.scrollY;
  return Math.ceil(top + Math.max(layout.scrollHeight, layout.getBoundingClientRect().height) + 16);
})()
"""


def chrome_args() -> list[str]:
    return [
        CHROME,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--hide-scrollbars",
        "--force-device-scale-factor=" + str(SCALE),
        f"--remote-debugging-port={DEBUG_PORT}",
        f"--window-size={WIDTH},{MAX_HEIGHT}",
        "--user-data-dir=/tmp/chrome-profile",
        "about:blank",
    ]


class Browser:
    """The smallest CDP client that does the job."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._id = 0

    async def connect(self) -> None:
        for _ in range(60):
            try:
                async with self._session.get(
                    f"http://127.0.0.1:{DEBUG_PORT}/json/version", timeout=aiohttp.ClientTimeout(total=2)
                ) as response:
                    data = await response.json()
                    endpoint = data["webSocketDebuggerUrl"]
                    break
            except Exception:  # noqa: BLE001 - chromium is still starting
                await asyncio.sleep(0.5)
        else:
            raise RuntimeError("Chromium did not start")
        self._ws = await self._session.ws_connect(endpoint, max_msg_size=0)

    async def send(self, method: str, params: dict | None = None, session_id: str | None = None):
        assert self._ws is not None
        self._id += 1
        message = {"id": self._id, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        await self._ws.send_json(message)
        while True:
            reply = await self._ws.receive_json()
            if reply.get("id") == self._id:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return reply.get("result", {})


def token_bootstrap() -> str:
    """Seed the frontend's stored credentials before any page script runs.

    The Home Assistant frontend reads its token from localStorage. Injecting it
    here means Chromium never sees a login form, and the token stays inside this
    container.
    """
    tokens = {
        "access_token": HA_TOKEN,
        "token_type": "Bearer",
        "expires_in": 1800,
        "hassUrl": HA_URL,
        "clientId": HA_URL + "/",
        "expires": int(time.time() * 1000) + 10 * 365 * 24 * 3600 * 1000,
        "refresh_token": "",
    }
    script = (
        "try{"
        f"localStorage.setItem('hassTokens', {json.dumps(json.dumps(tokens))});"
        "localStorage.setItem('sidebarPanelOrder', '[]');"
        "localStorage.setItem('dockedSidebar', '\"always_hidden\"');"
    )
    if THEME in {"dark", "light"}:
        script += f"localStorage.setItem('selectedTheme', '{json.dumps({'dark': THEME == 'dark'})}');"
    script += "}catch(e){}"
    return script


async def capture(browser: Browser, path: str, destination: Path) -> None:
    """Screenshot one dashboard view."""
    target = await browser.send("Target.createTarget", {"url": "about:blank"})
    session = (await browser.send(
        "Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}
    ))["sessionId"]

    await browser.send("Page.enable", session_id=session)
    await browser.send("Runtime.enable", session_id=session)
    await browser.send(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": token_bootstrap()},
        session_id=session,
    )
    await browser.send(
        "Emulation.setDeviceMetricsOverride",
        {"width": WIDTH, "height": MAX_HEIGHT, "deviceScaleFactor": SCALE, "mobile": False},
        session_id=session,
    )

    url = f"{HA_URL}/{path.strip('/')}"
    _LOGGER.info("rendering %s", url)
    await browser.send("Page.navigate", {"url": url}, session_id=session)
    await asyncio.sleep(SETTLE_SECONDS)

    if PERIOD:
        outcome = await browser.send(
            "Runtime.evaluate",
            {"expression": f"{SET_PERIOD}({json.dumps(PERIOD)})", "returnByValue": True},
            session_id=session,
        )
        _LOGGER.info("period: %s", (outcome.get("result") or {}).get("value"))
        # The energy cards refetch their statistics for the new range.
        await asyncio.sleep(max(3.0, SETTLE_SECONDS / 2))

    # Strip the application chrome and measure the real content. The frontend is
    # a full-height shell that scrolls internally, so the document height says
    # nothing; the view container inside hui-root does.
    result = await browser.send(
        "Runtime.evaluate",
        {"expression": PREPARE_PAGE, "returnByValue": True},
        session_id=session,
    )
    measured = (result.get("result") or {}).get("value") or 0
    height = max(200, min(int(measured) or MAX_HEIGHT, MAX_HEIGHT))
    if measured:
        # Give the layout a moment to settle at the new size before capturing.
        await browser.send(
            "Emulation.setDeviceMetricsOverride",
            {"width": WIDTH, "height": height, "deviceScaleFactor": SCALE, "mobile": False},
            session_id=session,
        )
        await asyncio.sleep(0.8)

    shot = await browser.send(
        "Page.captureScreenshot",
        {
            "format": "png",
            "captureBeyondViewport": True,
            "clip": {"x": 0, "y": 0, "width": WIDTH, "height": height, "scale": 1},
        },
        session_id=session,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(".tmp")
    tmp.write_bytes(base64.b64decode(shot["data"]))
    tmp.replace(destination)
    _LOGGER.info("wrote %s (%d bytes, %dx%d)", destination, destination.stat().st_size, WIDTH, height)

    await browser.send("Target.closeTarget", {"targetId": target["targetId"]})


def write_status(ok: bool, message: str = "") -> None:
    try:
        (OUTPUT_DIR / STATUS_FILE).write_text(
            json.dumps({"ok": ok, "at": time.time(), "message": message}), encoding="utf-8"
        )
    except OSError:
        pass


async def run_once(session: aiohttp.ClientSession) -> None:
    process = subprocess.Popen(
        chrome_args(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        browser = Browser(session)
        await browser.connect()
        await capture(browser, DASHBOARD, OUTPUT_DIR / "dashboard.png")
        write_status(True)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


async def main() -> int:
    if not HA_TOKEN:
        _LOGGER.error("HA_TOKEN is not set")
        return 1
    if not DASHBOARD:
        _LOGGER.error("DASHBOARD is not set (for example 'pv-public/pv')")
        return 1

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    request = OUTPUT_DIR / REQUEST_FILE
    last = 0.0

    async with aiohttp.ClientSession() as session:
        while True:
            asked = request.exists()
            if asked:
                request.unlink(missing_ok=True)
            missing = not (OUTPUT_DIR / "dashboard.png").exists()
            due_by_interval = (
                not ON_DEMAND and (time.time() - last) >= INTERVAL_SECONDS
            )
            # On demand is the default: render when the plugin asks because
            # somebody opened the page, or when there is no image at all. The
            # interval is only used when on-demand is switched off, so a
            # dashboard nobody looks at costs a Raspberry Pi nothing.
            if asked or missing or due_by_interval:
                try:
                    await run_once(session)
                    last = time.time()
                except Exception as error:  # noqa: BLE001 - keep the old image
                    _LOGGER.exception("snapshot failed")
                    write_status(False, str(error))
            await asyncio.sleep(5)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
