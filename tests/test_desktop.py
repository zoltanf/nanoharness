from __future__ import annotations

import sys
from types import SimpleNamespace
from pathlib import Path

from nanoharness.desktop import _JsApi


class _FakeWindow:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def create_file_dialog(self, dialog_type, directory: str):
        self.calls.append((dialog_type, directory))
        return self.result


def test_pick_folder_prefers_new_filedialog_enum(monkeypatch):
    fake_window = _FakeWindow(["/tmp/chosen"])
    fake_webview = SimpleNamespace(
        FileDialog=SimpleNamespace(FOLDER="folder-enum"),
        FOLDER_DIALOG="deprecated-folder-enum",
    )
    monkeypatch.setitem(sys.modules, "webview", fake_webview)

    api = _JsApi()
    api._window = fake_window

    result = api.pick_folder()

    assert result == "/tmp/chosen"
    assert fake_window.calls == [("folder-enum", str(Path.home()))]


def test_pick_folder_falls_back_for_older_pywebview(monkeypatch):
    fake_window = _FakeWindow(["/tmp/chosen"])
    fake_webview = SimpleNamespace(FOLDER_DIALOG="deprecated-folder-enum")
    monkeypatch.setitem(sys.modules, "webview", fake_webview)

    api = _JsApi()
    api._window = fake_window

    result = api.pick_folder()

    assert result == "/tmp/chosen"
    assert fake_window.calls == [("deprecated-folder-enum", str(Path.home()))]
