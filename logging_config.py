# logging_config.py
from __future__ import annotations

import logging
import sys

class RequestIdFilter(logging.Filter):
    """Inject a default request_id if not provided in log 'extra'."""
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        return True

def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()

    # Always set root level to INFO (or the provided level)
    root.setLevel(level)

    # Ensure there is a stdout handler; reuse existing if present
    handler = None
    for h in root.handlers:
        if isinstance(h, logging.StreamHandler):
            handler = h
            break

    fmt = "%(asctime)s %(levelname)s %(name)s [%(process)d] [rid=%(request_id)s] %(message)s"

    if handler is None:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(fmt))
        handler.addFilter(RequestIdFilter())
        root.addHandler(handler)
    else:
        # Make sure formatting includes request_id and add the filter
        handler.setFormatter(logging.Formatter(fmt))
        has_filter = any(isinstance(f, RequestIdFilter) for f in getattr(handler, "filters", []))
        if not has_filter:
            handler.addFilter(RequestIdFilter())

    # Ensure our app loggers are INFO and propagate
    for name in ("app", "llm", "calendar"):
        lg = logging.getLogger(name)
        lg.setLevel(level)
        lg.propagate = True

    # Quiet some noisy libraries if you like (keep httpx requests though)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.INFO)
    logging.getLogger("openai").setLevel(logging.INFO)
