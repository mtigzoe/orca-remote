"""Legacy connection notices must wait for Orca's GLib main thread."""

import ast
from pathlib import Path
import sys
import types
from unittest import mock


def test_connection_notice_does_not_initialize_speech_on_transport_thread(monkeypatch):
    source = Path(__file__).resolve().parents[1] / "orca-customizations.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in {"_speak_message", "_speak_message_idle_cb"}]
    pending = []
    repository = types.ModuleType("gi.repository")
    repository.GLib = types.SimpleNamespace(
        idle_add=lambda cb, *args: pending.append((cb, args)),
    )
    orca = types.ModuleType("orca")
    orca.__path__ = []
    speech = types.ModuleType("orca.speech")
    speech.speak = mock.Mock()
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    monkeypatch.setitem(sys.modules, "orca", orca)
    monkeypatch.setitem(sys.modules, "orca.speech", speech)
    namespace = {}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)

    namespace["_speak_message"]("Connected")

    speech.speak.assert_not_called()
    assert len(pending) == 1
    callback, args = pending.pop()
    assert callback(*args) is False
    speech.speak.assert_called_once_with("Connected")
