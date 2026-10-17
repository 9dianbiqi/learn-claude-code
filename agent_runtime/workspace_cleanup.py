"""Bind trusted workspace release to the existing repository effect ledger.

Only controller-side verification scopes install this context. Model tool schemas
cannot create or clear reservations through these functions.
"""
from contextlib import contextmanager
from contextvars import ContextVar

from .store import LeaseLost


_store = ContextVar("workspace_cleanup_store", default=None)


@contextmanager
def workspace_cleanup_authority(store):
    token = _store.set(store)
    try:
        yield
    finally:
        _store.reset(token)


@contextmanager
def workspace_cleanup_effect(resource):
    store = _store.get()
    if store is None:
        yield  # Standalone backend users have no Runtime repository authority.
        return
    reservation = store.begin_workspace_cleanup(resource)
    try:
        yield
    except BaseException:
        # A stale owner may annotate only its own journal, never external state.
        store.finish_effect_reservation(reservation, "unknown", {"resource": resource,
            "reason": "workspace release failed"}, allow_stale=True)
        raise
    else:
        if not store.finish_effect_reservation(reservation, "completed", {"resource": resource,
                "reason": "workspace release confirmed"}):
            raise LeaseLost("Workspace cleanup reservation changed before confirmation")
