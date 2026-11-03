# Bus Arrival Bot

A Telegram bot that shows live bus arrival times for your chosen stops using the [LTA DataMall](https://datamall.lta.gov.sg/) Bus Arrival API (v3).

## Features

- `/arrival` shows the next 3 buses for each stop (send it again for new times)
- `/clear` deletes recent messages in the chat (Telegram only allows deleting messages under 48 hours old)
- `/menu` lists what each command does
- Crowding shown as 🟢 seats, 🟡 standing, 🔴 limited
- 🕒 marks times taken from the timetable instead of live GPS
- Outside operating hours it shows "Not in operation, first bus HH:MM"
- Always uses Singapore time (UTC+8), even on an overseas server
- Your stops are kept in an environment variable, not in the code
- Optional private shuttle commands, with names and timings kept out of the repo (see [Private shuttle commands](#private-shuttle-commands))

## Architecture

There are two versions of the bot. They share the same logic but receive messages differently.

### Vercel version (webhook)

Nothing runs until a message arrives. Telegram calls the Vercel function, which fetches arrival times, replies, and exits.

```mermaid
sequenceDiagram
    participant U as You (Telegram app)
    participant T as Telegram
    participant V as Vercel<br/>api/webhook.py
    participant L as LTA DataMall

    U->>T: /arrival
    T->>V: POST /api/webhook (with secret token)
    V->>L: BusArrival for each stop (in parallel)
    L-->>V: Next 3 buses, crowding
    V->>T: sendMessage
    T-->>U: Arrival times
```

First/last bus times don't change often, so they aren't fetched on every message. You run `python api/webhook.py` on your PC once. It downloads the times from LTA's BusRoutes and stores them inside the `WATCHLIST` variable you paste into Vercel.

### Local version (polling)

`bus_arrival_bot.py` runs all the time and keeps asking Telegram for new messages. It downloads first/last bus times itself, once a day.

```mermaid
flowchart LR
    U[You] <--> T[Telegram]
    B[bus_arrival_bot.py<br/>running on your PC] -- polls for messages --> T
    B -- BusArrival on each /arrival --> L[LTA DataMall]
    B -- BusRoutes once a day --> L
```

### How `/clear` works

In a private chat, every message (yours and the bot's) gets the next number in one sequence. `/clear` deletes the 300 numbers before it, in batches of 100. The bot doesn't need to remember what it sent, which matters on Vercel, where nothing is saved between messages.

## Project structure

```
.
├── api/
│   ├── webhook.py        # Vercel serverless version (webhook)
│   └── _private.py       # Optional shuttle commands, used by both versions
├── bus_arrival_bot.py    # Long-running version (polling), for your PC or a server
├── pyproject.toml        # Tells Vercel where the webhook entrypoint is
├── requirements.txt
├── .env                  # Your keys and stops (don't commit this)
├── shuttles.json         # Shuttle command names and timings (don't commit this)
└── README.md
```

## Prerequisites

- Python 3.10+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- An LTA DataMall API key ([request one here](https://datamall.lta.gov.sg/content/datamall/en/request-for-api.html))

## Setup

1. Install the dependencies:

   ```bash
   pip install -r requirements.txt
   ```

2. Create a `.env` file in the project folder:

   ```env
   TELEGRAM_TOKEN=your-telegram-bot-token
   LTA_API_KEY=your-lta-datamall-key
   WATCHLIST='[{"stop":"12345","name":"Home","service":"10"},{"stop":"67890","name":"Work","service":"20"}]'
   ```

   Each entry in `WATCHLIST` has:

   | Field     | Meaning                                          |
   | --------- | ------------------------------------------------ |
   | `stop`    | 5-digit bus stop code (printed on the stop sign) |
   | `name`    | Label shown in the bot                           |
   | `service` | Bus number to show at that stop                  |

   Keep `WATCHLIST` on a single line.

## Option A: run locally (polling)

```bash
python bus_arrival_bot.py
```

Then send `/arrival` to your bot in Telegram. The bot only responds while the script is running.

## Option B: deploy to Vercel (webhook)

1. Get the `WATCHLIST` value for Vercel by running this on your PC:

   ```bash
   python api/webhook.py
   ```

   It reads your stops from `.env`, adds their first/last bus times, and prints a line to copy.

2. In [Vercel](https://vercel.com/), click **Add New → Project** and import this GitHub repo.
3. In the Vercel project settings, add these environment variables:
   - `TELEGRAM_TOKEN`
   - `LTA_API_KEY`
   - `WATCHLIST`: paste the line from step 1 (no quotes around it)
   - `SHUTTLES` (optional): only if you use [private shuttle commands](#private-shuttle-commands)
4. Deploy, then open this URL once to connect Telegram to your deployment:

   ```
   https://<your-project>.vercel.app/api/webhook?setup
   ```

   Use your production domain (**Settings → Domains**), not a deployment URL with a random hash like `<project>-8b2ybvio7-<team>.vercel.app`. Deployment URLs sit behind Vercel's login, so Telegram can't reach them.

   You should see "✅ Connected!". This also adds `/arrival`, `/clear` and `/menu` to Telegram's command menu. Run it again whenever you add or rename a command.

5. Send `/arrival` to your bot.

You only need to do step 4 once. Pushing to `main` redeploys to the same domain, and the bot picks up the new code automatically.

The webhook rejects any request that doesn't carry a secret token. The secret is derived from your bot token, so there's nothing extra to configure.

> **Note:** Telegram sends a bot's updates either by polling or to a webhook, not both. Starting `bus_arrival_bot.py` removes the webhook, so the Vercel bot stops replying. To switch back, open `?setup` again.

## Changing the stops

Edit `WATCHLIST` in `.env`. The polling version picks up the change the next time you start it.

For the Vercel version, run `python api/webhook.py` again. Paste the new line into Vercel's `WATCHLIST` variable, then redeploy. Do the same if LTA changes the bus schedule.

## Private shuttle commands

`api/_private.py` adds shuttle timing commands that aren't in the public menu. The code is in the repo, but the command names and timings aren't. They're read from the `SHUTTLES` environment variable, or from `shuttles.json` in the project folder if that variable isn't set. `shuttles.json` is gitignored, so your commands stay secret even though the repo is public.

There are two kinds of command:

- **Route commands** show the next departures for every route tagged with that command.
- **The "from" command** takes a place, e.g. `/<from-command> <place>`, and shows the next departures leaving from there. The place is matched ignoring case and spaces. Sent without a place, it lists the places you can use.

The shuttle runs Monday to Friday, except on the dates in `off_days`. When there are no more shuttles today, the bot shows the first one on the next day it runs.

Example `shuttles.json` (the names are placeholders, so choose your own):

```json
{
  "show": {"my-route-cmd": 1, "my-other-cmd": 3, "my-from-cmd": 3},
  "from_command": "my-from-cmd",
  "routes": [
    {"command": "my-route-cmd", "from": "Place A", "to": "Place B", "times": ["0700", "0730"]},
    {"command": "my-other-cmd", "from": "Place B", "to": "Place A", "times": ["1710", "1740"]}
  ],
  "off_days": ["2026-12-25"]
}
```

| Field          | Meaning                                                               |
| -------------- | --------------------------------------------------------------------- |
| `show`         | Each command name (without `/`), and how many upcoming shuttles it shows |
| `from_command` | Which command in `show` works as the "from" command                   |
| `routes`       | One entry per route. `times` are 24-hour `HHMM` strings               |
| `off_days`     | Dates (`YYYY-MM-DD`) when the shuttle doesn't run                     |

To use the commands on Vercel, run this on your PC:

```bash
python api/_private.py
```

Paste the line it prints into Vercel's `SHUTTLES` variable, then redeploy. Do this again every time you edit `shuttles.json`.

The commands aren't added to Telegram's command menu, so only people who know the names can use them.

## Data source

Arrival times and bus routes come from LTA DataMall. Times are rounded down to the minute, as LTA recommends.
