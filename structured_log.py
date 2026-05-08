"""Structured (JSON) logging + correlation_id propagation.

Activated when ``LOG_FORMAT=json`` in the environment. Otherwise the existing
human-readable format remains. Logs go to stdout (Docker captures them) and to
``logs/assistant.log`` with size-based rotation (max 100 MB per file, 7 backups).
"""
from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import os
from pathlib import Path

CORRELATION_ID: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")

LOG_DIR = Path(__file__).parent / "logs"
LOG_FILE = LOG_DIR / "assistant.log"
MAX_BYTES = 100 * 1024 * 1024
BACKUP_COUNT = 7


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        payload = {
            "timestamp":      self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level":          record.levelname,
            "service":        record.name,
            "message":        record.getMessage(),
            "correlation_id": CORRELATION_ID.get(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        for k in ("user_id", "chat_id", "request_path"):
            v = getattr(record, k, None)
            if v is not None:
                payload[k] = v
        return json.dumps(payload, ensure_ascii=False, default=str)


class CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = CORRELATION_ID.get()
        return True


def configure_logging() -> None:
    """Idempotent — safe to call multiple times."""
    fmt = os.getenv("LOG_FORMAT", "text").lower()
    LOG_DIR.mkdir(exist_ok=True)
    root = logging.getLogger()
    # Strip prior handlers we own (keeps re-config clean under uvicorn reload).
    for h in list(root.handlers):
        if getattr(h, "_assistant_owned", False):
            root.removeHandler(h)

    text_fmt = "%(asctime)s %(levelname)s %(name)s [cid=%(correlation_id)s]: %(message)s"
    formatter: logging.Formatter = JsonFormatter() if fmt == "json" else logging.Formatter(text_fmt)
    cid_filter = CorrelationFilter()

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    stream.addFilter(cid_filter)
    stream._assistant_owned = True  # type: ignore[attr-defined]
    root.addHandler(stream)

    rotating = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8",
    )
    rotating.setFormatter(formatter)
    rotating.addFilter(cid_filter)
    rotating._assistant_owned = True  # type: ignore[attr-defined]
    root.addHandler(rotating)

    root.setLevel(logging.INFO)
