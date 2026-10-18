"""Host-side key injection backends: Wayland, Xorg, and fallbacks.

The rule under test: XTEST (controller.synthesize_key_event) is the
default where it can reach clients. On Wayland, and on Orca builds
without the hook, the external backends (ydotool, RemoteDesktop portal,
xdotool) take over, and each failure falls through to the next one.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

from tests.test_bypass_chords import _load_remote_module

ROOT = Path(__file__).resolve().parent.parent


def _load_inject():
    path = ROOT / "inject.py"
    spec = importlib.util.spec_from_file_location("orca_remote_inject", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["orca_remote_inject"] = module
    spec.loader.exec_module(module)
    return module


def _injector(mod):
    log = mock.Mock()
    injector = mod.KeyInjector(log)
    injector._send_xdotool = mock.Mock(return_value=True)
    injector._send_ydotool = mock.Mock(return_value=True)
    injector._portal.send = mock.Mock(return_value=True)
    return injector, log


class TestSessionDetection:
    def test_wayland_from_session_type(self, monkeypatch):
        mod = _load_inject()
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        assert mod.is_wayland_session()

    def test_wayland_from_display_variable(self, monkeypatch):
        mod = _load_inject()
        monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        assert mod.is_wayland_session()

    def test_xorg_when_neither_is_set(self, monkeypatch):
        mod = _load_inject()
        monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        assert not mod.is_wayland_session()


class TestBackendOrder:
    def test_wayland_prefers_ydotool(self, monkeypatch):
        mod = _load_inject()
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/" + name)
        injector, _log = _injector(mod)

        assert injector._dispatch(0x61, True, "a", 30)
        injector._send_ydotool.assert_called_once_with(30, True)
        injector._send_xdotool.assert_not_called()
        assert injector._backend == "ydotool"

    def test_wayland_falls_back_to_portal_then_xdotool(self, monkeypatch):
        mod = _load_inject()
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/" + name)
        injector, _log = _injector(mod)
        injector._send_ydotool.return_value = False
        injector._send_xdotool.return_value = False

        assert injector._dispatch(0x61, True, "a", 30)
        injector._portal.send.assert_called_once_with(0x61, True)
        assert injector._backend == "portal"

    def test_xorg_prefers_xdotool(self, monkeypatch):
        mod = _load_inject()
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/" + name)
        injector, _log = _injector(mod)

        assert injector._dispatch(0xffe3, True, "Control_L", 37)
        injector._send_xdotool.assert_called_once_with("Control_L", True)
        injector._send_ydotool.assert_not_called()

    def test_remembers_the_backend_that_worked(self, monkeypatch):
        mod = _load_inject()
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/" + name)
        injector, _log = _injector(mod)
        injector._send_ydotool.return_value = False

        injector._dispatch(0x61, True, "a", 30)   # ydotool fails -> portal
        injector._send_ydotool.reset_mock()
        injector._dispatch(0x62, True, "b", 48)   # portal first now
        injector._send_ydotool.assert_not_called()

    def test_exhausted_when_nothing_works(self, monkeypatch):
        mod = _load_inject()
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        monkeypatch.setattr(mod.shutil, "which", lambda _name: None)
        injector, _log = _injector(mod)
        injector._portal.send.return_value = False
        injector._portal.unavailable = True

        injector.send(0x61, True, name=None, evdev=None)
        for _ in range(200):
            if injector.exhausted:
                break
            import time

            time.sleep(0.01)
        assert injector.exhausted
        assert not injector._dispatch(0x61, True, None, None)


class TestKeysymResolution:
    def test_name_and_evdev_come_from_gdk(self, monkeypatch):
        mod = _load_inject()

        import types as pytypes

        class Entry:
            keycode = 38  # X keycode; evdev is 30 for 'a'

        keymap = pytypes.SimpleNamespace(
            get_entries_for_keyval=lambda _keysym: (True, [Entry()]),
        )
        Gdk = pytypes.SimpleNamespace(
            keyval_name=lambda _keysym: "a",
            Display=pytypes.SimpleNamespace(get_default=lambda: object()),
            Keymap=pytypes.SimpleNamespace(
                get_for_display=lambda _display: keymap,
            ),
        )

        gi = pytypes.ModuleType("gi")
        gi.require_version = lambda *a, **k: None
        repository = pytypes.ModuleType("gi.repository")
        repository.Gdk = Gdk
        monkeypatch.setitem(sys.modules, "gi", gi)
        monkeypatch.setitem(sys.modules, "gi.repository", repository)

        name, evdev = mod.resolve_keysym(0x61)
        assert name == "a"
        assert evdev == 30

    def test_unavailable_gdk_resolves_to_none(self, monkeypatch):
        mod = _load_inject()
        monkeypatch.setitem(sys.modules, "gi", None)
        assert mod.resolve_keysym(0x61) == (None, None)


class TestRemoteSynthSelection:
    """RemoteExtension._synth picks XTEST or the injector per session."""

    def _instance(self, mod, has_hook: bool = True):
        instance = mod.RemoteExtension.__new__(mod.RemoteExtension)
        instance._log = mock.Mock()
        instance.controller = mock.Mock()
        if not has_hook:
            del instance.controller.synthesize_key_event
        instance._injector = mock.Mock()
        instance._injector.exhausted = False
        return instance

    def test_xorg_uses_the_controller(self, monkeypatch):
        mod = _load_remote_module()
        instance = self._instance(mod)
        monkeypatch.setattr(mod, "is_wayland_session", lambda: False)

        assert instance._synth(0x61, True)
        instance.controller.synthesize_key_event.assert_called_once_with(0x61, True)
        instance._injector.send.assert_not_called()

    def test_wayland_prefers_the_injector(self, monkeypatch):
        mod = _load_remote_module()
        instance = self._instance(mod)
        monkeypatch.setattr(mod, "is_wayland_session", lambda: True)
        monkeypatch.setattr(mod, "resolve_keysym", lambda keysym: ("a", 30))

        assert instance._synth(0x61, True)
        instance._injector.send.assert_called_once_with(
            0x61, True, name="a", evdev=30,
        )
        instance.controller.synthesize_key_event.assert_not_called()

    def test_released_orca_without_the_hook_uses_the_injector(self, monkeypatch):
        mod = _load_remote_module()
        instance = self._instance(mod, has_hook=False)
        monkeypatch.setattr(mod, "is_wayland_session", lambda: False)
        monkeypatch.setattr(mod, "resolve_keysym", lambda keysym: ("a", 30))

        assert instance._synth(0x61, True)
        instance._injector.send.assert_called_once()

    def test_controller_failure_falls_back_to_the_injector(self, monkeypatch):
        mod = _load_remote_module()
        instance = self._instance(mod)
        instance.controller.synthesize_key_event.return_value = False
        monkeypatch.setattr(mod, "is_wayland_session", lambda: False)
        monkeypatch.setattr(mod, "resolve_keysym", lambda keysym: ("a", 30))

        assert instance._synth(0x61, True) is False
        instance._injector.send.assert_called_once()

    def test_exhausted_injector_never_preempts_on_wayland(self, monkeypatch):
        mod = _load_remote_module()
        instance = self._instance(mod)
        instance._injector.exhausted = True
        monkeypatch.setattr(mod, "is_wayland_session", lambda: True)

        assert instance._synth(0x61, True)
        instance.controller.synthesize_key_event.assert_called_once_with(0x61, True)
        instance._injector.send.assert_not_called()
