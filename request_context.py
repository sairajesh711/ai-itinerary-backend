# request_context.py
from __future__ import annotations
import contextvars, uuid

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

def new_request_id() -> str:
    rid = uuid.uuid4().hex[:12]
    _request_id.set(rid)
    return rid

def get_request_id() -> str:
    return _request_id.get()
