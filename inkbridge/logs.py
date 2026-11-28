"""Logging for every process: one JSON object per line on stderr, shaped for Loki.

    {"ts": "2026-11-26T09:14:03.120Z", "level": "info", "logger": "inkbridge.poller",
     "msg": "poll finished", "trigger": "wake", "pulled": 0, "blob_fetches": 7}

`msg` is a constant per event; whatever varies goes into its own flat snake_case field, passed as
`extra={...}`. A traceback goes into `stack` (with `error` and `error_type`), so an event stays one
line. LOG_FORMAT=text prints plain lines for a terminal instead; LOG_LEVEL sets the level.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from datetime import datetime, timezone

LEVELS = {"DEBUG": "debug", "INFO": "info", "WARNING": "warn", "ERROR": "error", "CRITICAL": "error"}

# Attributes every LogRecord has; anything else on a record came from `extra`.
_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime", "taskName", "color_message"}  # uvicorn: ANSI copy of msg


def _uvicorn_access(record: logging.LogRecord) -> dict | None:
    """uvicorn's access log passes (client, method, path, http version, status) as format args."""
    if record.name == "uvicorn.access" and isinstance(record.args, tuple) and len(record.args) == 5:
        client, method, path, http_version, status = record.args
        return {"msg": "http request", "client": client, "method": method, "path": path,
                "http_version": http_version, "status": status}
    return None


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds")
        out: dict = {
            "ts": ts.replace("+00:00", "Z"),
            "level": LEVELS.get(record.levelname, record.levelname.lower()),
            "logger": record.name,
        }
        # Our own events have no args, so this is the constant message; a library's is rendered.
        out.update(_uvicorn_access(record) or {"msg": record.getMessage()})
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith("_"):
                out[key] = value
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            out.setdefault("error", str(exc))
            out["error_type"] = type(exc).__name__
            out["stack"] = "".join(traceback.format_exception(*record.exc_info)).rstrip()
        if record.stack_info:
            out["stack"] = (out.get("stack", "") + "\n" + record.stack_info).strip()
        return json.dumps(out, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Plain lines for a terminal: the message, then the fields as key=value."""

    def format(self, record: logging.LogRecord) -> str:
        fields = " ".join(f"{k}={v!r}" if isinstance(v, str) and " " in v else f"{k}={v}"
                          for k, v in vars(record).items() if k not in _RESERVED and not k.startswith("_"))
        line = f"{self.formatTime(record)} {LEVELS.get(record.levelname, record.levelname.lower())} " \
               f"{record.name} {record.getMessage()}" + (f" {fields}" if fields else "")
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def setup(level: int | str | None = None) -> None:
    """Send every logger's records (uvicorn's, httpx's, ours) to stderr in one format."""
    level = level or os.environ.get("LOG_LEVEL", "").strip().upper() or logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    text = os.environ.get("LOG_FORMAT", "").strip().lower() == "text"
    handler.setFormatter(TextFormatter() if text else JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # uvicorn installs its own handlers unless told otherwise (log_config=None); make sure its
    # loggers propagate to ours either way.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
