"""Released-Orca (51.1) fallbacks.

Released Orca 51.1's remote controller has none of the perf-branch
hooks the extension prefers -- no subscribe_keyboard_event, no
subscribe_braille_emitted, no display_braille_text. These tests pin
the fallbacks that keep a stock 51.1 install fully functional:

- master key forwarding via a KeyboardEvent.process patch,
- host braille mirroring via Extension.on_braille_output,
- inbound braille rendering via display_message_internal.
"""

from __future__ import annotations

import sys
import types
from unittest import mock

from tests.test_bypass_chords import _load_remote_module


class _Region:
    def __init__(self, string: str, cursor_offset: int = 0) -> None:
        self.string = string
        self.cursor_offset = cursor_offset


class _BrailleOutput:
    def __init__(self, regions, focused_region) -> None:
        self.regions = tuple(regions)
        self.focused_region = focused_region


def _instance(mod):
    instance = mod.RemoteExtension.__new__(mod.RemoteExtension)
    instance._log = mock.Mock()
    instance._keyboard_patch = None
    return instance


def test_on_braille_output_joins_regions_and_locates_cursor():
    mod = _load_remote_module()
    host = _instance(mod)
    host._on_braille_emitted = mock.Mock()

    focused = _Region("world", cursor_offset=2)
    output = _BrailleOutput([_Region("hello "), focused], focused)

    assert host.on_braille_output(output) is None
    host._on_braille_emitted.assert_called_once_with("hello world", 8)


def test_on_braille_output_without_focused_region_has_no_cursor():
    mod = _load_remote_module()
    host = _instance(mod)
    host._on_braille_emitted = mock.Mock()

    host.on_braille_output(_BrailleOutput([_Region("abc")], None))

    host._on_braille_emitted.assert_called_once_with("abc", -1)


class _FakeEvent:
    def __init__(self) -> None:
        self.id = 0x61  # keysym 'a'
        self.hw_code = 38
        self.modifiers = 0
        self._text = "a"
        self.processed = False

    def is_pressed_key(self) -> bool:
        return True


class _FakeKeyboardEvent:
    @staticmethod
    def process(event) -> str:
        event.processed = True
        return "orig"


def test_keyboard_process_patch_feeds_on_keyboard_event():
    mod = _load_remote_module()
    ext = _instance(mod)
    ext._on_keyboard_event = mock.Mock(return_value=True)

    fake_module = types.ModuleType("orca.input_event")
    fake_module.KeyboardEvent = _FakeKeyboardEvent
    saved = sys.modules.get("orca.input_event")
    sys.modules["orca.input_event"] = fake_module
    try:
        assert ext._hook_keyboard_process() is True

        consumed_event = _FakeEvent()
        assert _FakeKeyboardEvent.process(consumed_event) is True
        assert consumed_event.processed is False
        ext._on_keyboard_event.assert_called_once_with(True, 38, 0x61, 0, "a")

        ext._on_keyboard_event = mock.Mock(return_value=False)
        passed_event = _FakeEvent()
        assert _FakeKeyboardEvent.process(passed_event) == "orig"
        assert passed_event.processed is True

        ext._unhook_keyboard_process()
        restored_event = _FakeEvent()
        assert _FakeKeyboardEvent.process(restored_event) == "orig"
    finally:
        if saved is not None:
            sys.modules["orca.input_event"] = saved
        else:
            sys.modules.pop("orca.input_event", None)


class _ReleasedController:
    """51.1-shaped controller: no display_braille_text."""

    def __init__(self) -> None:
        self.messages: list = []

    def display_message_internal(self, text: str, persistent: bool = False) -> bool:
        self.messages.append((text, persistent))
        return True


def test_inbound_braille_falls_back_to_display_message_internal():
    mod = _load_remote_module()
    ext = _instance(mod)
    ext.controller = _ReleasedController()

    assert ext._render_inbound_braille_idle_cb("braille text") is False
    assert ext.controller.messages == [("braille text", False)]
