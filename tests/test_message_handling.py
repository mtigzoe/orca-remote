"""Tests for RemoteExtension inbound message dispatch."""

from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from tests.test_bypass_chords import _load_remote_module


def _new_message_instance(mod):
    instance = mod.RemoteExtension.__new__(mod.RemoteExtension)
    instance._say_async = mock.Mock()
    instance._log = mock.Mock()
    instance._current_role = mock.Mock(return_value="client")
    instance._focus_on_remote = False
    instance._inbound_speech_muted = False
    instance._announced_join = True
    instance._peer_not_connected_announced = False
    instance._dropped_nonstring_items = 0
    instance._handle_inbound_key = mock.AsyncMock()
    return instance


@pytest.mark.parametrize("msg_type", ["cancel", "pause_speech"])
@pytest.mark.parametrize("focus,muted", [(False, False), (True, True), (True, False)])
def test_remote_interrupt_obeys_the_speech_gate(monkeypatch, msg_type, focus, muted):
    mod = _load_remote_module()
    instance = _new_message_instance(mod)
    instance._focus_on_remote = focus
    instance._inbound_speech_muted = muted
    idle = mock.Mock()
    monkeypatch.setattr(mod.GLib, "idle_add", idle, raising=False)

    asyncio.run(instance._on_message({"type": msg_type}))

    assert idle.call_count == int(focus and not muted)


class TestConnectionMessages:
    def test_client_joined_is_announced(self):
        mod = _load_remote_module()
        instance = _new_message_instance(mod)

        asyncio.run(instance._on_message({"type": mod.protocol.MSG_CLIENT_JOINED}))

        instance._say_async.assert_called_once_with("Orca Remote: peer joined.")
        instance._log.assert_not_called()

    def test_nvda_not_connected_is_announced(self):
        mod = _load_remote_module()
        instance = _new_message_instance(mod)

        asyncio.run(
            instance._on_message({"type": mod.protocol.MSG_NVDA_NOT_CONNECTED})
        )

        instance._say_async.assert_called_once_with(
            "Orca Remote: peer is not connected."
        )
        instance._log.assert_not_called()

    def test_nvda_not_connected_repeats_are_suppressed(self):
        mod = _load_remote_module()
        instance = _new_message_instance(mod)
        message = {"type": mod.protocol.MSG_NVDA_NOT_CONNECTED}

        asyncio.run(instance._on_message(message))
        asyncio.run(instance._on_message(message))

        instance._say_async.assert_called_once_with(
            "Orca Remote: peer is not connected."
        )

    def test_client_joined_resets_not_connected_gate(self):
        mod = _load_remote_module()
        instance = _new_message_instance(mod)

        asyncio.run(
            instance._on_message({"type": mod.protocol.MSG_NVDA_NOT_CONNECTED})
        )
        asyncio.run(instance._on_message({"type": mod.protocol.MSG_CLIENT_JOINED}))
        asyncio.run(
            instance._on_message({"type": mod.protocol.MSG_NVDA_NOT_CONNECTED})
        )

        assert instance._say_async.call_args_list == [
            mock.call("Orca Remote: peer is not connected."),
            mock.call("Orca Remote: peer joined."),
            mock.call("Orca Remote: peer is not connected."),
        ]


class TestToneMessages:
    """Inbound `tone` frames: NVDA's beeps must be played locally.

    Regression target for the parity gap where the modern dispatch
    had no tone branch and dropped every beep as "unhandled", while
    the legacy implementation played them via msg_tone.
    """

    def _tone_instance(self, mod, focus=True, muted=False, role="client"):
        instance = _new_message_instance(mod)
        instance._current_role = mock.Mock(return_value=role)
        instance._focus_on_remote = focus
        instance._inbound_speech_muted = muted
        instance._play_tone = mock.Mock()
        return instance

    def test_tone_is_played_when_focused_on_remote(self):
        mod = _load_remote_module()
        instance = self._tone_instance(mod)
        message = {"type": "tone", "hz": 880, "length": 100, "left": 50, "right": 50}

        asyncio.run(instance._on_message(message))

        instance._play_tone.assert_called_once_with(message)

    @pytest.mark.parametrize(
        "focus,muted,role",
        [(False, False, "client"), (True, True, "client"), (True, False, "host")],
    )
    def test_tone_obeys_the_speech_gate(self, focus, muted, role):
        mod = _load_remote_module()
        instance = self._tone_instance(mod, focus=focus, muted=muted, role=role)

        asyncio.run(instance._on_message({"type": "tone", "hz": 880, "length": 100}))

        instance._play_tone.assert_not_called()

    def test_play_tone_parses_and_clamps(self, monkeypatch):
        mod = _load_remote_module()
        instance = _new_message_instance(mod)
        spawned = {}

        class FakeThread:
            def __init__(self, target=None, args=(), daemon=None):
                spawned["target"] = target
                spawned["args"] = args

            def start(self):
                pass

        monkeypatch.setattr(mod.threading, "Thread", FakeThread)

        instance._play_tone(
            {"type": "tone", "hz": 440, "length": 250, "left": 150, "right": -5}
        )

        assert spawned["target"] is mod._play_tone_blocking
        assert spawned["args"][:4] == (440, 250, 100, 0)

    @pytest.mark.parametrize(
        "message",
        [
            {"type": "tone"},
            {"type": "tone", "hz": 0, "length": 100},
            {"type": "tone", "hz": 440, "length": 0},
            {"type": "tone", "hz": "loud", "length": 100},
            {"type": "tone", "hz": [440], "length": 100},
        ],
    )
    def test_play_tone_drops_malformed_frames(self, monkeypatch, message):
        mod = _load_remote_module()
        instance = _new_message_instance(mod)
        threads = []
        monkeypatch.setattr(
            mod.threading, "Thread", lambda **kw: threads.append(kw)
        )

        instance._play_tone(message)

        assert threads == []


class TestTonePlaybackBackends:
    def test_sox_play_is_preferred(self, monkeypatch):
        mod = _load_remote_module()
        calls = []
        monkeypatch.setattr(
            mod.shutil, "which", lambda name: "/usr/bin/" + name
        )
        monkeypatch.setattr(
            mod.subprocess, "Popen", lambda args, **kw: calls.append(args)
        )

        mod._play_tone_blocking(880, 200, 50, 50, mock.Mock())

        assert calls == [
            ["/usr/bin/play", "-qn", "synth", "0.200", "sine", "880", "vol", "0.50"]
        ]

    def test_wav_fallback_uses_paplay(self, monkeypatch, tmp_path):
        import os

        mod = _load_remote_module()
        calls = []

        def fake_which(name):
            return "/usr/bin/paplay" if name == "paplay" else None

        def fake_popen(args, **kw):
            assert os.path.exists(args[1])
            calls.append(args)

        monkeypatch.setattr(mod.shutil, "which", fake_which)
        monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(mod.tempfile, "tempdir", str(tmp_path))

        mod._play_tone_blocking(440, 100, 100, 100, mock.Mock())

        assert len(calls) == 1
        assert calls[0][0] == "/usr/bin/paplay"
        assert calls[0][1].endswith(".wav")

    def test_no_backend_logs_once_and_returns(self, monkeypatch):
        mod = _load_remote_module()
        log = mock.Mock()
        monkeypatch.setattr(mod.shutil, "which", lambda name: None)

        mod._play_tone_blocking(440, 100, 50, 50, log)

        log.assert_called_once()
