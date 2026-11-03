"""
Private shuttle commands: /gg, /leave, /from. Gitignored; the bots load this file if it's there.
Config: SHUTTLES env var, else shuttles.json in the project folder.

Run on your PC to get the SHUTTLES value for Vercel:  python api/_private.py
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

SGT = timezone(timedelta(hours=8))

_shuttle_file = Path(__file__).resolve().parent.parent / "shuttles.json"
SHUTTLE = json.loads(
    os.environ.get("SHUTTLES")
    or (_shuttle_file.read_text(encoding="utf-8-sig") if _shuttle_file.exists() else "{}")
)
# Command names live in the config so they stay secret: {"name": how many upcoming shuttles it shows}
SHOW = SHUTTLE.get("show", {})
FROM_COMMAND = SHUTTLE.get("from_command", "")
COMMANDS = tuple(SHOW)
SHUTTLE_ROUTES = SHUTTLE.get("routes", [])
SHUTTLE_OFF_DAYS = {datetime.fromisoformat(
    d).date() for d in SHUTTLE.get("off_days", [])}


def shuttle_runs(day) -> bool:
    """Shuttle runs Mon-Fri, except dates in SHUTTLE_OFF_DAYS."""
    return day.weekday() < 5 and day not in SHUTTLE_OFF_DAYS


def shuttle_message(routes: list, count: int) -> str:
    """Next `count` departures after now for each route."""
    now = datetime.now(SGT)
    today = now.date()
    runs_today = shuttle_runs(today)
    # off day: nothing left today
    t = now.strftime("%H%M") if runs_today else "9999"
    # Next day the shuttle runs, skipping weekends and off days (capped so a bad list can't loop forever)
    next_day = today + timedelta(days=1)
    for _ in range(30):
        if shuttle_runs(next_day):
            break
        next_day += timedelta(days=1)
    next_label = (
        "tomorrow"
        if next_day == today + timedelta(days=1)
        else f"{next_day:%a} {next_day.day} {next_day:%b}"  # e.g. "Mon 28 Dec"
    )
    # Same from → to listed more than once: merge their times so it only shows once
    merged = {}
    for route in routes:
        merged.setdefault((route["from"], route["to"]), set()).update(
            route.get("times", []))
    lines = []
    for (start, end), times in merged.items():
        times = sorted(times)
        upcoming = [x for x in times if x >= t][:count]
        if upcoming:
            parts = []
            for i, x in enumerate(upcoming, 1):
                dep = now.replace(hour=int(x[:2]), minute=int(
                    x[2:]), second=0, microsecond=0)
                mins = int((dep - now).total_seconds() // 60)
                parts.append(
                    f"{i}. {x[:2]}:{x[2:]} ({'now' if mins < 1 else f'{mins} min'})")
            text = "Timings:\n" + "\n".join(parts)
        elif times:
            reason = "No more today" if runs_today else "No shuttle today"
            text = f"{reason}, first shuttle {next_label} {times[0][:2]}:{times[0][2:]}"
        else:
            text = "No timings set"
        lines.append(f"🚐 *{start} → {end}*\n{text}")

    if not lines:
        return "No shuttle timings set up yet."
    return "\n\n".join(lines) + f"\n\n_Updated {now.strftime('%H:%M:%S')}_"


def reply(text: str) -> str:
    """'/gg' (nearest shuttle), '/leave' or '/from <place>' (next few) -> the shuttle message."""
    words = text.split()
    command = words[0].split("@")[0][1:]  # "/gg" -> "gg"
    if command != FROM_COMMAND:
        return shuttle_message([r for r in SHUTTLE_ROUTES if r.get("command") == command], SHOW[command])
    # /from: match ignoring case and spaces, so "/from blocka" finds "Block A"
    wanted = "".join(words[1:]).lower()
    routes = [
        r for r in SHUTTLE_ROUTES
        if wanted and wanted in r["from"].replace(" ", "").lower()
    ]
    if routes:
        return shuttle_message(routes, SHOW[FROM_COMMAND])
    places = list(dict.fromkeys(r["from"] for r in SHUTTLE_ROUTES))
    if not places:
        return "No shuttle timings set up yet."
    return f"Send /from and where you're leaving, e.g. /from {places[0]}\n\nPlaces: " + ", ".join(places)


if __name__ == "__main__":
    print("In Vercel → Settings → Environment Variables, set SHUTTLES to:\n")
    print(json.dumps(SHUTTLE, ensure_ascii=False, separators=(",", ":")))
