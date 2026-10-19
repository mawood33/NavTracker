# NavTracker

NavTracker reads tracker positions from Navixy and publishes them to ChatSurfer. It is a single Python process. `NAVIXY_MODE` selects how positions are read.

## Ingestion mode

Set `NAVIXY_MODE` to `websocket` or `poll`.

| Value | Behavior |
| --- | --- |
| `websocket` | Subscribe to the Navixy websocket for `TRACKER_IDS`. If the socket keeps failing, poll the REST API, then switch back once the socket stays healthy. This is the default when `NAVIXY_MODE` and `navixy.mode` are both unset. |
| `poll` | Poll `tracker/get_state` on an interval. The websocket is not opened and failover is disabled. |

Matching is case-insensitive. Any other value stops the process at startup with `Invalid NAVIXY_MODE=... Expected one of: websocket, poll.`

The environment variable overrides `navixy.mode` in the YAML file for `APP_ENV`. Development, staging, and production all set `mode: websocket`, so failover to polling is on unless `NAVIXY_MODE=poll`. `TRACKER_SOURCE_MODE` and `navixy.source_mode` are no longer read; set `NAVIXY_MODE` instead.

`TRACKER_IDS` is the tracker scope for both modes (comma-separated). An empty list does not subscribe to every tracker and does not poll. The websocket subscription uses Navixy's `target.type: selected` with those ids, and any frame outside the set is dropped.

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

`MODE` is `pem`, `ssh`, or `fernet` (`crypto.mode` in YAML). `pem` and `ssh` encrypt with the public certificate at `ENCRYPTION_CERT`. `fernet` encrypts with `FERNET_KEY`, which is passed into `CryptoHandler`.

Fernet mode refuses to start when `FERNET_KEY` is missing, blank, or the YAML literal `None` (PyYAML loads that literal as the string `"None"`, which is unset). A value that is not a url-safe base64-encoded 32-byte key also fails at startup. The process does not generate a key.

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
python -m pytest
```
