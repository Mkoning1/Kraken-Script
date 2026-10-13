"""Durable local state writing: replace atomically, fsync file and parent directory.

This protects against partial JSON files after process termination. It does
not make multiple simultaneous writers safe; the production runner holds a lock.
"""
import json
import os
import tempfile
from pathlib import Path


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".state-",
            suffix=".tmp", delete=False,
        ) as handle:
            tmp = handle.name
            json.dump(data, handle, separators=(",", ":"), ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        tmp = None
        if os.name == "posix":
            fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
