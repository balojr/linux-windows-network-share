"""lan-share: Windows <-> Linux file sharing orchestration."""

__version__ = "0.2.0"


class LanShareError(Exception):
    """A user-facing error; the CLI prints it without a traceback."""
