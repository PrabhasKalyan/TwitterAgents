"""Shared logger — writes to both stdout and the shared log file the API tails."""
import logging
import os
import sys
from collections import deque

LOG_FILE = os.environ.get("LOG_FILE", "/logs/outreach.log")
_TAIL: deque = deque(maxlen=20)


class TailHandler(logging.Handler):
    def emit(self, record):
        _TAIL.append(self.format(record))


def get_logger(name: str = "outreach") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    fh = logging.FileHandler(LOG_FILE)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    th = TailHandler()
    th.setFormatter(fmt)
    logger.addHandler(th)
    return logger


def get_tail() -> str:
    return "\n".join(_TAIL)
