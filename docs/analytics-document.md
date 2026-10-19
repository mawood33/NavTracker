# Analytics document (`navtracker.analytics` version 2)

Version 1 used `status` for the confidence band (`possible`, `probable`,
`confirmed`). Version 2 uses `status` for operational activity. Clients
that read version 1 `status` as a band must read `assessment` instead.
`confidence_band` still carries the version 1 value for this version and
will leave in a later version.

`confirmed` remains the stored band and is still accepted on input,
including `POST /api/sites/{id}/verify`. Exports write that band as
`verified`.

## Site fields

These fields are the same on `GET /api/sites`, `GET /sites`, the WebSocket
snapshot and diff items, OGC Features (`/ogc/collections/sites/items`), and
`GET /geojson`.

| Field | Version 1 | Version 2 |
| --- | --- | --- |
| `status` | `possible`, `probable`, `confirmed` | `unknown`, `active`, `inactive` (from activity) |
| `assessment` | absent | `possible`, `probable`, `verified` |
| `confidence` | number | number (unchanged) |
| `types` | site types | site types (unchanged) |
| `confidence_band` | absent | deprecated copy of version 1 `status` |

`GET /sites` and the snapshot also include evidence, origin, and location
detail. `GET /api/sites` is the shorter list and adds nothing that
contradicts those fields.

## Tracks

The snapshot field is `tracks`. Each fix is:

| Field | Meaning |
| --- | --- |
| `vehicle_id` | Vehicle identifier |
| `lat`, `lon` | Decimal degrees |
| `timestamp` | ISO-8601 |
| `speed` | Meters per second, or null |
| `heading` | Degrees, or null |
| `origin_id` | Chat message id when the fix came from chat |

`GET /tracks` and `GET /api/tracks` return `{"tracks": [...]}`. OGC
collection `tracks` is a FeatureCollection of points (GeoJSON coordinates
are longitude, latitude). All three require the read token when one is
configured. The write token also grants read.

`ANALYTICS_FIX_RETENTION_SECONDS` (default 86400, `0` disables the window)
drops fixes older than that many seconds before each vehicle's newest fix.
`ANALYTICS_MAX_FIXES_PER_VEHICLE` (default 100, `0` disables the cap) then
keeps the newest remaining fixes for that vehicle.

## Diffs and retract

A diff is `type: update` with `changes`. Each change has `op` `upsert` or
`remove` and `kind` `site`, `cell`, `route`, or `track`. `upsert` includes
`item`. `remove` names the key (`name`, `cell_id`, or `vehicle_id` plus
`timestamp`). Removing an id the client does not have is a no-op.

Cells include `boundary` and routes include `points`, each a list of
`[latitude, longitude]`. A dropped cell or route is a `remove`, not an
omission the client has to guess.

`type: retract` carries `message_id` and `tracks` (the fixes just removed).
Drop every track whose `origin_id` equals `message_id`, and every track
listed in `tracks`. The stored snapshot used for a new WebSocket client
drops those fixes immediately. The log line is the id only.

## Push adapter

`analytics/infrastructure/frontend.py` POSTs this same document to
`ANALYTICS_FRONTEND_INGEST_URL`. It does not reshape sites or tracks.
