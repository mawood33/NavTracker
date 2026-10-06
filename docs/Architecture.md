# NavTracker Architecture

## 1. Overview

NavTracker is a containerized Python service that:

1. Retrieves tracker GPS data from Navixy.
2. Supports two data-ingestion modes:
   - Periodic REST API polling.
   - WebSocket event streaming.
3. Converts provider-specific payloads into domain-level tracker position objects.
4. Publishes tracker data to ChatSurfer.
5. Applies request-rate limiting to ChatSurfer calls.
6. Optionally encrypts messages before publication.
7. Selects the ChatSurfer room based on encryption configuration:
   - `freedom_flag`
   - `freedom_flag_encrypted`
8. Supports separate development, staging, and production configurations.
9. Runs inside a Docker container.

The design follows domain-driven architecture and separates business logic from external systems, configuration, transport, and cryptography.

---

## 2. Goals

The application is designed to provide:

- Clear separation between domain logic and infrastructure.
- Replaceable Navixy data sources.
- Replaceable ChatSurfer publishing implementation.
- Safe handling of API credentials and encryption keys.
- Shared rate limiting across outbound ChatSurfer requests.
- Environment-specific configuration.
- Automatic WebSocket reconnection.
- Testable application services.
- Docker-based deployment.
- Small, structured GPS messages suitable for ChatSurfer rooms.

---

## 3. Non-goals

The initial implementation does not provide:

- Persistent local storage of tracker positions.
- Historical route analysis.
- A web dashboard.
- Multi-tenant user management.
- Automatic tracker discovery.
- Message replay after process failure.
- Guaranteed delivery across service restarts.

If guaranteed delivery is required, a durable queue or database should be added.

---

## 4. High-level architecture

```text
                         +----------------------+
                         |      Navixy          |
                         |----------------------|
                         | REST API             |
                         | WebSocket events     |
                         +----------+-----------+
                                    |
                                    v
                     +--------------+---------------+
                     |     Infrastructure Layer      |
                     |-------------------------------|
                     | NavixyApiClient              |
                     | NavixyWebSocketClient        |
                     +--------------+---------------+
                                    |
                                    v
                     +--------------+---------------+
                     |       Application Layer       |
                     |-------------------------------|
                     | TrackerPollingService        |
                     | TrackerWebSocketService      |
                     +--------------+---------------+
                                    |
                                    v
                     +--------------+---------------+
                     |          Domain Layer         |
                     |-------------------------------|
                     | TrackerPosition              |
                     | TrackerDataSource port        |
                     | MessagePublisher port        |
                     +--------------+---------------+
                                    |
                                    v
                     +--------------+---------------+
                     |     ChatSurfer Adapter        |
                     |-------------------------------|
                     | RateLimiter                  |
                     | CryptoHandler                |
                     | ChatSurferApiClient          |
                     +--------------+---------------+
                                    |
                                    v
                         +----------------------+
                         |     ChatSurfer       |
                         |----------------------|
                         | freedom\_flag         |
                         | freedom\_flag_        |
                         | encrypted            |
                         +----------------------+
