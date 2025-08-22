# logging_config.py
from __future__ import annotations
import json, logging, os, sys, time
from typing import Any, Dict

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_JSON = os.getenv("LOG_JSON", "false").lower() == "true"

class RedactFilter(logging.Filter):
    SENSITIVE_KEYS = {"authorization", "api_key", "openai_api_key", "x-api-key"}
    def filter(self, record: logging.LogRecord) -> bool:
        msg = str(record.getMessage())
        for k in self.SENSITIVE_KEYS:
            if k in msg.lower():
                record.msg = msg.replace(record.msg, "[REDACTED]") if hasattr(record, "msg") else "[REDACTED]"
        return True

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k in ("request_id", "path", "method", "status", "duration_ms", "model", "provider"):
            if hasattr(record, k):
                payload[k] = getattr(record, k)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)

def setup_logging() -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RedactFilter())
    if LOG_JSON:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    root.addHandler(handler)
    root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
