"""Persistent per-workspace input history for the TUI."""

from __future__ import annotations

import json
from pathlib import Path


class InputHistory:
    """Shell-like input history backed by a JSONL file.

    Each line in the file is a JSON-encoded string.  Multi-line inputs are
    stored as a single JSON string with embedded ``\\n``.
    """

    def __init__(self, path: Path, max_entries: int = 1000) -> None:
        self._path = path
        self._max_entries = max_entries
        self._entries: list[str] = []
        self._index: int = 0  # points past end = "new input" position
        self._draft: str = ""
        self._dir_ensured: bool = False
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        self._entries = []
        try:
            with self._path.open() as f:
                for line in f:
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    try:
                        self._entries.append(json.loads(line))
                    except (json.JSONDecodeError, TypeError):
                        continue  # skip corrupt lines
        except (FileNotFoundError, OSError):
            pass
        # Trim on load: cap the in-memory list and rewrite the file.
        if len(self._entries) > self._max_entries:
            self._entries = self._entries[-self._max_entries :]
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._dir_ensured = True
                with self._path.open("w") as f:
                    for entry in self._entries:
                        f.write(json.dumps(entry) + "\n")
            except OSError:
                pass
        self._index = len(self._entries)

    def _save_entry(self, text: str) -> None:
        if not self._dir_ensured:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._dir_ensured = True
        with self._path.open("a") as f:
            f.write(json.dumps(text) + "\n")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(self, text: str) -> None:
        """Append *text* to history (consecutive duplicates are skipped)."""
        if not text or not text.strip():
            return
        # Consecutive deduplication.
        if self._entries and self._entries[-1] == text:
            self._index = len(self._entries)
            self._draft = ""
            return
        self._entries.append(text)
        self._save_entry(text)
        if len(self._entries) > self._max_entries:
            self._entries = self._entries[-self._max_entries :]
        self._index = len(self._entries)
        self._draft = ""

    def navigate_up(self, current_text: str) -> str | None:
        """Move one step back in history.  Returns the entry, or ``None``."""
        if self._index == 0:
            return None
        if self._index == len(self._entries):
            self._draft = current_text
        self._index -= 1
        return self._entries[self._index]

    def navigate_down(self) -> str | None:
        """Move one step forward in history.  Returns the entry, or ``None``."""
        if self._index >= len(self._entries):
            return None
        self._index += 1
        if self._index == len(self._entries):
            return self._draft
        return self._entries[self._index]

    def reset_navigation(self) -> None:
        """Reset navigation pointer to the end (called after submission)."""
        self._index = len(self._entries)
        self._draft = ""
