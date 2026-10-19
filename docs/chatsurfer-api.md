# ChatSurfer REST API 4.24.0

Summary of the UNCLASSIFIED ChatSurfer REST contract this repository
implements. It is not a copy of the vendor document. Base URLs:

| Network | REST | WebSocket |
| --- | --- | --- |
| UNCLASS | `https://chatsurfer.nro.mil` | `wss://chatsurfer.nro.mil/ws/connect` |
| SIPR | `https://chatsurfer.proj.nro.smil.mil` | `wss://chatsurfer.proj.nro.smil.mil/ws/connect` |
| JWICS | `https://chatsurfer.proj.nro.ic.gov` | `wss://chatsurfer.proj.nro.ic.gov/ws/connect` |

SockJS is the same path over HTTPS. This service uses the native websocket.

## Authentication

Every request sends the `Api-Key` header and the NPE PKI client certificate.
UNCLASS and SIPR use a DoD NPE certificate. JWICS uses an IC NPE certificate.
BICES, CX-J, and CX-K are not supported.

## Rate limit

60 requests per minute per user, token bucket. A 429 carries
`X-RateLimit-UserLimit`, `X-RateLimit-UserRemaining`, and
`X-RateLimit-UserReset` (seconds until the remaining count refills). Those
headers are on every response. Reads, publishes, searches, domain checks,
and scrub polls share one limiter. A 429 defers the next call until
`UserReset`. `/health` reports `ingest.chat.rate_remaining`.

## Read

`GET /api/chat/messages/{domainId}/{roomName}`

`roomName` is URL-encoded and may start with `#`. The body is
`{classification, messages:[ChatMessage]}`. **404 means the room does not
exist or has no messages.** That is an empty room (`empty_or_missing` on
`/health`), logged at debug, not an error.

There is no documented `limit` or `since` query on this route. After a
message id is known, the next page is:

`GET /api/chat/messages/{messageId}?numAfter=N&numBefore=0`

`numAfter` and `numBefore` are 0..1000. A 404 on that route means the id
was not found; the poll treats it as an empty page.

`CHAT_SURFER_READ_PATH` overrides the room route. `{domainId}` and
`{roomName}` are substituted when present.

## Search and backfill

`POST /api/chatsearch/messages/search`

```json
{
  "roomFilter": {"<domainId>": {"properties": ["<roomName>"]}},
  "timeFilter": {"startDateTime": "...", "endDateTime": "..."},
  "limit": 100,
  "cursor": "<previous nextCursorMark>",
  "includePrivateRooms": true,
  "requestGeoTags": true
}
```

`roomFilter` maps a domain id to a DomainFilterDetail. An empty
`properties` array means every room in that domain. `limit` is 1..1000
(default 100). Pass `nextCursorMark` back as `cursor`. Stop when it
repeats or is `*`. Reuse the returned `searchTimeFilter` on the next page.
`CHAT_SURFER_BACKFILL_MINUTES` is the startup lookback. `0` skips search.
The same call is the REST failover path when the websocket is down.

## ChatMessage

`classification`, `domainId`, `id`, `roomName`, `sender` (not `nickname`),
`text` (not `message`), `timestamp` (ISO, time received), `tags`,
`threadId`, `userId`, `private`, `isEdited`, `revisionId`,
`revisionTimestamp`, `mentions`, `geoTags`, `sconumTags`, `imageTags`.

`geoTags[].location` is a GeoJSON Point whose coordinates are
`[longitude, latitude]`. A missing TACREP or SITREP position uses the first
point. A point that disagrees with the parsed position beyond
`mismatch_meters` adds a warning. The message classification is kept.

`revisionId` equals `id` until the message is edited. `isEdited` with a new
`revisionId` replaces the evidence from the earlier revision.

### Tags

| Tag | Behavior |
| --- | --- |
| `EXERCISE` | Exercise data. Stored as an attribute. `CHAT_SURFER_EXERCISE_ONLY` drops everything else. |
| `SENT_BY_SYSTEM` | Always skipped. |
| `SENT_BY_BOT` | Our `CHAT_SURFER_NICKNAME` is always skipped. `CHAT_SURFER_SKIP_BOTS` names other nicknames to skip. |
| `MARKDOWN_FORMATTED` | Markdown markers are stripped before parsing. |
| anything else | Ignored. |

## WebSocket

STOMP 1.2. Send a heart-beat at least every 15 seconds and accept server
heart-beats. A heart-beat is a newline. Topics:

- `/topic/chat-messages-room/{domainId}/{roomName}`
- `/topic/chat-messages-domain/{domainId}`
- `/topic/chat-messages-geotagged-room/{domainId}/{roomName}`
- `/topic/chat-messages-scrubbed`

`CHAT_SURFER_INGEST_MODE` is `websocket` (default) or `poll`. Websocket
failures back off, then REST polling runs while a probe tries the socket
again. `poll` never opens the socket. The API key and client certificate
are on the websocket handshake.

## Data spill

Subscribe to `/topic/chat-messages-scrubbed` and poll
`GET /api/dataspill/messages/scrubbed?page=1&size=100` (`page` 1..1000,
`size` 1..100). The body is `{scrubbedMessages:[{messageId, scrubDate}]}`.

On a scrub the service deletes that message's text and the fixes, TACREPs,
reports, taskings, and correlations derived from it, recomputes site
confidence, and broadcasts `{type: "retract", message_id}`. OGC and
WebSocket snapshots are rebuilt without it. Chat text is not stored in
files. The log line is the id only.

## Publish

`POST /api/chatserver/message` with `{classification, domainId, message,
nickname, roomName}` returns **204** and an empty body. `userHighClassification`
is not in the documented schema and is sent only when configured. The API
user must be a member of a private room to send to it.

## Domains and rooms

`GET /api/chatserver/domains` returns `{domains:[{id, name, ...}]}`. Startup
warns if the configured domain id is absent. It does not stop the process.

`POST /api/roomsearch/rooms/search` can check room names. A miss or a
rejected body is a warning only. The request used here is keyword search on
`DISPLAY_NAME` with `pageNumber` 0.

## Errors

Error bodies are `{classification, code, message, fieldErrors[]}`. Logs
record the status, `code`, and `message`. They do not record the API key,
the certificate, or `fieldErrors`.
