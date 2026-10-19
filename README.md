# NavTracker

NavTracker reads tracker positions from Navixy and publishes them to ChatSurfer. It is a single Python process. `NAVIXY_MODE` selects how positions are read.

## Ingestion mode

Set `NAVIXY_MODE` to `websocket` or `poll`.

| Value | Behavior |
| --- | --- |
| `websocket` | Subscribe to the Navixy websocket for `TRACKER_IDS`. If the socket keeps failing, poll the REST API, then switch back once the socket stays healthy. This is the default when `NAVIXY_MODE` and `navixy.mode` are both unset. |
| `poll` | Poll `tracker/get_state` on an interval. The websocket is not opened and failover is disabled. |

Matching is case-insensitive. Any other value stops the process at startup with `Invalid NAVIXY_MODE=... Expected one of: websocket, poll.`

The environment variable overrides `navixy.mode` in the YAML file for `APP_ENV`. Development, staging, and production all set `mode: websocket`, so failover to polling is on unless `NAVIXY_MODE=poll`.

`TRACKER_IDS` is the tracker scope for both modes (comma-separated). A non-empty list is used as given and the tracker list API is not called. An empty list discovers ids from Navixy's `tracker/list` and uses those ids for websocket and poll. Discovery retries with exponential backoff, starting at 1 second and capped at 30 seconds. A failed or empty list never subscribes to every tracker. `NAVIXY_TRACKER_DISCOVERY_INTERVAL_SECONDS` (YAML `navixy.tracker_discovery_interval_seconds`) is how often that list is read again so new trackers are picked up. The default is 300 seconds. `0` discovers once at startup and does not refresh. The environment variable overrides the YAML value.

## Failover

Failover runs only when the mode is `websocket`. The process retries the socket with exponential backoff. After `NAVIXY_FAILOVER_MAX_ATTEMPTS` consecutive socket failures it switches to polling and logs `Ingestion mode switch: websocket -> poll`. While polling, it probes the websocket every `NAVIXY_FAILOVER_PROBE_INTERVAL_SECONDS`. When a probe stays connected for `NAVIXY_FAILOVER_HEALTHY_WINDOW_SECONDS`, it logs `Ingestion mode switch: poll -> websocket` and uses that socket again.

Only Navixy socket failures count toward that threshold, and only a socket failure reconnects Navixy. The client logs the socket exception with the websocket URL and tracker ids (not the API hash) before raising it.

A ChatSurfer failure is a publication error on both the websocket path and the poll path. The first attempt returns immediately so a dead publisher cannot stall the Navixy read. The latest unpublished fix per tracker is then retried off that loop, up to 3 attempts, with backoff starting at 0.5 seconds and capped at 2 seconds. After that budget the fix is logged and dropped. An unbounded retry buffer is not used: during an outage the useful payload is the newest fix, and a backlog would burst through the ChatSurfer rate limiter after recovery.

A fix is identified by tracker id and timestamp. The same pair is published once, including across a mode switch. If Navixy omits a timestamp, NavTracker reuses the last timestamp received for that tracker instead of the current clock, so the handoff does not invent a new fix. The first fix for a tracker that has never had a timestamp uses the local clock.

Backoff after failure `n` (1-based) sleeps `min(cap, initial * 2^(n-1))` seconds. A socket that opens resets the failure streak.

| Variable | YAML key | Default |
| --- | --- | --- |
| `NAVIXY_FAILOVER_MAX_ATTEMPTS` | `navixy.failover_max_attempts` | `5` |
| `NAVIXY_FAILOVER_BACKOFF_INITIAL_SECONDS` | `navixy.failover_backoff_initial_seconds` | `1` |
| `NAVIXY_FAILOVER_BACKOFF_CAP_SECONDS` | `navixy.failover_backoff_cap_seconds` | `30` |
| `NAVIXY_FAILOVER_PROBE_INTERVAL_SECONDS` | `navixy.failover_probe_interval_seconds` | `60` |
| `NAVIXY_FAILOVER_HEALTHY_WINDOW_SECONDS` | `navixy.failover_healthy_window_seconds` | `30` |

Values that are not positive, or a backoff cap below the initial delay, fail at startup. The environment variable overrides the YAML key.

See `.env.example` for every setting `load_settings` reads. A value in `.env` overrides the same key in `config/<APP_ENV>.yaml`.

## Encryption

`MODE` is `pem`, `ssh`, or `fernet` (`crypto.mode` in YAML). Startup checks only the material that mode uses. The error names the mode and the missing setting.

`pem` and `ssh` require `ENCRYPTION_CERT`. They do not require `FERNET_KEY` and do not validate it, so a blank or malformed Fernet key is ignored. `fernet` requires `FERNET_KEY` and passes that key into `CryptoHandler`. A missing key, a blank value, or the YAML literal `None` (PyYAML loads that literal as the string `"None"`, which is unset) fails at startup. A value that is not a url-safe base64-encoded 32-byte key fails only in fernet mode. The process does not generate a key.

## Run

```bash
cp .env.example .env
# fill in NAVIXY_API_KEY and CHAT_SURFER_API_KEY
docker compose up --build
```

`APP_ENV` selects `config/development.yaml`, `config/staging.yaml`, or `config/production.yaml`. `docker-compose.yml` passes `APP_ENV` into the container and uses `development` when the variable is unset. `APP_ENV=staging docker compose up --build` therefore loads the staging file.

## Tests

```bash
pip install -r requirements-dev.txt
python -m ruff check .
python -m mypy
python -m pytest
```

GitHub Actions runs those three commands on every push and pull request, and a failure there fails the workflow. A separate config job loads each environment, validates settings, and constructs the process objects. That job is warning-only: on failure it emits a GitHub Actions warning naming the environment and setting, and it does not fail the workflow or block merges. Run the check locally from the repository root:

```bash
python scripts/validate_config.py
python scripts/validate_config.py development
```

The check uses `config/development.yaml`, `config/staging.yaml`, and `config/production.yaml`, then the same environments under `config/analytics/`. API keys and the key material for the crypto mode in that file are dummies. The ChatSurfer client certificate is a local dummy pair, so `/data/certs` is not read. It does not open a Navixy, ChatSurfer, Clairvoyant, tile, or routing connection. An invalid setting exits locally with `APP_ENV=<environment> setting=<NAME>: ...`. In GitHub Actions the same line is a `::warning` annotation. The script exits non-zero when either the ingest check or the analytics check fails.

## Analytics backend

`analytics` is a second process. It does not read Navixy. It shares ChatSurfer, crypto, and settings helpers with the ingest worker through the `navtracker_common` package under `packages/navtracker_common`. Ingest modules re-export those helpers, so the ingest process behavior is unchanged.

```bash
docker compose up --build analytics
```

`APP_ENV` selects `config/analytics/<APP_ENV>.yaml`. The HTTP API listens on port 8080. Reference files (Player Data CSV/XLSX/KML and MSR KML/KMZ) are read from `ANALYTICS_REFERENCE_DIR` (`/data/reference` in Compose, mounted from `./reference`).

GPS can arrive from ChatSurfer rooms (`chatsurfer.read_rooms`) and, when enabled, from a Clairvoyant websocket with API failover. Staging and production enable Clairvoyant with placeholder URLs. Development leaves it off. The Clairvoyant message shapes are assumed and documented in `analytics/infrastructure/clairvoyant.py`.

TACREP rooms (`chatsurfer.tacrep_rooms` or `CHAT_SURFER_TACREP_ROOMS`) and Clairvoyant report layers (`clairvoyant.report_layers`, only when Clairvoyant is enabled) are non-GPS evidence. Reports are deduplicated, keep source, layer, and message id, and can raise site confidence.

Sites keep a set of types (`hide`, `launch`, `transload`), a status (`possible`, `probable`, `confirmed`), and a numeric confidence. Imports start at `possible` with confidence 0.2. Decay is off unless `analytics.confidence.decay_per_day` is greater than 0. The external assessment name `verified` means `confirmed`. Manual verification and the confidence thresholds can both reach `confirmed`. Operational status is separate: `unknown`, `active`, or `inactive`. Hex activity uses H3 resolution 9. MSR corridors default to a 50 meter buffer.

TACREP lines use `KIND-seq//time//lat,lon//key=value`. Kinds are `O` (launch), `I` (imagery), `J` (movement), `E` (electronic), and `UNKNOWN`. Activities move `pre_launch_hide` → `launch` → `hide_after_launch` → `transload` → `hide_after_transload`. An `O` message enters `launch`. A gap longer than 20 minutes enters the next hide state. A `J`, `I`, or `E` message enters `transload` from `hide_after_launch` when it shares a BMOA. Pair score is `0.45 * same_subject + 0.35 * spatial + 0.20 * time`, grouped by H3 cell and a 45-minute window. A group is `probable` with at least three sources and a best score of at least 0.70.

### Basemap

`NEXT_PUBLIC_MAP_PROVIDER` (or `analytics.map.provider`) is `leaflet` by default. Leaflet uses public OpenStreetMap tiles. `mapbox` uses style `mapbox/dark-v11` and requires `NEXT_PUBLIC_MAPBOX_TOKEN`. `none` serves no tiles. `xyz` requires `ANALYTICS_MAP_TILE_URL`. `osm` is accepted as `leaflet`. The default view is latitude 39.8283, longitude -98.5795, zoom 4. The choice is included in the WebSocket snapshot and the OGC landing page. No API key is written into the repository.

### Routing

`ROUTING_PROVIDER` (or `analytics.routing.engine`) is `osrm` by default, at `https://router.project-osrm.org`, with a 15 second timeout. `mapbox` uses the Mapbox Directions API and the same token as the basemap. `valhalla` and `graphhopper` need `ANALYTICS_ROUTING_BASE_URL`. `msr` routes along imported MSR corridors and makes no network call. Vehicle types are `truck` and `intermodal`. `rail` returns an error. `light` and `heavy` are extra size profiles. ETA minutes are `max(engine minutes, miles / maxSpeedMph * 60)` with `maxSpeedMph` capped at 120, then multiplied by the profile ETA factor (1.0 for truck and intermodal). OSRM and Mapbox do not receive height, weight, or hazmat. Valhalla receives a top speed only. GraphHopper receives the profile name. Successful engine responses are cached. `POST /api/routes` accepts `{originSiteId, destinationSiteId, vehicleType, maxSpeedMph}`. `GET /routing?from_site=&to_site=&profile=truck` returns the same line and ETA. The route is also `GET /ogc/collections/itineraries/items`.

### Outputs

- `GET /ws` sends a versioned snapshot (`navtracker.analytics` version 1) and then diffs.
- `GET /ogc` is an OGC API - Features landing page. Collections are sites, cells, routes, evidence, and itineraries. Item requests accept `bbox` and `datetime`.
- `GET /geojson`, `GET /heatmap`, and `GET /kml` export the current picture. `GET /kml/networklink` is a KML NetworkLink pointed at `/kml`.
- `GET /api/sites` returns `{sites}` with operational `status` and assessment `possible`, `probable`, or `verified`.
- `POST /api/routes` returns `{provider, routes}`.
- `POST /v1/events`, `POST /v1/events/tacrep`, and `POST /v1/events/batch` accept TACREP.
- `GET /v1/events/correlations` and `GET /v1/activities` return the current groups. `GET /v1/activities/stream` sends one server-sent event. Clients can poll `/v1/activities` every 10 seconds.
- `POST /v1/activities/{id}/advance` moves one activity one state. Errors are `{error, code}`.

OGC SensorThings, WMS/WMTS, and vector tiles are not implemented.

The optional frontend is off unless `frontend.enabled` is true, which also requires `ANALYTICS_FRONTEND_INGEST_URL`. The process then POSTs the same snapshot and diff documents to that URL. `docker compose --profile frontend up analytics-frontend` serves `analytics/frontend/index.html` on port 8081. That page is a NavTracker viewer for `/ws` and `/routing`. The `/api` and `/v1` routes are the adapter a TransloadOracle frontend can call.

## Rollback and emergency operation

Redeploy the image built from the annotated tag `pre-ddd-refactor` (`91265630ea2f0e21e6975936e15abb5de2da3775`). That tag is the last `master` commit before the ingestion refactor.

To undo the merged stack in git, revert these merge commits newest first. `-m 1` keeps `master` as the mainline:

```bash
git revert -m 1 7d37b10a602e22df857de77888999effe66e7358
git revert -m 1 ee9338f066926891d3b14301105d4c1786dcf166
git revert -m 1 1b30015013aaf7d82eaf55f56c9d0561f5c12640
git revert -m 1 55f3aa72eb64b255d740bb34b8d9409fcffb0cb6
git revert -m 1 2bb4b123f7a26e2492ae01618a22128e965cd367
git revert -m 1 706e447a6d8ca231507c0bfc3dd78997aba01a92
```

To force polling, set `NAVIXY_MODE=poll` in the process environment (in `.env` when using Docker Compose) and restart. The variable overrides `navixy.mode` in the YAML, the websocket is not opened, and failover stays off. The running process does not reload it. Unset the variable and restart to follow the YAML mode again.
