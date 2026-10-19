class PublicationError(Exception):
    """A chat message could not be published.

    This is independent of any position source. Callers must keep reading
    and must not treat it as a socket failure.
    """
