"""Keep secrets out of logs."""
from __future__ import annotations

import logging
import re

_PATTERNS = [re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"), re.compile(r"partners\s+\S+", re.I)]


class RedactFilter(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 6]

    def scrub(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, "***")
        for p in _PATTERNS:
            text = p.sub("***", text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.scrub(record.getMessage())
        record.args = ()
        return True
