"""Key injection backends for the host (controlled) side.

XTEST, through `controller.synthesize_key_event`, covers Xorg and
XWayland clients but cannot reach native Wayland clients. This module
adds the backends that can, plus one that covers Orca builds which have
no controller hook at all:

- the XDG RemoteDesktop portal (GNOME and KDE Wayland; the compositor
  asks the user once to allow keyboard control),
- ydotool (uinput; any compositor, needs ydotoold running),
- xdotool (X11, and XWayland clients on Wayland).

Backend order follows the session: Wayland tries ydotool, portal,
xdotool; Xorg tries xdotool, ydotool, portal. The caller only prefers
this module when it is the better path -- always on Wayland, and on Xorg
when the controller hook is missing (released Orca) or failed.

Everything here runs on a worker thread. Portal setup can block on the
user's consent dialog, and key injection does subprocess calls; neither
may freeze Orca's GLib main loop. The queue is FIFO, so key order on the
wire is preserved.
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import uuid
from typing import Any, Callable

PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
PORTAL_OBJECT_PATH = "/org/freedesktop/portal/desktop"
PORTAL_REMOTE_DESKTOP_IFACE = "org.freedesktop.portal.RemoteDesktop"
PORTAL_REQUEST_IFACE = "org.freedesktop.portal.Request"
PORTAL_KEYBOARD = 1 << 0
_PORTAL_CONSENT_TIMEOUT_SECONDS = 180
_PORTAL_CALL_TIMEOUT_MS = 5000

_WAYLAND_BACKENDS = ("ydotool", "portal", "xdotool")
_XORG_BACKENDS = ("xdotool", "ydotool", "portal")


def is_wayland_session() -> bool:
    """True when this is a Wayland session (compositor-provided input)."""

    return (
        os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
        or bool(os.environ.get("WAYLAND_DISPLAY"))
    )


def resolve_keysym(keysym: int) -> "tuple[str | None, int | None]":
    """Return (xdg key name, evdev keycode) for keysym.

    Call on the GLib main thread: it touches GDK. The evdev code is the
    X keycode minus 8, which is the mapping every evdev-based X server
    uses, and it follows the user's actual layout.
    """

    name = None
    evdev = None
    try:
        import gi  # pylint: disable=import-outside-toplevel

        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk  # pylint: disable=import-outside-toplevel

        name = Gdk.keyval_name(keysym) or None
        display = Gdk.Display.get_default()
        if display is not None:
            keymap = Gdk.Keymap.get_for_display(display)
            found, entries = keymap.get_entries_for_keyval(keysym)
            if found and entries:
                keycode = int(entries[0].keycode) - 8
                if keycode > 0:
                    evdev = keycode
    except Exception:  # pylint: disable=broad-except
        pass
    return name, evdev


def _load_gio() -> "tuple[Any, Any]":
    """Import GLib and Gio lazily (only the worker thread needs them)."""

    import gi  # pylint: disable=import-outside-toplevel

    from gi.repository import Gio, GLib  # pylint: disable=import-outside-toplevel

    return GLib, Gio


class _PortalKeyboard:
    """Keyboard injection through the XDG RemoteDesktop portal."""

    def __init__(self, log: Callable[[str], None]) -> None:
        self._log = log
        self._lock = threading.RLock()
        self._bus: Any = None
        self._session: Any = None
        self._started = False
        self.denied = False
        self.unavailable = False
        self._warned = False

    def send(self, keysym: int, pressed: bool) -> bool:
        """Notify one keyboard event. Returns True when the portal took it."""

        if self.unavailable or self.denied:
            return False
        try:
            if not self._ensure_session():
                return False
            GLib, Gio = _load_gio()
            self._bus.call_sync(
                PORTAL_BUS_NAME,
                PORTAL_OBJECT_PATH,
                PORTAL_REMOTE_DESKTOP_IFACE,
                "NotifyKeyboardKeysym",
                GLib.Variant(
                    "(oa{sv}iu)",
                    (self._session, {}, int(keysym), 1 if pressed else 0),
                ),
                None,
                Gio.DBusCallFlags.NONE,
                _PORTAL_CALL_TIMEOUT_MS,
                None,
            )
            return True
        except Exception as error:  # pylint: disable=broad-except
            self._started = False
            self._fail(f"RemoteDesktop portal key injection failed: {error}")
            return False

    def _ensure_session(self) -> bool:
        with self._lock:
            if self._started:
                return True
            if self.denied or self.unavailable:
                return False

            GLib, Gio = _load_gio()
            try:
                self._bus = self._session_bus(GLib, Gio)
            except Exception as error:  # pylint: disable=broad-except
                self.unavailable = True
                self._fail(f"no session bus for the RemoteDesktop portal: {error}")
                return False

            session_token = self._new_token("session")
            created = self._request(
                "CreateSession",
                lambda token: GLib.Variant("(a{sv})", ({
                    "handle_token": GLib.Variant("s", token),
                    "session_handle_token": GLib.Variant("s", session_token),
                },)),
            )
            self._session = self._unpack(created.get("session_handle"))
            if not self._session:
                self.unavailable = True
                self._fail("RemoteDesktop portal returned no session handle")
                return False

            self._request(
                "SelectDevices",
                lambda token: GLib.Variant("(oa{sv})", (
                    self._session,
                    {
                        "handle_token": GLib.Variant("s", token),
                        "types": GLib.Variant("u", PORTAL_KEYBOARD),
                    },
                )),
            )
            started = self._request(
                "Start",
                lambda token: GLib.Variant("(osa{sv})", (
                    self._session,
                    "",
                    {"handle_token": GLib.Variant("s", token)},
                )),
            )
            devices = self._unpack(started.get("devices"), 0) or 0
            if not int(devices) & PORTAL_KEYBOARD:
                self.denied = True
                self._fail("RemoteDesktop portal: keyboard access was not granted")
                return False
            self._started = True
            return True

    @staticmethod
    def _session_bus(GLib: Any, Gio: Any) -> Any:
        """Connect to the session bus without X11 autolaunch.

        Gio.bus_get_sync falls back to dbus-launch when
        DBUS_SESSION_BUS_ADDRESS is unset, which can block for a long
        time -- and this call would then hold the injection worker (and
        every queued key) behind it. Use the standard runtime socket
        instead, and fail fast when there is none.
        """

        address = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        if not address:
            runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or GLib.get_user_runtime_dir()
            socket_path = os.path.join(runtime_dir or "", "bus")
            if not os.path.exists(socket_path):
                raise RuntimeError("no session bus socket")
            address = f"unix:path={socket_path}"
        return Gio.DBusConnection.new_for_address_sync(
            address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None,
            None,
        )

    def _request(self, method: str, params_factory: Callable[[str], Any]) -> dict:
        """Send one portal request and wait for its Response signal."""

        GLib, Gio = _load_gio()
        token = self._new_token(method.lower())
        expected_path = self._request_path(token)
        result: dict[str, Any] = {"done": False, "response": None, "results": None}
        loop = GLib.MainLoop()

        def on_response(
            _connection, _sender, _path, _iface, _signal, parameters, _user_data,
        ) -> None:
            response, results = parameters.unpack()
            result["done"] = True
            result["response"] = response
            result["results"] = results
            loop.quit()

        subscriptions = [
            self._bus.signal_subscribe(
                PORTAL_BUS_NAME, PORTAL_REQUEST_IFACE, "Response",
                expected_path, None, Gio.DBusSignalFlags.NONE, on_response, None,
            ),
        ]
        timeout_id = GLib.timeout_add_seconds(_PORTAL_CONSENT_TIMEOUT_SECONDS, loop.quit)
        try:
            reply = self._bus.call_sync(
                PORTAL_BUS_NAME, PORTAL_OBJECT_PATH, PORTAL_REMOTE_DESKTOP_IFACE,
                method, params_factory(token), GLib.VariantType.new("(o)"),
                Gio.DBusCallFlags.NONE, 30000, None,
            )
            request_path = reply.unpack()[0]
            if request_path != expected_path:
                subscriptions.append(self._bus.signal_subscribe(
                    PORTAL_BUS_NAME, PORTAL_REQUEST_IFACE, "Response",
                    request_path, None, Gio.DBusSignalFlags.NONE, on_response, None,
                ))
            if not result["done"]:
                loop.run()
        finally:
            try:
                GLib.source_remove(timeout_id)
            except Exception:  # pylint: disable=broad-except
                pass
            for subscription in subscriptions:
                self._bus.signal_unsubscribe(subscription)

        if not result["done"]:
            raise RuntimeError(f"timed out waiting for portal {method} response")
        if result["response"] == 1:
            self.denied = True
            raise PermissionError(f"portal {method} was denied")
        if result["response"] != 0:
            raise RuntimeError(f"portal {method} failed with response {result['response']}")
        return result["results"] or {}

    def _request_path(self, token: str) -> str:
        sender = self._bus.get_unique_name()[1:].replace(".", "_")
        return f"/org/freedesktop/portal/desktop/request/{sender}/{token}"

    def _fail(self, message: str) -> None:
        if self._warned:
            return
        self._warned = True
        self._log(f"key injection: {message}")

    @staticmethod
    def _new_token(prefix: str) -> str:
        return f"orcaremote_{prefix}_{uuid.uuid4().hex}"

    @staticmethod
    def _unpack(value: Any, default: Any = None) -> Any:
        if value is None:
            return default
        if hasattr(value, "unpack"):
            return value.unpack()
        return value


class KeyInjector:
    """Runs key injection off the GLib thread, picking a working backend."""

    def __init__(self, log: Callable[[str], None]) -> None:
        self._log = log
        self._queue: "queue.Queue[tuple[int, bool, str | None, int | None]]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._portal = _PortalKeyboard(log)
        self._backend: str | None = None
        # True once every backend failed; the caller should stop
        # preferring this module and use the controller instead.
        self.exhausted = False

    def send(self, keysym: int, pressed: bool, name: "str | None" = None,
             evdev: "int | None" = None) -> None:
        """Queue one key event (called from the GLib thread)."""

        self._ensure_thread()
        self._queue.put((int(keysym), bool(pressed), name, evdev))

    def _ensure_thread(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="orca-remote-inject", daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while True:
            keysym, pressed, name, evdev = self._queue.get()
            try:
                if not self._dispatch(keysym, pressed, name, evdev):
                    self.exhausted = True
            except Exception as error:  # pylint: disable=broad-except
                self._log(f"key injection failed: {error}")

    def _dispatch(self, keysym: int, pressed: bool, name: "str | None",
                  evdev: "int | None") -> bool:
        order = list(_WAYLAND_BACKENDS if is_wayland_session() else _XORG_BACKENDS)
        if self._backend in order:
            order.remove(self._backend)
            order.insert(0, self._backend)

        usable = [
            backend for backend in order
            if self._usable(backend, name, evdev)
        ]
        if not usable:
            self._fail_once(
                "no external backend available on this session "
                "(install xdotool or ydotool, or allow the RemoteDesktop "
                "portal); falling back to XTEST"
            )
            return False

        for backend in usable:
            if backend == "xdotool":
                ok = self._send_xdotool(name, pressed)
            elif backend == "ydotool":
                ok = self._send_ydotool(evdev, pressed)
            else:
                ok = self._portal.send(keysym, pressed)
            if ok:
                if self._backend != backend:
                    self._backend = backend
                    self._log(f"key injection backend: {backend}")
                return True
        return False

    def _usable(self, backend: str, name: "str | None", evdev: "int | None") -> bool:
        if backend == "xdotool":
            return bool(name) and shutil.which("xdotool") is not None
        if backend == "ydotool":
            return bool(evdev) and shutil.which("ydotool") is not None
        # The portal needs only the session bus; the first call tells us
        # whether it exists and whether the user grants keyboard access.
        return not (self._portal.denied or self._portal.unavailable)

    def _fail_once(self, message: str) -> None:
        if self.exhausted:
            return
        self._log(f"key injection: {message}")

    @staticmethod
    def _send_xdotool(name: str, pressed: bool) -> bool:
        try:
            result = subprocess.run(
                ["xdotool", "keydown" if pressed else "keyup", name],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except Exception:  # pylint: disable=broad-except
            return False

    @staticmethod
    def _send_ydotool(evdev: int, pressed: bool) -> bool:
        try:
            result = subprocess.run(
                ["ydotool", "key", f"{evdev}:{1 if pressed else 0}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            return result.returncode == 0
        except Exception:  # pylint: disable=broad-except
            return False
