"""Incremental binary reading keeps incomplete UTF-8 lines until the next poll."""
import json
import hashlib
from pathlib import Path

from .analyzer import Analyzer


class RolloutReader:
    def __init__(self, path):
        self.path = Path(path)
        self.analyzer = Analyzer()
        self.offset = 0
        self.pending = b''
        self.line = 0
        self.identity = None
        self.fingerprint = hashlib.sha256()
        self.signature = None
        self.resets = 0

    def _reset(self):
        self.analyzer = Analyzer()
        self.offset = 0
        self.pending = b''
        self.line = 0
        self.fingerprint = hashlib.sha256()
        self.resets += 1

    def poll(self):
        with self.path.open('rb') as stream:
            import os
            stat = os.fstat(stream.fileno())
            identity = (stat.st_dev, stat.st_ino)
            signature = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            if identity == self.identity and signature == self.signature:
                return False
            reset = self.identity is not None and (identity != self.identity or stat.st_size < self.offset
                or (stat.st_size == self.offset and signature != self.signature))
            if not reset and self.offset:
                # Verify the consumed prefix without reparsing it. Tail samples alone
                # miss an earlier rewrite when an append occurs before the next poll.
                previous = hashlib.sha256()
                remaining = self.offset
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    previous.update(chunk)
                    remaining -= len(chunk)
                reset = bool(remaining) or previous.digest() != self.fingerprint.digest()
            if reset:
                self._reset()
            self.identity = identity
            stream.seek(self.offset)
            changed = reset
            while chunk := stream.read(1024 * 1024):
                self.offset += len(chunk)
                self.fingerprint.update(chunk)
                parts = (self.pending + chunk).split(b'\n')
                self.pending = parts.pop()
                for raw in parts:
                    self.line += 1
                    if not raw.strip():
                        continue
                    try:
                        event = json.loads(raw)
                    except (ValueError, UnicodeError, RecursionError):
                        self.analyzer.malformed_lines += 1
                        changed = True
                        continue
                    self.analyzer.feed(event, self.line)
                    changed = True
            self.signature = signature
            return changed
