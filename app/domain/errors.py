class SourceConnectionError(Exception):
    """Navixy socket failed to connect, subscribe, or stay open.

    ChatSurfer publish failures are not source failures and must not be
    raised as this error.
    """
