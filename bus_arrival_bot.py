"""
Telegram bot: bus arrivals for two fixed stops in Ang Mo Kio,
using the LTA DataMall Bus Arrival API (v3).

Commands: /menu, /arrival, /clear
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

# ---------- Config ----------

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
LTA_API_KEY = os.getenv("LTA_API_KEY")

ARRIVAL_URL = "https://datamall2.mytransport.sg/ltaodataservice/v3/BusArrival"
ROUTES_URL = "https://datamall2.mytransport.sg/ltaodataservice/BusRoutes"
LTA_HEADERS = {"AccountKey": LTA_API_KEY or "", "accept": "application/json"}

# always use Singapore time, even on a cloud server
SGT = timezone(timedelta(hours=8))
DAY_PREFIX = ["WD"] * 5 + [
    "SAT",
    "SUN",
]  # Mon-Fri, Sat, Sun -> BusRoutes field prefixes

# Retrieve bus timings from the .env file
WATCHLIST = json.loads(os.environ.get("WATCHLIST") or "[]")

LOAD_ICON = {"SEA": "🟢", "SDA": "🟡", "LSD": "🔴"}  # seats / standing / limited
LEGEND = "🟢 seats  🟡 standing  🔴 limited  🕒 scheduled"
MENU_TEXT = "🚌 *Bus Arrival Bot*\n\n/arrival - next buses at your stops\n/menu - show this menu"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("busbot")


# ---------- Arrival times ----------


def format_eta(bus: dict) -> str | None:
    """One NextBus block -> e.g. '5 min 🟢'. None if the slot is empty."""
    eta_str = bus.get("EstimatedArrival")
    if not eta_str:  # NextBus2/3 are blank when no more buses are on the road
        return None
    seconds = (
        datetime.fromisoformat(eta_str) - datetime.now(timezone.utc)
    ).total_seconds()
    minutes = int(seconds // 60)  # LTA: always round down
    text = "Arr" if minutes < 1 else f"{minutes} min"
    text += " " + LOAD_ICON.get(bus.get("Load", ""), "")
    if bus.get("Monitored") == 0:  # time comes from the schedule, not live GPS
        text += "🕒"
    return text


async def fetch_service(
    client: httpx.AsyncClient, stop: str, service: str
) -> dict | None:
    r = await client.get(
        ARRIVAL_URL, params={"BusStopCode": stop, "ServiceNo": service}
    )
    r.raise_for_status()
    for svc in r.json().get("Services", []):
        if svc.get("ServiceNo") == service:
            return svc
    return None


# ---------- Operating hours (first/last bus) ----------


class BusHours:
    """Caches first/last bus times from BusRoutes, refreshed once a day."""

    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}
        self.loaded_on = None
        self._lock = (
            asyncio.Lock()
        )  # stop two messages from downloading at the same time

    async def ensure_loaded(self, client: httpx.AsyncClient):
        today = datetime.now(SGT).date()
        if self.loaded_on == today:
            return
        async with self._lock:
            if (
                self.loaded_on == today
            ):  # another request finished loading while we waited
                return
            try:
                self.rows = await self._download(client)
                self.loaded_on = today
                log.info(
                    "Loaded bus hours for %d of %d stops",
                    len(self.rows),
                    len(WATCHLIST),
                )
            except httpx.HTTPError as e:
                log.warning("Couldn't load bus hours: %s", e)

    @staticmethod
    async def _download(client: httpx.AsyncClient) -> dict:
        """BusRoutes can't be filtered, so page through it (500 rows a call) until both stops are found."""
        wanted = {(w["stop"], w["service"]) for w in WATCHLIST}
        found, skip = {}, 0
        while len(found) < len(wanted):
            r = await client.get(ROUTES_URL, params={"$skip": skip})
            r.raise_for_status()
            rows = r.json().get("value", [])
            if not rows:
                break
            for row in rows:
                key = (row.get("BusStopCode"), row.get("ServiceNo"))
                if key in wanted:
                    found[key] = row
            skip += 500
        return found

    def status_text(self, stop: str, service: str) -> str:
        """Shown when LTA has no arrivals: 'not in operation' outside first/last bus, else 'No Est.'"""
        row = self.rows.get((stop, service))
        if not row:
            return "No Estimate Available"

        now = datetime.now(SGT)
        t = now.strftime("%H%M")
        prefix = DAY_PREFIX[now.weekday()]
        first, last = (
            row.get(f"{prefix}_FirstBus", ""),
            row.get(f"{prefix}_LastBus", ""),
        )

        if first.isdigit() and last.isdigit():
            running = (
                (first <= t <= last) if first <= last else (
                    t >= first or t <= last)
            )
            if running:
                return "No Est. Available"

        # Not running: next first bus is later today, or else tomorrow
        if not (first.isdigit() and t < first):
            first = row.get(
                f"{DAY_PREFIX[(now.weekday() + 1) % 7]}_FirstBus", "")
        if first.isdigit():
            return f"Not in operation, first bus {first[:2]}:{first[2:]}"
        return "Not in operation"


hours = BusHours()


# ---------- Message ----------


async def stop_line(client: httpx.AsyncClient, item: dict) -> str:
    header = f"🚏 *{item['name']}*\n🚌 {item['service']}: "
    try:
        svc = await fetch_service(client, item["stop"], item["service"])
    except httpx.HTTPError as e:
        log.warning("API error for %s: %s", item["stop"], e)
        return header + "⚠️ Couldn't reach LTA, try again"

    etas = [
        t
        for key in ("NextBus", "NextBus2", "NextBus3")
        if svc and (t := format_eta(svc.get(key, {})))
    ]
    return header + (
        " | ".join(etas) if etas else hours.status_text(
            item["stop"], item["service"])
    )


async def build_message(client: httpx.AsyncClient) -> str:
    await hours.ensure_loaded(client)
    lines = await asyncio.gather(
        *(stop_line(client, item) for item in WATCHLIST)
    )  # both stops at once
    updated = datetime.now(SGT).strftime("%H:%M:%S")
    return "\n\n".join(lines) + f"\n\n_{LEGEND}_\n_Updated {updated}_"


# ---------- Telegram handlers ----------


async def arrival(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await build_message(context.bot_data["http"])
    await update.message.reply_text(text, parse_mode="Markdown")


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(MENU_TEXT, parse_mode="Markdown")


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deletes the last 300 messages, yours and the bot's. Telegram skips ones over 48h old."""
    last_id = update.message.message_id
    for end in range(
        last_id, max(last_id - 300, 0), -100
    ):  # delete_messages takes up to 100 at a time
        try:
            await context.bot.delete_messages(
                update.effective_chat.id, list(
                    range(max(end - 99, 1), end + 1))
            )
        except BadRequest as e:
            log.info("Couldn't delete some messages: %s", e)


# ---------- Startup / shutdown ----------


async def on_startup(app: Application):
    """One shared HTTP client for the bot's lifetime, pre-load bus hours, and fill Telegram's command list."""
    app.bot_data["http"] = httpx.AsyncClient(headers=LTA_HEADERS, timeout=10)
    await hours.ensure_loaded(app.bot_data["http"])
    await app.bot.set_my_commands(
        [
            BotCommand("menu", "Show what each command does"),
            BotCommand("arrival", "Show bus arrival times"),
            BotCommand("clear", "Delete recent messages"),
        ]
    )


async def on_shutdown(app: Application):
    await app.bot_data["http"].aclose()


def main():
    if not TELEGRAM_TOKEN or not LTA_API_KEY or not WATCHLIST:
        raise SystemExit(
            "Set TELEGRAM_TOKEN and LTA_API_KEY and WATCHLIST (in .env locally, or as host variables)"
        )

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(20)
        .read_timeout(20)
        .write_timeout(20)
        .pool_timeout(20)
        .get_updates_connect_timeout(20)
        .get_updates_read_timeout(30)
        .concurrent_updates(True)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("arrival", arrival))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(
        CommandHandler(["menu", "start"], menu)
    )  # Telegram sends /start the first time you open the bot
    log.info("Bot running…")
    app.run_polling(bootstrap_retries=5)


if __name__ == "__main__":
    main()
