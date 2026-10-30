"""
Telegram bus bot as a Vercel serverless function (webhook).
Runs only when Telegram sends a message or a button tap. Nothing stays running.

Commands: /menu, /arrival
Env vars (set in Vercel): TELEGRAM_TOKEN, LTA_API_KEY

One-time setup after deploying: open https://<your-project>.vercel.app/api/webhook?setup
"""

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

import httpx

# ---------- Config ----------

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
LTA_API_KEY = os.environ.get("LTA_API_KEY", "")
# Secret Telegram must send with every request, derived from the token so there's nothing extra to set
WEBHOOK_SECRET = hashlib.sha256(f"webhook:{TELEGRAM_TOKEN}".encode()).hexdigest()[:32]

TG_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
ARRIVAL_URL = "https://datamall2.mytransport.sg/ltaodataservice/v3/BusArrival"
LTA_HEADERS = {"AccountKey": LTA_API_KEY, "accept": "application/json"}

SGT = timezone(timedelta(hours=8))
DAY_PREFIX = ["WD"] * 5 + ["SAT", "SUN"]  # Mon-Fri, Sat, Sun

WATCHLIST = json.loads(os.environ.get("WATCHLIST") or "[]")

LOAD_ICON = {"SEA": "🟢", "SDA": "🟡", "LSD": "🔴"}
LEGEND = "🟢 seats  🟡 standing  🔴 limited  🕒 scheduled"
MENU_TEXT = "🚌 *Bus Arrival Bot*\n\n/arrival - next buses at your stops\n/menu - show this menu"


# ---------- Arrival times ----------

def format_eta(bus: dict) -> str | None:
    """One NextBus block -> e.g. '5 min 🟢'. None if the slot is empty."""
    eta_str = bus.get("EstimatedArrival")
    if not eta_str:
        return None
    seconds = (datetime.fromisoformat(eta_str) - datetime.now(timezone.utc)).total_seconds()
    minutes = int(seconds // 60)  # LTA: always round down
    text = "Arr" if minutes < 1 else f"{minutes} min"
    text += " " + LOAD_ICON.get(bus.get("Load", ""), "")
    if bus.get("Monitored") == 0:
        text += "🕒"
    return text


def status_text(item: dict) -> str:
    """Shown when LTA has no arrivals."""
    row = item.get("hours")
    if not row:
        return "No Estimate Available"
    now = datetime.now(SGT)
    t = now.strftime("%H%M")
    prefix = DAY_PREFIX[now.weekday()]
    first, last = row.get(f"{prefix}_FirstBus", ""), row.get(f"{prefix}_LastBus", "")
    if first.isdigit() and last.isdigit():
        running = (first <= t <= last) if first <= last else (t >= first or t <= last)
        if running:
            return "No Estimate Available"
    if not (first.isdigit() and t < first):
        first = row.get(f"{DAY_PREFIX[(now.weekday() + 1) % 7]}_FirstBus", "")
    if first.isdigit():
        return f"Not in operation, first bus {first[:2]}:{first[2:]}"
    return "Not in operation"


def stop_line(client: httpx.Client, item: dict) -> str:
    header = f"🚏 *{item['name']}*\n🚌 {item['service']}: "
    try:
        r = client.get(ARRIVAL_URL, params={"BusStopCode": item["stop"], "ServiceNo": item["service"]})
        r.raise_for_status()
        svc = next((s for s in r.json().get("Services", []) if s.get("ServiceNo") == item["service"]), None)
    except (httpx.HTTPError, ValueError):
        return header + "⚠️ Couldn't reach LTA, try again"
    etas = [t for key in ("NextBus", "NextBus2", "NextBus3")
            if svc and (t := format_eta(svc.get(key, {})))]
    return header + (" | ".join(etas) if etas else status_text(item))


def build_message() -> str:
    with httpx.Client(headers=LTA_HEADERS, timeout=8) as client, ThreadPoolExecutor() as pool:
        lines = list(pool.map(lambda item: stop_line(client, item), WATCHLIST))  # both stops at once
    updated = datetime.now(SGT).strftime("%H:%M:%S")
    return "\n\n".join(lines) + f"\n\n_{LEGEND}_\n_Updated {updated}_"


# ---------- Telegram ----------

def tg(method: str, **payload):
    httpx.post(f"{TG_API}/{method}", json=payload, timeout=8)

def handle_update(update: dict):
    msg = update.get("message") or {}
    words = msg.get("text", "").split()
    command = words[0].split("@")[0] if words else ""
    if command == "/arrival":
        tg("sendMessage", chat_id=msg["chat"]["id"], text=build_message(), parse_mode="Markdown")
    elif command in ("/menu", "/start"):  # Telegram sends /start the first time you open the bot
        tg("sendMessage", chat_id=msg["chat"]["id"], text=MENU_TEXT, parse_mode="Markdown")

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        # Only accept requests that carry our secret (registered during ?setup)
        if self.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            self.send_response(403)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            handle_update(json.loads(self.rfile.read(length) or b"{}"))
        except Exception as e:  # never let Telegram retry forever on a bad update
            print("Error handling update:", repr(e))
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        """Visit /api/webhook?setup once after deploying to connect Telegram to this URL."""
        text = "Bus bot is alive. Open this page with ?setup at the end to connect Telegram."
        if urlparse(self.path).query.startswith("setup"):
            url = f"https://{self.headers.get('Host')}/api/webhook"
            r = httpx.post(f"{TG_API}/setWebhook", timeout=8, json={
                "url": url,
                "secret_token": WEBHOOK_SECRET,
                                "allowed_updates": ["message"],
                "drop_pending_updates": True,
            }).json()
            tg("setMyCommands", commands=[{"command": "arrival", "description": "Show bus arrival times"},
                                          {"command": "menu", "description": "Show what each command does"}])
            text = (f"✅ Connected! Telegram will now send messages to {url}\nNow send /arrival to your bot."
                    if r.get("ok") else f"❌ Setup failed: {r.get('description')}\nCheck TELEGRAM_TOKEN in Vercel.")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(text.encode())


# ---------- Run on your PC to fill in HOURS:  python api/webhook.py ----------

# ---------- Run on your PC to get the WATCHLIST value for Vercel:  python api/webhook.py ----------

def update_hours():
    """Adds first/last bus times to WATCHLIST from .env and prints the value to paste into Vercel."""
    from pathlib import Path

    # Look for .env in the project folder, the api folder, and wherever you ran the command from
    here = Path(__file__).resolve().parent
    places = list(dict.fromkeys([here.parent / ".env", here / ".env", Path.cwd() / ".env"]))
    env = {}
    for path in places:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():  # utf-8-sig handles Notepad's BOM
            name, sep, value = line.partition("=")
            if sep:  # forgiving about case and spaces
                env.setdefault(name.strip().upper(), value.strip().strip("'\""))
    key = LTA_API_KEY or env.get("LTA_API_KEY")
    watchlist = WATCHLIST or json.loads(env.get("WATCHLIST") or "[]")
    if not key or not watchlist:
        missing = " and ".join(n for n, v in (("LTA_API_KEY", key), ("WATCHLIST", watchlist)) if not v)
        found = [str(p) for p in places if p.exists()]
        raise SystemExit(
            f"Couldn't find {missing}.\n"
            "Looked for .env in:\n  " + "\n  ".join(map(str, places)) + "\n"
            + (f"Found: {', '.join(found)}, but no {missing} line in it."
               if found else "No .env file found in any of those. Check it isn't named .env.txt.")
        )

    fields = ["WD_FirstBus", "WD_LastBus", "SAT_FirstBus", "SAT_LastBus", "SUN_FirstBus", "SUN_LastBus"]
    wanted = {(w["stop"], w["service"]) for w in watchlist}
    found, skip = {}, 0
    with httpx.Client(headers={"AccountKey": key, "accept": "application/json"}, timeout=20) as client:
        while len(found) < len(wanted):
            rows = client.get("https://datamall2.mytransport.sg/ltaodataservice/BusRoutes",
                              params={"$skip": skip}).json().get("value", [])
            if not rows:
                break
            for row in rows:
                k = (row["BusStopCode"], row["ServiceNo"])
                if k in wanted:
                    found[k] = {f: row[f] for f in fields}
            skip += 500
            print(f"  checked {skip} rows…", end="\r")

    for w in watchlist:
        w["hours"] = found.get((w["stop"], w["service"]), {})

    print(f"\n✅ Found hours for {len(found)} of {len(wanted)} stops")
    for k, v in found.items():
        print(f"   {k[1]} @ {k[0]}: weekdays {v['WD_FirstBus']}–{v['WD_LastBus']}")
    if wanted - found.keys():
        print(f"⚠️ Not found: {wanted - found.keys()}. Check the stop codes.")
    print("\nIn Vercel → Settings → Environment Variables, set WATCHLIST to:\n")
    print(json.dumps(watchlist, ensure_ascii=False, separators=(",", ":")))
    print("\nThen redeploy so Vercel picks it up.")


if __name__ == "__main__":
    update_hours()