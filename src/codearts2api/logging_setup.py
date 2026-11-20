"""Structured logging that never writes AK/SK/tokens."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Iterable

class SecretRedactFilter(logging.Filter):
    """Redact configured secret values and obvious credential key=value pairs."""

    def __init__(self, secrets: Iterable[str] | None = None) -> None:
        super().__init__()
        self._secrets = tuple(s for s in (secrets or []) if s and len(s) >= 4)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redact(str(record.msg))
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._redact_value(v) for k, v in record.args.items()}
            else:
                record.args = tuple(self._redact_value(a) for a in record.args)
        return True

    def _redact_value(self, value: object) -> object:
        if isinstance(value, str):
            return self._redact(value)
        return value

    def _redact(self, text: str) -> str:
        redacted = text
        for secret in self._secrets:
            redacted = redacted.replace(secret, "***")
        # key=value / key: value patterns for known credential names
        redacted = re.sub(
            r"(?i)(authorization|x-security-token|security[_-]?token|"
            r"access[_-]?key|secret[_-]?key|api[_-]?key)\s*([=:])\s*([^\s,;]+)",
            r"***",
            redacted,
        )
        return redacted


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO", secrets: Iterable[str] | None = None) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(SecretRedactFilter(secrets))
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # Quiet noisy third-party loggers a bit
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.INFO)
