"""Per-run content-cache policy, isolated from concurrent analysis tasks."""

from contextlib import contextmanager
from contextvars import ContextVar


_REFRESH = ContextVar("content_cache_refresh", default=False)
_CANCEL = ContextVar("content_cache_cancel", default=None)


def refreshing() -> bool:
    return _REFRESH.get()


def refresh_cancelled() -> bool:
    cancel = _CANCEL.get()
    return refreshing() and bool(cancel and cancel())


@contextmanager
def content_cache_policy(refresh: bool = False, should_cancel=None):
    """Bypass reads for this run; retain old unit results on cancellation."""
    refresh_token = _REFRESH.set(refresh)
    cancel_token = _CANCEL.set(should_cancel)
    try:
        yield
    finally:
        _CANCEL.reset(cancel_token)
        _REFRESH.reset(refresh_token)
