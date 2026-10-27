from ..models import GameInfo, Match


class ProviderError(Exception):
    """A source failed in a way the user can be told about in plain words (network down, no data, bad answer)."""


def describe_error(exc: BaseException) -> str:
    """Plain text for an error shown to the user (and used for every source the same way)."""
    if isinstance(exc, ProviderError):
        return str(exc)
    if isinstance(exc, TimeoutError) and not str(exc):
        return "did not answer in time"
    return f"{type(exc).__name__}: {exc}"


class Provider:
    """A source of game information. Implement search() and fetch().

    Both raise ProviderError on failure and never leak raw network or parsing exceptions.
    """
    name = "provider"

    def search(self, title: str) -> list:
        raise NotImplementedError

    def fetch(self, match: Match) -> GameInfo:
        raise NotImplementedError
