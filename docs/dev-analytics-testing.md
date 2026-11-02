# Development analytics testing

Use this when ChatSurfer is not available. Development ingest does not
read Navixy. Analytics can still be filled from TACREP text produced by
[navtracker_tacrep](https://github.com/mawood33/navtracker_tacrep) and
from a Navixy JSON replay. TransloadOracle is a separate program. It
reads this API when it wants to. NavTracker does not call it.

Commands are `python` invocations. They do not use a shell `NAME=value`
prefix, so the same lines work in VS Code's terminal on Windows
(PowerShell or Command Prompt) and on Linux. `python -m analytics.main`
and `python -m app.main` load `.env` from the working directory when
that file exists. A variable already set in the environment is kept.

## What you can exercise

| Goal | Input | Where it shows up |
| --- | --- | --- |
| TACREP parse, tracks, correlations | `fixtures/dev/tacrep_track.txt` (also `.json`) | `GET /tracks`, `GET /v1/events/correlations`, the dashboard Tracks list |
| Navixy-shaped fixes | `fixtures/dev/navixy_halt.json` | `POST /v1/dev/fixes` (development only), then the same tracks |
| Transload near an MSR | The TACREP fixture plus `fixtures/dev/reference/` | `GET /aois` and the dashboard Sites list after analysis |
| An external consumer | The same running API | See TransloadOracle below |

`fixtures/dev/tacrep_track.txt` is five MTI lines in the companion shape:

```text
REAPER 21//TACREP 1//1 MVR//BULL-1//01000//N3712.34 E12812.34//MSR BURGUNDY//-//1910Z//1 MVR, 9 MPH, SW IVO 10105
REAPER 21//TACREP 2//1 MVR HALT//BULL-1//01000//N3713.10 E12812.00//MSR BURGUNDY//-//1915Z//1 MVR HALT IVO 10105
```

`TACREP 1` is the serial. `01000` is the LINK-16 track number and stays
constant for REAPER 21, so those reports are one track `link16:01000`.
A track number is exactly five letters or digits, kept as written aside
from upper case (`A1B2C` and `a1b2c` are `link16:A1B2C`). It is not a
decimal or octal number, and it is not zero-padded: `1000` is four
characters, so that field is ignored and a warning is logged. REAPER 22
uses `01001`. `1 MVR` is one moving vehicle. `SW` is heading 225. `9 MPH`
is stored as meters per second. `1 MVR HALT` has no speed or direction;
the track point is 0 m/s. `IVO 10105` is the place name.
`MSR BURGUNDY` is the route. The two halts at 1915Z and 1920Z are five
minutes apart, which is long enough for a stop. A second mover halts
beside site `10105` on that MSR, which is the transload case.

Checked-in sample files:

- `fixtures/dev/tacrep_track.txt`
- `fixtures/dev/tacrep_track.json`
- `fixtures/dev/navixy_halt.json` (`gps.speed` is km/h; `36` becomes 10 m/s)
- `fixtures/dev/reference/sites.csv` (site `10105`, type `transload`)
- `fixtures/dev/reference/msr_burgundy.kml`

## 1. Local analytics process

From the repository root:

```text
python -m pip install -r requirements-dev.txt
```

Copy the example environment and edit `.env` in an editor (not with a
shell prefix):

```text
copy .env.example .env
```

On Linux the copy is `cp .env.example .env`. Set these lines in `.env`:

```text
APP_ENV=development
CHAT_SURFER_API_KEY=dev
CHAT_SURFER_ENCRYPTION_REQUIRED=false
CHAT_SURFER_READ_ENABLED=false
ANALYTICS_READ_TOKEN=dev
ANALYTICS_WRITE_TOKEN=dev
ANALYTICS_COORD_CARD=config/analytics/coord_card.example.yaml
ANALYTICS_REFERENCE_DIR=fixtures/dev/reference
LOG_FILE=logs/analytics.log
```

`LOG_FILE=logs/analytics.log` keeps the log in the repository `logs/`
directory. Leave it unset only inside Docker, where the default
`/app/logs/analytics.log` matches the Compose mount. An empty
`LOG_FILE=` keeps stdout only. `CHAT_SURFER_API_KEY=dev` satisfies the
required variable; reads stay off, so the key is not sent.

`CHAT_SURFER_ENCRYPTION_REQUIRED=false` and `CHAT_SURFER_READ_ENABLED=false`
let the process start without a client certificate and without calling
ChatSurfer. A configured `read_rooms` list does not by itself open a
client. Turning reads or event publish on again requires the certificate
paths in the analytics YAML to exist.
Empty API tokens fail closed: replay and the dashboard need `dev`.

Start analytics:

```text
python scripts/run_analytics.py
```

`scripts/run_analytics.py` puts the repo and `packages/navtracker_common`
on `sys.path`, then loads `.env` and serves `http://127.0.0.1:8080`.
`GET /health` answers before any external system is contacted:

```text
python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/health').read().decode())"
```

The payload has no TransloadOracle status. NavTracker does not know
whether that program is running.

Docker on Windows is the same `.env` with one difference: do not set
`ANALYTICS_REFERENCE_DIR` in `.env` if you use Compose, because the
analytics service already mounts `./reference` at `/data/reference`.
Copy the fixture files into `reference/` first:

```text
mkdir reference
copy fixtures\dev\reference\sites.csv reference\
copy fixtures\dev\reference\msr_burgundy.kml reference\
docker compose up --build analytics
```

Compose sets `ANALYTICS_HTTP_HOST=0.0.0.0` and publishes port 8080.
Inside the container the coord card path is
`config/analytics/coord_card.example.yaml` (the image contains `config/`).

## 2. Replay TACREP

With the server running, from a second terminal at the repository root:

```text
python scripts/replay_tacrep.py --file fixtures/dev/tacrep_track.txt --token dev
```

The script POSTs the file to `/v1/events/tacrep`, then POSTs
`/v1/dev/analyze` so stops and sites update immediately. Outside
`APP_ENV=development` that analyze route is not found and the script
exits non-zero. `--no-analyze` skips it; the analysis loop still runs
every `analytics.analysis_interval_seconds` (30 in development).

JSON and CSV work the same way:

```text
python scripts/replay_tacrep.py --file fixtures/dev/tacrep_track.json --token dev
```

A CSV file uses a `tacrep` column when that header is present, and the
first column otherwise.

Check the track:

```text
python -c "import urllib.request; print(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8080/tracks', headers={'Authorization': 'Bearer dev'})).read().decode())"
```

You should see `link16:01000` and `link16:01001`. Correlations:

```text
python -c "import urllib.request; print(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8080/v1/events/correlations', headers={'Authorization': 'Bearer dev'})).read().decode())"
```

Areas:

```text
python -c "import urllib.request; print(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8080/aois', headers={'Authorization': 'Bearer dev'})).read().decode())"
```

The halt cluster matches site `10105` and MSR `MSR BURGUNDY`, and the
area type includes `transload`.

## 3. Replay Navixy JSON

There is no live Navixy socket in this path. `POST /v1/dev/fixes`
accepts a Navixy `state_batch`, a `tracker/get_state` body, or
`{"fixes": [...]}`. It exists only when `APP_ENV=development`. Any
other environment returns 404. Navixy `gps.speed` is kilometers per
hour. Live ingest and this replay convert it to meters per second
(`36` km/h becomes 10 m/s) for stored fixes and analytics tracks.
Objects that already contain `speed_mps` stay meters per second.
`ANALYTICS_CHAT_SPEED_UNIT` defaults to `kmh`: that is the unit of the
`speed` field published to ChatSurfer and the unit analytics uses when
it reads that field. `mps` switches both sides to meters per second.
Set it in `.env` so the ingest process and analytics use the same value.

```text
python scripts/replay_navixy.py --file fixtures/dev/navixy_halt.json --token dev
```

The sample is tracker `21` stopped, then moving at 36 km/h. Both points
use vehicle id `21`.

A captured `get_state` body (`{"success": true, "state": {...}}`) can
be passed to the same script. The worker's live websocket and poll
loops are unchanged; this route does not open Navixy.

## 4. Dashboard

In a third terminal:

```text
python -m http.server 8081 --directory analytics/frontend
```

Open:

```text
http://127.0.0.1:8081/?token=dev&api=http://127.0.0.1:8080&ws=ws://127.0.0.1:8080/ws
```

Click Connect. The page lists sites and track points from the snapshot.
`token=dev` is sent on the WebSocket and on the route request. The write
token also grants read.

`config/analytics/development.yaml` allows the browser origins
`http://127.0.0.1:8081` and `http://localhost:8081`. A browser on another
origin is rejected until `ANALYTICS_WS_ORIGINS` lists it
(comma-separated). That variable replaces the YAML list when it is set.
Staging and production leave the list empty, so a browser `Origin`
header is rejected there. An allowed origin also receives
`Access-Control-Allow-Origin` on HTTP responses, which is what the
route button needs. Other origins do not.

With Docker the same page is:

```text
docker compose --profile frontend up analytics-frontend
```

That container serves the dashboard on port 8081.

## 5. TransloadOracle as a consumer

TransloadOracle is a separate application. NavTracker does not import
it, configure it, or send it anything. Hide, launch, and transload
rules, the H3 grid, and routing are NavTracker behavior. They run
whether or not TransloadOracle is installed.

When you want that screen against this API, it pulls:

- `GET /api/sites`, `GET /cells`, `GET /msrs`, `GET /events`
- `GET /api/tracks` and the OGC tracks collection
- `GET /v1/activities`, `GET /v1/events/correlations`
- `GET /geojson`, `GET /ogc`
- the `/ws` snapshot, using the read token (the write token also grants read)

It has no inbound endpoint for NavTracker to call.

In the TransloadOracle repo, standalone development is:

```text
npm run dev:standalone
```

To point it at this process:

```text
DATA_SOURCE=navtracker
```

with `NAVTRACKER_API_URL`, `NAVTRACKER_WS_URL`, and the read and write
tokens set to this analytics server (for the steps above,
`http://127.0.0.1:8080`, `ws://127.0.0.1:8080/ws`, and token `dev`).
Set those in that program's environment. They are not NavTracker
settings.

To exercise a transload on this side without that program, replay
`fixtures/dev/tacrep_track.txt` against `fixtures/dev/reference/` as in
section 2. Two movers halt on MSR BURGUNDY next to site 10105.

## Tests

```text
python -m ruff check .
python -m mypy
python -m pytest
```

Focused cases:

```text
python -m pytest tests/test_tacrep_mvr.py tests/test_dev_replay.py tests/test_navixy_parse.py tests/test_envfile.py
```

## Not in this pass

- A fake ChatSurfer websocket that replays a room. Development leaves
  `CHAT_SURFER_READ_ENABLED` false and the replay scripts POST the HTTP
  API instead.
- A live Navixy websocket simulator. The development route accepts a
  saved `state_batch` or `get_state` body.
- A copy of TransloadOracle. It consumes the API above. This repo does
  not start it.
- The private companion generator is not vendored. New TACREP text from
  it should be dropped in as another `--file`. If a future export uses
  a different serial than `TACREP <n>` or a different field order than
  the MTI card, the parser will need that layout added beside the
  current one.
