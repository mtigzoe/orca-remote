"""Session-state regressions: stuck keys, stale grabs, dropped speech."""

from __future__ import annotations

from unittest import mock

from tests.test_bypass_chords import _load_remote_module

INSERT, ALT_L, TAB, LETTER_H = 0xff63, 0xffe9, 0xff09, 0x68


def _master(mod):
    instance = mod.RemoteExtension.__new__(mod.RemoteExtension)
    instance._settings = {"role": "client"}
    instance._focus_on_remote = True
    instance._transport = object()
    instance._loop = None
    instance._master_orca_mods_down = set()
    instance._forwarded_keys_down = set()
    instance._master_grab_ids = None
    instance._master_grab = None
    instance._inbound_speech_muted = False
    instance._pressed_keysyms = set()
    instance._schedule_send = mock.Mock()
    instance._say = mock.Mock()
    instance._log = mock.Mock()
    return instance


def _sent(instance):
    return [
        (c.args[0]["vk_code"], c.args[0]["pressed"])
        for c in instance._schedule_send.call_args_list
    ]


def test_leaving_remote_releases_the_held_insert_and_alt():
    mod = _load_remote_module()
    m = _master(mod)
    m._on_keyboard_event(True, 0, INSERT, 0, "")
    m._on_keyboard_event(True, 0, ALT_L, mod._AT_SPI_MOD_ALT, "")
    # Tab is the bypassed trigger: not forwarded, switch_side runs locally.
    assert m._on_keyboard_event(True, 0, TAB, mod._AT_SPI_MOD_ALT, "") is False
    m._schedule_send.reset_mock()

    m.switch_side()

    assert m._focus_on_remote is False
    assert sorted(_sent(m)) == [(0x12, False), (0x2D, False)]


def test_release_of_key_pressed_while_local_is_not_forwarded():
    mod = _load_remote_module()
    m = _master(mod)
    assert m._on_keyboard_event(False, 0, ALT_L, 0, "") is True
    m._schedule_send.assert_not_called()


def test_insert_autorepeat_does_not_leave_bypass_stuck():
    mod = _load_remote_module()
    m = _master(mod)
    for _ in range(5):
        m._on_keyboard_event(True, 0, INSERT, 0, "")
    m._on_keyboard_event(False, 0, INSERT, 0, "")
    # Plain Ctrl+M on the remote must forward, not bypass.
    assert not m._is_local_bypass_chord(0x6d, mod._AT_SPI_MOD_CTRL)


def test_caps_lock_orca_modifier_keeps_switch_side_local():
    mod = _load_remote_module()
    m = _master(mod)
    m._on_keyboard_event(True, 0, 0xffe5, 0, "")
    assert m._is_local_bypass_chord(TAB, mod._AT_SPI_MOD_ALT)


def test_stop_transport_drops_remote_focus_and_grab():
    mod = _load_remote_module()
    m = _master(mod)
    m._master_grab_ids = []
    m._on_keyboard_event(True, 0, LETTER_H, 0, "")
    m._schedule_send.reset_mock()
    m._release_held_keys = mock.Mock()

    m._stop_transport()

    assert m._focus_on_remote is False
    assert m._master_grab_ids is None
    assert _sent(m) == [(0x48, False)]


def test_repeated_host_speech_is_sent_after_the_dedupe_window():
    mod = _load_remote_module()
    m = _master(mod)
    m._settings = {"role": "host"}
    m._loop = object()
    m._mirror_speech = True
    m._last_outbound_speech = ""
    m._last_outbound_speech_at = 0.0
    with mock.patch.object(mod.time, "monotonic", side_effect=[100.0, 100.1, 101.0]):
        m._on_speech_emitted("blank", "", "")
        m._on_speech_emitted("blank", "", "")  # same event reported twice
        m._on_speech_emitted("blank", "", "")  # next blank line
    assert m._schedule_send.call_count == 2


def test_failed_modifier_release_is_still_forgotten():
    mod = _load_remote_module()
    m = _master(mod)
    m._chord_matches_own_command = mock.Mock(return_value=False)
    m.controller = mock.Mock()
    m.controller.synthesize_key_event = mock.Mock(return_value=True)
    m._synthesize_key_idle_cb(0xffe3, True)
    m.controller.synthesize_key_event.return_value = False
    m._synthesize_key_idle_cb(0xffe3, False)
    assert 0xffe3 not in m._pressed_keysyms


def test_connect_redials_when_transport_is_not_connected():
    mod = _load_remote_module()
    m = _master(mod)
    m._set_auto_connect = mock.Mock()
    m._restart_transport = mock.Mock()
    m._transport = mock.Mock(connected=False)
    m.connect()
    m._restart_transport.assert_called_once()

    m._restart_transport.reset_mock()
    m._transport = mock.Mock(connected=True)
    m.connect()
    m._restart_transport.assert_not_called()


def test_inbound_caps_lock_is_held_for_orca_chords():
    mod = _load_remote_module()
    m = _master(mod)
    m._chord_matches_own_command = mock.Mock(return_value=False)
    m.controller = mock.Mock()
    m.controller.synthesize_key_event = mock.Mock(return_value=True)
    m._synthesize_key_idle_cb(0xffe5, True)
    m.controller.synthesize_key_event.assert_called_once_with(0xffe5, True)


def test_legacy_host_releases_keys_left_down_by_a_dropped_link(monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "orca-scripts"))
    import local_machine

    machine = local_machine.LocalMachine()
    injected = []
    monkeypatch.setattr(machine, "_resolve_key", lambda name, vk, ext: name)
    monkeypatch.setattr(machine, "_inject", lambda key, pressed: injected.append((key, pressed)))
    machine.send_key(key_name="ctrl", pressed=True)
    machine.send_key(key_name="h", pressed=True)
    machine.send_key(key_name="h", pressed=False)
    injected.clear()

    machine.release_all_keys()

    assert injected == [("ctrl", False)]
