from navtracker_common.errors import PublicationError


class SourceConnectionError(Exception):
    """Navixy socket failed to connect, subscribe, or stay open.

    ChatSurfer publish failures are not source failures and must not be
    raised as this error.
    """


class TrackerDiscoveryError(Exception):
    """The Navixy tracker list could not be read.

    This is not a socket failure and must not count toward websocket
    failover. Callers retry discovery. They must not subscribe to every
    tracker in its place.
    """


__all__ = [
    "PublicationError",
    "SourceConnectionError",
    "TrackerDiscoveryError",
]
