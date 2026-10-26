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

Navixy's websocket speaks Atmosphere. On connect, missing handshake query parameters are added: `X-Atmosphere-tracking-id=0`, `X-Atmosphere-Framework=2.3.6-javascript`, `X-Atmosphere-Transport=websocket`, `Content-Type=application/json`, and `X-atmo-protocol=true`. The configured path stays as it is (`wss://ws.navixy.com/events` by default). An existing `X-Atmosphere-TrackMessageSize` query parameter is left as written; the client does not add one. A frame that is `X` or blank, and the opening `uuid|interval|X|` handshake, keep the socket alive and are logged at DEBUG. A length prefix `<len>|` is removed, including when one frame carries several messages. The subscribe message asks for `state_batch` and `readings_batch` on the selected tracker ids. A `state_batch` snapshot is the current state of those trackers. Readings, lifecycle, and logout frames stay unpublished. A payload that is still unparseable after that is logged at WARNING with an 80-character preview.

A ChatSurfer failure is a publication error on both the websocket path and the poll path. The first attempt returns immediately so a dead publisher cannot stall the Navixy read. The latest unpublished fix per tracker is then retried off that loop, up to 3 attempts, with backoff starting at 0.5 seconds and capped at 2 seconds. After that budget the fix is logged and dropped. An unbounded retry buffer is not used: during an outage the useful payload is the newest fix, and a backlog would burst through the ChatSurfer rate limiter after recovery.

A fix is identified by tracker id and timestamp. The same pair is published once, including across a mode switch. Navixy timestamps are stored as timezone-aware UTC. A value with no offset is UTC, and a value with an offset is converted to UTC, on both the websocket and the poll path. The same instant is therefore one fix even when one path omits the offset. If Navixy omits a timestamp, NavTracker reuses the last timestamp received for that tracker instead of the current clock, so the handoff does not invent a new fix. The first fix for a tracker that has never had a timestamp uses the local clock.

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

## Logs

The worker and analytics each write the same log lines to stdout and to a rotating file. Tracebacks are included. `docker-compose.yml` bind-mounts `./logs` at `/app/logs`, which works for a Windows or Linux Docker host. The files are `navtracker.log` and `analytics.log`. Leave `LOG_FILE` unset to use those defaults. Set `LOG_FILE` empty to keep stdout only. A shared `.env` value applies to both services, so set a per-service path only when each process has its own environment.

| Variable | Default | Meaning |
| --- | --- | --- |
| `LOG_FILE` | `/app/logs/navtracker.log` or `/app/logs/analytics.log` | File path. Empty disables the file. |
| `LOG_LEVEL` | `INFO` | Root logger level (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`). |
| `LOG_MAX_BYTES` | `10485760` | Rotate after this many bytes. `0` does not rotate. |
| `LOG_BACKUP_COUNT` | `5` | Older files to keep (`navtracker.log.1`, and so on). |
| `LOG_POSITIONS` | `true` | Log each tracker fix before publish. `false` keeps the lines at DEBUG. `debug` raises `navtracker.positions` to DEBUG. |

Each fix is logged on `navtracker.positions` before timestamp resolution, dedupe, and publish. The line names the tracker id, the source (`websocket` or `poll`), the timestamp as received with its type and tzinfo, the normalized UTC timestamp, latitude, longitude, speed, heading, the previous cursor it is compared with (including that cursor's type and tzinfo), and the dedupe decision (`publish`, `duplicate`, or `drop`). The same line goes to stdout and the log file. API keys and encrypted payloads are not written.

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

`APP_ENV` selects `config/analytics/<APP_ENV>.yaml`. The HTTP API listens on port 8080. `GET /health` is answered as soon as that socket is bound. A stalled or unreachable ChatSurfer or Clairvoyant source does not delay it. The log line `Analytics HTTP listening` is followed by whether each source is disabled, connecting, or failed, which is what `docker compose logs analytics` shows during startup. Reference files (Player Data CSV/XLSX/KML and MSR KML/KMZ) are read from `ANALYTICS_REFERENCE_DIR` (`/data/reference` in Compose, mounted from `./reference`).

GPS truth fixes can arrive from the ChatSurfer truth room (`room_name` or `room_name_encrypted`, chosen by `CHAT_SURFER_ENCRYPTION_REQUIRED`) and, when enabled, from a Clairvoyant websocket with API failover. Uploaded reference files are not written to that room. Staging and production enable Clairvoyant with placeholder URLs. Development leaves it off. The Clairvoyant message shapes are assumed and documented in `analytics/infrastructure/clairvoyant.py`.

ChatSurfer's read, search, websocket, and data-spill contract is summarized in `docs/chatsurfer-api.md` (REST API 4.24.0). The room read is `GET /api/chat/messages/{domainId}/{roomName}`. A 404 means the room is empty or missing. `CHAT_SURFER_INGEST_MODE` is `websocket` (STOMP 1.2 on `wss://<host>/ws/connect`) or `poll`. Development leaves reads off (`read_enabled: false` / `CHAT_SURFER_READ_ENABLED`) so the process starts without calling ChatSurfer. The path stays overridable with `CHAT_SURFER_READ_PATH`, and the default is the documented route. A scrubbed message id is removed from memory, the live snapshot, and WebSocket clients (`type: retract`). The log line is the id only.

TACREP rooms (`chatsurfer.tacrep_rooms` or `CHAT_SURFER_TACREP_ROOMS`) and Clairvoyant report layers (`clairvoyant.report_layers`, only when Clairvoyant is enabled) are non-GPS evidence. Reports are deduplicated, keep source, layer, and message id, and can raise site confidence.

Sites keep a set of types (`hide`, `launch`, `transload`), a status (`possible`, `probable`, `confirmed`), and a numeric confidence. Imports start at `possible` with confidence 0.2. Decay is off unless `analytics.confidence.decay_per_day` is greater than 0. `verified` is accepted on input and stored as `confirmed`. Exports write `confirmed`. Manual verification and the confidence thresholds can both reach `confirmed`. Operational status is separate: `unknown`, `active`, or `inactive`. Hex activity uses H3 resolution 9. MSR corridors default to a 50 meter buffer.

TACREP text is read from the exercise coord card first. `ANALYTICS_COORD_CARD` or `analytics.coord_card` is a YAML, JSON, or CSV file. The format is the file extension, then the `Content-Type` (`application/json`, `text/yaml`, `text/csv`, and the `application/yaml` / `application/csv` aliases), then a sniff of the body. `POST /v1/coord-card` uploads a replacement with the write token. The body is checked before the file is replaced, so a bad upload leaves the previous card in place. A sample with fake block numbers is `config/analytics/coord_card.example.yaml` (the same rows in `coord_card.example.csv` and `coord_card.example.json`). It is not loaded unless that path is configured.

Each card has a `schema_version`. The bundled catalog is version `1` (`analytics/domain/schemas/coord_card.schema.json`, Draft 2020-12). A card that omits the field uses the catalog default. Unknown fields and template properties are allowed, so a new exercise key does not by itself need a new schema. `ANALYTICS_COORD_CARD_SCHEMA` or `analytics.coord_card_schema` points at a replacement catalog. When that path is unset, a file named `coord-card.schema.json` in the reference directory is used if it exists; otherwise the bundled catalog is used. `POST /v1/coord-card/schema` (write token, same upload size cap as the card) replaces the catalog or, with `X-Schema-Version`, adds one schema beside the versions already loaded. The upload must be valid Draft 2020-12 JSON Schema, and the card already in memory must still validate against the version it selected. A failure keeps the previous schema and is logged. A success logs the version ids before and after. Several versions live side by side; the card's `schema_version` picks one.

A card lists each type name, one or more kind letters, an inclusive serial range, and ordered fields. The sample types are MTI (`J`), IMINT (`I`), ELINT (`E` and `G`, including COMEXT), and FUSED (`F`). Another exercise adds a type or extra field names; names the parser does not know stay on the message as attributes. Fields are separated by `//`, with optional spaces and a stray apostrophe before the separator. One chat message may hold a header plus continuation lines, or several TACREPs. The sample field order is:

- MTI: callsign, TACREP id, activity (`ROTATOR`, `MOVER`, or `MILLING`), bullseye location, Link-16 track, location, TAI and/or MSR, CAT, time of intercept, fill-ins.
- IMINT: callsign, TACREP id, equipment name, location, elevation, TAI, time of image, CAT.
- ELINT: callsign, TACREP id, entity or system, activity (`ACTIVE` or `AWAKE`), location, TAI, ellipse semi-major/semi-minor, magnetic orientation, CAT, time of intercept, fill-ins. A TAI glued to the location with a comma is accepted, and a following TAI field wins when both are present.
- FUSED: callsign, id, subject, location, CAT, time, correlates. `UPDATE G-501` in the id field reissues that base id. `CORRELATES TO J-401, G-501` stores those links.

Location accepts decimal degrees, MGRS, and hemisphere `DDMM.MM` (`N6919 W11919` is 69°19' N, 119°19' W; `N3712.34 E12812.34` uses decimal minutes). Latitude and longitude win over MGRS. Published ChatSurfer text uses a different format so the point maps. The default is 1 m MGRS (`11WMS0869991399` for that same point; spaces optional, as in `11W MS 08699 91399`). `latlon` writes decimal degrees ChatSurfer recognizes (`69.31667N 119.31667W` by default; decimals, a space or comma separator, and `suffix` or `signed` hemisphere are configurable). `ddmm` such as `N6919 W11919` is emitted only when selected, and that choice logs a warning. The received token stays on the stored message and in evidence. Precedence is an analyst override, then `output` on the coord card (per type, with `TACREP` covering MTI, IMINT, ELINT, and FUSED), then `ANALYTICS_OUTPUT_COORD_FORMAT` and the matching precision keys, then `mgrs`. `GET` and `PUT`/`POST /v1/settings/output-coordinates` read and set the override (`{"use_coord_card": true}` reverts it). It is stored in `output-coordinates.json` under the reference directory. `GET /health` and the WebSocket snapshot expose the effective format. See `docs/analytics-document.md`. A disagreement beyond `mismatch_meters` (default 250, or the card's own value) is a warning. Bullseye text is stored raw. `bullseye.name`, `bullseye.latitude`, and `bullseye.longitude` on the card are the hook for a reference point; nothing is projected until that point is set. Ellipse length uses `ellipse_unit` (`nmi`, `km`, or `meters`; `ANALYTICS_ELLIPSE_UNIT`, default nautical miles, 1852 m). A unit written on the field (`10/4 NM`, `2/1 km`, `2000/1000 m`) overrides the card. Stored lengths are meters. Magnetic orientation plus `magnetic_declination_degrees` (`ANALYTICS_MAGNETIC_DECLINATION`, default 0) becomes a true azimuth. Positive declination means magnetic north is east of true north. A blank or `-` orientation keeps the ellipse and treats the semi-major axis as a circle. A point inside that ellipse, or inside the CAT circular error, matches spatially, including across H3 cells. `-` and a blank token are null on every field.

`1919Z` is HHMM Zulu and `011919Z` is day, hour, and minute. Both are placed on the chat message's date, rolling across midnight when the clock is more than 12 hours away. CAT is a target-location-error category. A lower number is more accurate: CAT I is the tightest. `cat_ce` maps each category to a circular-error radius and a unit (`m`, `km`, or `nmi`). The built-in radii are CAT I 6 m, II 15 m, III 30 m, IV 91 m. `ANALYTICS_CAT_CE` accepts the same compact form (`I:6m,II:15m,III:30m,IV:91m`) and the card replaces it when `cat_ce` is present. That radius is the spatial uncertainty used for matching. Fill-ins read a vehicle count, a speed with a unit (miles per hour, knots, km/h, or m/s), a heading, and `HALT`. Card speed fields stay miles per hour and are stored as meters per second. `MILLING` becomes evidence kind `mill`, `HALT` becomes `stop`, `MOVER` becomes `movement`, and `ROTATOR` becomes `rotator`. A TAI or MSR token is linked to an imported area, route, or site when the name matches. `VACANT` is negative evidence (`ANALYTICS_VACANT_EVIDENCE_WEIGHT`, default 0.25) and can lower site activity confidence.

`POSS LOW`, `POSS HIGH`, `PROB` / `PROBABLE`, and `CONFIRMED` are read from TACREP text (for example `POSS HIGH TEL`). `CERTAIN` and `VERIFIED` are aliases of `CONFIRMED`. The phrase becomes the reported confidence and the remaining words stay the entity. Defaults: POSS LOW → possible/low (0.25), POSS HIGH → possible/high (0.45), PROB → probable (0.70), CONFIRMED → confirmed (1.0). A numeric confidence is unchanged. `confidence_terms` on the card, or `ANALYTICS_CONFIDENCE_TERMS` (`PHRASE:level:band:weight` items separated by `;`), replaces that table. NavTracker keeps the phrase parsed from that message and a second assessed phrase after correlation. `assessment_text` on the card templates the ChatSurfer remark (`single`, `correlated`, `disagreement`, `republish`). A single stated level is `SINGLE SOURCE`. A level raised by other reports is `ASSESSED` and names those serials. A stated `CONFIRMED` that correlation does not support stays on the report, the assessed level is shown beside it, and `disagreement` is true. The same fields are on site, correlation, and evidence rows. See `docs/analytics-document.md`.

Serial ids match `^[A-Z]-\d{3}[A-Z]?$`. The base id (`I-103`) is the identity, scoped by source and UTC day. A higher suffix replaces the earlier version and keeps it as history. `UPDATE <id>` ranks above a letter suffix and supersedes that base id. An older suffix does not overwrite a newer one. The same full id is deduplicated. An id outside the card's block, or an unknown kind letter, is still accepted and the evidence warning says so. There is no built-in block range. A subject is set only from the subject field.

`CORRELATES TO` links are copied onto the evidence (`linked_ids`). They add `ANALYTICS_CORRELATION_EXPLICIT_LINK_WEIGHT` (default 0.5) to the pair score, capped at 1, and the same weight is an unscaled `explicit_link` confidence row. When one TACREP's ellipse or circular error contains another inside the correlation window, the service emits a suggested FUSED correlation. That object names the highest-accuracy TACREP as `anchor_id` (lowest CAT, then the smallest ellipse area or CE), lists the linked ids, and sets `sent` to false. It is not stored as a received message. The WebSocket snapshot and `GET /v1/events/correlations` both include `suggested_fused`.

The card's `templates` list adds report shapes without a code change. A template has a name, a detection regex, a separator (`lines`, `comma`, or `slashes`), ordered fields (`name`, `type`, optional `line` or `pattern`), and an evidence mapping (`tasking` or `report`). The sample defines ADHOC and SITREP. ADHOC is a six-line tasking: classification banners are stored and copied on republish, the id is the `DDMMMYY` plus serial inside the START and END markers (a mismatch is a warning), and the numbered lines are effects, assets, target name/id, location plus elevation, EEI equipment, and reporting instructions (TAI, room, and `VACANT`). A later TACREP or SITREP that matches the tasking by place, TAI, equipment, or time is `response` evidence. SITREP is comma-separated and tolerant: a token is placed by what it looks like (MGRS or another location, `HHMMZ`, a frequency, a heading, `ACTIVE`/`AWAKE`, or `<equipment> at <place>`). SITREP evidence has no CAT unless the text carries one. `-` and blank fields are null. Zero-width characters are removed. Info logs name the template and its id. The raw body, including classification banners, is debug only.

ChatSurfer ingest and publish rooms are chosen per message type. Precedence is the coord card room on that type or template, then `ANALYTICS_ROOMS_<TYPE>_INGEST` or `ANALYTICS_ROOMS_<TYPE>_PUBLISH`, then the card-level `rooms` list when the card sets that side, then `ANALYTICS_ROOMS_DEFAULT_INGEST` or `ANALYTICS_ROOMS_DEFAULT_PUBLISH`. `CHAT_SURFER_TACREP_ROOMS` is the ingest default when `ANALYTICS_ROOMS_DEFAULT_INGEST` is unset. An explicit empty list does not fall through. When every step is empty, that type is not published. `CHAT_SURFER_ROOM_NAME` and `CHAT_SURFER_ROOM_NAME_ENCRYPTED` (defaults `freedom_flag` and `freedom_flag_encrypted`) are the GPS truth rooms. They are set in the env or config file, they are not coord-card rooms, and uploaded files do not use them. The worker publishes tracker fixes there, and analytics reads those fixes from that same room. `read_rooms` does not choose it. `CHAT_SURFER_ENCRYPTION_REQUIRED` (`true`, `yes`, or `1`) applies only to that truth room: reads and publishes use the encrypted name and the crypto handler (`pem`, `ssh`, or `fernet`). `false`, `no`, or `0` uses the plain room in plaintext. A missing encrypted room fails config check and startup. TACREP, including MTI `J-###`, plus ADHOC and SITREP, publish and read on the coord-card rooms or the `ANALYTICS_ROOMS_*` fallbacks. Those rooms stay as named, in plaintext, and this flag does not encrypt them. Publish rules (`rooms.publish` on the card, or `ANALYTICS_PUBLISH_RULES`) are ordered. The first rule whose confidence (`POSS LOW`, `POSS HIGH`, `PROB`, `CONFIRMED`; `CERTAIN` and `VERIFIED` match `CONFIRMED`), CAT range, type, and source all match supplies the rooms. The phrase compared is the assessed level. Several levels may share a room. A matching rule wins over the per-type room. `ANALYTICS_PUBLISH_REPUBLISH_ON_HIGHER_LEVEL` (default true) sends again when the assessed rank rises, and the remark names the previous phrase and the new one. Publishing is off when no room is configured. The remark is appended inside the last `//` field, or after a comma, and does not add a `//`. The shared ChatSurfer client is used: one immediate attempt and two retries (0.5 s, then 1 s, capped at 2 s), then the notice is logged and dropped without failing the room read. Uploading a card validates it first, keeps the previous card when validation fails, and hot-reloads ingest subscriptions and publish routes together. Room-subscription changes are logged. The sample card uses fake room ids.

`KIND-seq//time//lat,lon//key=value` remains a fallback when the body is not a coord-card record. Those kinds are `O` (launch), `I` (imagery), `J` (movement), `E` (electronic), `G` (ELINT), `F` (FUSED), and `UNKNOWN`. The HTTP source is `X-Source-Id` or JSON `source_id`, not the word `tacrep`. A parsed TACREP is correlated once and is not also stored as a legacy report.

Activities move `pre_launch_hide` → `launch` → `hide_after_launch` → `transload` → `hide_after_transload`. An `O` message enters `launch`. A gap longer than 20 minutes enters the next hide state. A `J`, `I`, `E`, `G`, or `F` message enters `transload` from `hide_after_launch` when it shares a BMOA. Sessions group messages whose span from the first message stays inside 45 minutes and whose cells are the anchor, an H3 neighbor, inside one another's ellipse, or explicitly linked. Pair score is `0.45 * same_subject + 0.35 * spatial + 0.20 * time`, plus the explicit-link weight when a message names the other. A group is `probable` with at least three sources and a best score of at least 0.70. Evidence rows include `tacrep_type`, `block`, `warning`, and `linked_ids`.

### Basemap

`NEXT_PUBLIC_MAP_PROVIDER` (or `analytics.map.provider`) is `leaflet` by default. Leaflet uses public OpenStreetMap tiles. `mapbox` uses style `mapbox/dark-v11` and requires `MAPBOX_PUBLIC_TOKEN` (or `NEXT_PUBLIC_MAPBOX_TOKEN`). That public token is the only Mapbox value placed in a tile URL. `none` serves no tiles. `xyz` requires `ANALYTICS_MAP_TILE_URL`. `osm` is accepted as `leaflet`. The default view is latitude 39.8283, longitude -98.5795, zoom 4. The choice is included in the WebSocket snapshot and the OGC landing page. No API key is written into the repository.

### Routing

`ROUTING_PROVIDER` (or `analytics.routing.engine`) is `osrm` by default, at `https://router.project-osrm.org`, with a 15 second timeout. `mapbox` uses the Mapbox Directions API and `MAPBOX_SERVER_TOKEN` (or `ANALYTICS_MAPBOX_TOKEN`). That server token is not sent to clients and does not fall back to the public map token. `valhalla` and `graphhopper` need `ANALYTICS_ROUTING_BASE_URL`. `msr` routes along imported MSR corridors and makes no network call. Vehicle types are `truck` and `intermodal`. `rail` returns an error. `light` and `heavy` are extra size profiles. ETA minutes are `max(engine minutes, miles / maxSpeedMph * 60)` with `maxSpeedMph` capped at 120, then multiplied by the profile ETA factor (1.0 for truck and intermodal). OSRM and Mapbox do not receive height, weight, or hazmat. Valhalla receives a top speed only. GraphHopper receives the profile name. Successful engine responses are cached. `POST /api/routes` accepts `{originSiteId, destinationSiteId, vehicleType, maxSpeedMph}`. `GET /routing?from_site=&to_site=&profile=truck` returns the same line and ETA. The route is also `GET /ogc/collections/itineraries/items`.

### Outputs

- `GET /ws` sends a versioned snapshot (`navtracker.analytics` version 2) and then diffs. Version 2 is documented in `docs/analytics-document.md`. `status` on a site is operational (`unknown`, `active`, or `inactive`). `assessment` is `possible`, `probable`, or `confirmed` (`verified` is accepted on input and written as `confirmed`). `confidence_band` repeats that band and is deprecated. Sites, correlations, and evidence also include `reported_level`, `reported_phrase`, `assessed_level`, `assessed_phrase`, `assessed_confidence`, `basis`, `contributor_ids`, and `disagreement`. Diffs use `op` `upsert` or `remove` for sites, cells, routes, and tracks. A cell includes its hex `boundary` and a route includes its `points`, both as latitude, longitude pairs.
- `tracks` on the snapshot are GPS fixes (`vehicle_id`, `lat`, `lon`, `timestamp`, `speed` in meters per second, `heading`, `origin_id`). They are bounded by `ANALYTICS_FIX_RETENTION_SECONDS` and `ANALYTICS_MAX_FIXES_PER_VEHICLE` (default 100, `0` keeps every fix that survived retention). `GET /tracks` and `GET /api/tracks` return the same array. A `retract` lists the removed tracks and drops any fix whose `origin_id` is that message id, including from the snapshot a later client receives.
- `GET /ogc` is an OGC API - Features landing page. Collections are sites, cells, routes, tracks, evidence, and itineraries. Item requests accept `bbox` and `datetime`.
- `GET /geojson`, `GET /heatmap`, and `GET /kml` export the current picture. `GET /kml/networklink` is a KML NetworkLink pointed at `/kml`.
- `GET /api/sites` and `GET /sites` return the same site fields: `status`, `assessment`, `confidence`, `types`, the deprecated `confidence_band`, and the reported versus assessed level fields. `GET /v1/events/correlations` includes those level fields on each group.
- `POST /api/routes` returns `{provider, routes}`.
- `GET /v1/settings/output-coordinates` returns the effective published coordinate format (`effective`, `override`, `coord_card`, `env`). `PUT` and `POST` set the analyst override, and `{"use_coord_card": true}` reverts it. `GET /health` includes the effective object. The WebSocket snapshot carries the same document.
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
