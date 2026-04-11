"""Tests for nanoharness.history.InputHistory."""

from __future__ import annotations

import json
from pathlib import Path

from nanoharness.history import InputHistory


class TestEmptyHistory:
    def test_navigate_up_returns_none(self, tmp_path: Path) -> None:
        h = InputHistory(tmp_path / ".nanoharness" / "history")
        assert h.navigate_up("") is None

    def test_navigate_down_returns_none(self, tmp_path: Path) -> None:
        h = InputHistory(tmp_path / ".nanoharness" / "history")
        assert h.navigate_down() is None


class TestAddAndNavigate:
    def test_navigate_through_entries(self, tmp_path: Path) -> None:
        h = InputHistory(tmp_path / ".nanoharness" / "history")
        h.add("first")
        h.add("second")
        h.add("third")

        assert h.navigate_up("") == "third"
        assert h.navigate_up("") == "second"
        assert h.navigate_up("") == "first"
        assert h.navigate_up("") is None  # at oldest

        assert h.navigate_down() == "second"
        assert h.navigate_down() == "third"

    def test_navigate_down_past_newest_returns_draft(self, tmp_path: Path) -> None:
        h = InputHistory(tmp_path / ".nanoharness" / "history")
        h.add("one")
        h.navigate_up("")
        assert h.navigate_down() == ""  # draft was empty
        assert h.navigate_down() is None  # already at end


class TestDraftPreservation:
    def test_draft_stashed_and_restored(self, tmp_path: Path) -> None:
        h = InputHistory(tmp_path / ".nanoharness" / "history")
        h.add("old entry")

        # User has typed "work in progress" but hasn't submitted
        assert h.navigate_up("work in progress") == "old entry"
        # Navigate back down restores the draft
        assert h.navigate_down() == "work in progress"

    def test_empty_draft_restored_as_empty_string(self, tmp_path: Path) -> None:
        h = InputHistory(tmp_path / ".nanoharness" / "history")
        h.add("entry")
        assert h.navigate_up("") == "entry"
        result = h.navigate_down()
        assert result == ""


class TestConsecutiveDedup:
    def test_duplicate_not_stored(self, tmp_path: Path) -> None:
        h = InputHistory(tmp_path / ".nanoharness" / "history")
        h.add("same")
        h.add("same")
        h.add("same")
        assert len(h._entries) == 1

    def test_non_consecutive_duplicates_stored(self, tmp_path: Path) -> None:
        h = InputHistory(tmp_path / ".nanoharness" / "history")
        h.add("a")
        h.add("b")
        h.add("a")
        assert len(h._entries) == 3

    def test_dedup_resets_navigation(self, tmp_path: Path) -> None:
        h = InputHistory(tmp_path / ".nanoharness" / "history")
        h.add("x")
        h.navigate_up("")
        h.add("x")  # dedup, but should still reset nav
        assert h.navigate_up("") == "x"


class TestPersistence:
    def test_entries_survive_reload(self, tmp_path: Path) -> None:
        path = tmp_path / ".nanoharness" / "history"
        h1 = InputHistory(path)
        h1.add("alpha")
        h1.add("beta")

        h2 = InputHistory(path)
        assert h2.navigate_up("") == "beta"
        assert h2.navigate_up("") == "alpha"

    def test_multiline_input_persisted(self, tmp_path: Path) -> None:
        path = tmp_path / ".nanoharness" / "history"
        h1 = InputHistory(path)
        h1.add("line1\nline2\nline3")

        h2 = InputHistory(path)
        assert h2.navigate_up("") == "line1\nline2\nline3"

    def test_jsonl_format(self, tmp_path: Path) -> None:
        path = tmp_path / ".nanoharness" / "history"
        h = InputHistory(path)
        h.add("hello world")
        h.add("multi\nline")

        lines = path.read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0]) == "hello world"
        assert json.loads(lines[1]) == "multi\nline"


class TestMaxEntries:
    def test_trim_on_overflow(self, tmp_path: Path) -> None:
        path = tmp_path / ".nanoharness" / "history"
        h = InputHistory(path, max_entries=3)
        for i in range(5):
            h.add(str(i))

        assert len(h._entries) == 3
        assert h._entries == ["2", "3", "4"]

        # File is trimmed on next load, not during the session
        h2 = InputHistory(path, max_entries=3)
        assert h2._entries == ["2", "3", "4"]
        lines = path.read_text().strip().split("\n")
        assert len(lines) == 3

    def test_trim_on_load(self, tmp_path: Path) -> None:
        path = tmp_path / ".nanoharness" / "history"
        # Write 5 entries directly
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            for i in range(5):
                f.write(json.dumps(str(i)) + "\n")

        h = InputHistory(path, max_entries=3)
        assert len(h._entries) == 3
        assert h._entries == ["2", "3", "4"]


class TestCorruptFile:
    def test_bad_lines_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / ".nanoharness" / "history"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '"good"\n'
            "not json at all\n"
            "\n"
            '"also good"\n'
        )

        h = InputHistory(path)
        assert len(h._entries) == 2
        assert h._entries == ["good", "also good"]


class TestLazyFileCreation:
    def test_directory_created_on_first_add(self, tmp_path: Path) -> None:
        path = tmp_path / "deep" / "nested" / "history"
        assert not path.parent.exists()

        h = InputHistory(path)
        h.add("first")
        assert path.exists()
        assert json.loads(path.read_text().strip()) == "first"


class TestResetNavigation:
    def test_reset_moves_to_end(self, tmp_path: Path) -> None:
        h = InputHistory(tmp_path / ".nanoharness" / "history")
        h.add("a")
        h.add("b")
        h.add("c")

        h.navigate_up("")
        h.navigate_up("")
        h.reset_navigation()

        # Next up should start from newest
        assert h.navigate_up("") == "c"

    def test_reset_clears_draft(self, tmp_path: Path) -> None:
        h = InputHistory(tmp_path / ".nanoharness" / "history")
        h.add("entry")
        h.navigate_up("my draft")
        h.reset_navigation()

        # Navigate up and back down — draft should be empty now
        h.navigate_up("")
        assert h.navigate_down() == ""


class TestWhitespaceInput:
    def test_empty_string_not_added(self, tmp_path: Path) -> None:
        h = InputHistory(tmp_path / ".nanoharness" / "history")
        h.add("")
        h.add("   ")
        h.add("\n")
        assert len(h._entries) == 0
