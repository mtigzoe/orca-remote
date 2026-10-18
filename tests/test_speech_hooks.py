"""Host-mode speech mirroring and inbound-speech diagnostics.

Two Orca APIs report outbound speech: the perf-branch controller hook
`subscribe_speech_emitted` (3 args) and released Orca's
`Extension.on_speech_output` (a SpeechOutput dataclass). RemoteExtension
must mirror on whichever exists, and must never echo speech back while
acting as the client.
"""

from __future__ import annotations

import asyncio
from unittest import mock

from tests.test_bypass_chords import _load_remote_module


class _SpeechOutput:
    """Stand-in for orca.extension.SpeechOutput."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.obj = None


def _instance(mod, role: str):
    instance = mod.RemoteExtension.__new__(mod.RemoteExtension)
    instance._settings = {"role": role}
    instance._focus_on_remote = False
    instance._inbound_speech_muted = False
    instance._mirror_speech = True
    instance._transport = object()
    instance._loop = object()
    instance._last_outbound_speech = ""
    instance._last_outbound_speech_at = 0.0
    instance._dropped_nonstring_items = 0
    instance._speak_ignored_reasons = set()
    instance._schedule_send = mock.Mock()
    instance._say_async = mock.Mock()
    instance._log = mock.Mock()
    return instance


def test_on_speech_output_mirrors_text_to_the_relay():
    mod = _load_remote_module()
    host = _instance(mod, "host")

    assert host.on_speech_output(_SpeechOutput("hello")) is None

    assert host._schedule_send.call_args.args[0] == {
        "type": mod.protocol.MSG_SPEAK,
        "sequence": ["hello"],
    }


def test_on_speech_output_ignores_an_empty_utterance():
    mod = _load_remote_module()
    host = _instance(mod, "host")

    host.on_speech_output(_SpeechOutput(""))

    host._schedule_send.assert_not_called()


def test_on_speech_output_does_not_echo_what_a_client_hears():
    mod = _load_remote_module()
    client = _instance(mod, "client")

    client.on_speech_output(_SpeechOutput("remote speech"))

    client._schedule_send.assert_not_called()


def test_focused_inbound_speech_is_spoken():
    mod = _load_remote_module()
    client = _instance(mod, "client")
    client._focus_on_remote = True

    asyncio.run(client._on_message(
        {"type": mod.protocol.MSG_SPEAK, "sequence": ["hi"]},
    ))

    client._say_async.assert_called_once_with("hi")
    client._log.assert_not_called()


def test_dropped_inbound_speech_logs_its_reason_once():
    mod = _load_remote_module()
    client = _instance(mod, "client")  # focused on the local machine
    message = {"type": mod.protocol.MSG_SPEAK, "sequence": ["hi"]}

    asyncio.run(client._on_message(message))
    asyncio.run(client._on_message(message))

    client._say_async.assert_not_called()
    assert client._log.call_args_list == [
        mock.call("inbound speech dropped (focused on the local machine)"),
    ]


def test_muted_inbound_speech_reports_the_mute_reason():
    mod = _load_remote_module()
    client = _instance(mod, "client")
    client._focus_on_remote = True
    client._inbound_speech_muted = True

    asyncio.run(client._on_message(
        {"type": mod.protocol.MSG_SPEAK, "sequence": ["hi"]},
    ))

    client._say_async.assert_not_called()
    assert client._log.call_args_list == [
        mock.call("inbound speech dropped (inbound muted)"),
    ]
