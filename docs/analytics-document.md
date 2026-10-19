# Analytics document (`navtracker.analytics` version 2)

Version 1 used `status` for the confidence band (`possible`, `probable`,
`confirmed`). Version 2 uses `status` for operational activity. Clients
that read version 1 `status` as a band must read `assessment` instead.
`confidence_band` still carries the version 1 value for this version and
will leave in a later version.

Schema 2 is unreleased, so `assessment` stays on this version.
`verified` is accepted on input, including a site status and
`POST /api/sites/{id}/verify`, and is written as `confirmed`.

## Site fields

These fields are the same on `GET /api/sites`, `GET /sites`, the WebSocket
snapshot and diff items, OGC Features (`/ogc/collections/sites/items`), and
`GET /geojson`.

| Field | Version 1 | Version 2 |
| --- | --- | --- |
| `status` | `possible`, `probable`, `confirmed` | `unknown`, `active`, `inactive` (from activity) |
| `assessment` | absent | `possible`, `probable`, `confirmed` |
| `confidence` | number | number (unchanged) |
| `types` | site types | site types (unchanged) |
| `confidence_band` | absent | deprecated copy of version 1 `status` (`possible`, `probable`, `confirmed`) |

`assessment` and `confidence_band` are the same three words.
`confidence_band` is the deprecated bridge for a version 1 client.

## Reported level and assessed level

A chat line or TACREP can state its own level (`POSS LOW`, `POSS HIGH`,
`PROB` / `PROBABLE`, `CONFIRMED`). `CERTAIN` and `VERIFIED` are synonyms
of `CONFIRMED`. NavTracker keeps that stated level and also records the
level it assessed after correlation.

These fields are on each site (`GET /api/sites`, `GET /sites`, the
WebSocket snapshot and site diff item), each correlation
(`GET /v1/events/correlations`), and each evidence row in the snapshot
and on `GET /sites`:

| Field | Meaning |
| --- | --- |
| `reported_level` | `possible`, `probable`, or `confirmed` parsed from that item's own text, or empty |
| `reported_phrase` | Operator phrase from that text (`POSS LOW`, `POSS HIGH`, `PROB`, `CONFIRMED`), or empty |
| `assessed_level` | `possible`, `probable`, or `confirmed` after correlation. On a site this matches `assessment` |
| `assessed_phrase` | Operator phrase for the assessed level |
| `assessed_confidence` | Numeric weight of the assessed level |
| `basis` | `single_source` or `correlated` |
| `contributor_ids` | Serials that support the assessment. A correction such as `I-103A` is the id that remains |
| `disagreement` | True when more than one report contributes, one of them stated `confirmed`, and the assessed level is not `confirmed` |

`basis` is `single_source` when the assessed phrase is the report's own
stated phrase, including a lone `CONFIRMED`. It is `correlated` when
other reports raised the level, or when a stated `CONFIRMED` is kept
beside a lower assessed level. The reported level is not replaced.

The phrase table is `confidence_terms` on the coord card. Defaults:
POSS LOW 0.25, POSS HIGH 0.45, PROB 0.70, CONFIRMED 1.0. A numeric
confidence maps the same way: at or above `confirmed_at` is CONFIRMED,
at or above `probable_at` is PROB, at or above the POSS HIGH weight is
POSS HIGH, and anything lower is POSS LOW. A card that lists its own
terms replaces those phrases everywhere a remark is written.

Room routing uses the assessed phrase and the CAT. A higher assessed
level is published again. The new remark names the previous phrase and
the new one.

## Published ChatSurfer remarks

The original TACREP, SITREP, or FUSED line is kept. The remark is
appended to the last `//` field, or after a comma when the line has no
`//`. The remark itself never contains `//`.

Wording comes from `assessment_text` on the coord card. The default
templates are:

```yaml
assessment_text:
  single: "SINGLE SOURCE {phrase}"
  correlated: "ASSESSED {phrase} (FROM {reported} {ids})"
  disagreement: "REPORTED {reported} ASSESSED {phrase} (FROM {ids})"
  republish: "WAS {previous} NOW {text}"
```

Placeholders are `phrase`, `reported`, `ids`, `count`, `previous`, and
`text`. `ids` is the contributor serials joined with ` + `.

Examples, using the default templates:

```text
HAWK // G-501 // POSS LOW TEL // N6919 W11919 // TAI // 1919Z // CAT IV SINGLE SOURCE POSS LOW
```

```text
HAWK // G-501 // POSS LOW TEL // N6919 W11919 // TAI // 1919Z // CAT IV ASSESSED PROB (FROM POSS LOW G-501 + J-401 + I-103A)
```

```text
HAWK // G-501 // CONFIRMED TEL // N6919 W11919 // TAI // 1919Z // CAT IV REPORTED CONFIRMED ASSESSED PROB (FROM G-501 + J-401)
```

```text
HAWK // G-501 // POSS LOW TEL // N6919 W11919 // TAI // 1919Z // CAT IV WAS POSS LOW NOW ASSESSED PROB (FROM POSS LOW G-501 + J-401 + I-103A)
```

A SITREP with no `//` gains the same remark after a comma.

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
