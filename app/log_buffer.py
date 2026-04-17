"""
In-memory ring-buffer for recent log lines, displayed in the UI log viewer.
Also wires a logging.Handler into the root logger so all log output is captured.
"""

from __future__ import annotations

import logging
import os
from collections import deque
from datetime import datetime, timezone


class LogBuffer:
    """Thread-safe deque that stores the last N log lines."""

    def __init__(self, maxlen: int = 500) -> None:
        self._buf: deque[str] = deque(maxlen=maxlen)

    def append(self, line: str) -> None:
        ts  = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self._buf.append(f"[{ts}] {line}")

    def lines(self, n: int = 100) -> list[str]:
        """Return the last *n* lines, newest last."""
        items = list(self._buf)
        return items[-n:]

    def clear(self) -> None:
        self._buf.clear()


# Module-level singleton
log_buffer = LogBuffer(maxlen=int(os.getenv("LOG_BUFFER_LINES", "500")))


class _BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            log_buffer.append(self.format(record))
        except Exception:
            pass


def setup_logging(level: str = "INFO", log_dir: str = "data/logs") -> None:
    """
    Configure root logger:
      - StreamHandler → stdout
      - _BufferHandler → in-memory ring buffer (for UI)
      - RotatingFileHandler → data/logs/bot.log
    """
    from logging.handlers import RotatingFileHandler

    os.makedirs(log_dir, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # stdout
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)

    # rotating file
    fh = RotatingFileHandler(
        os.path.join(log_dir, "bot.log"),
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # in-memory buffer
    bh = _BufferHandler()
    bh.setFormatter(fmt)
    root.addHandler(bh)
