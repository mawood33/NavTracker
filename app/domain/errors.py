class SourceConnectionError(Exception):
    """Navixy socket failed to connect, subscribe, or stay open.

    ChatSurfer publish failures are not source failures and must not be
    raised as this error.
    """


class PublicationError(Exception):
    """A tracker fix could not be published.

    This is independent of the Navixy socket. Callers must keep reading
    or polling and must not count it toward websocket failover.
    """
