"""The threaded relay transport in orca-scripts releases what it holds."""

from __future__ import annotations

import sys
import threading
import json
import ssl
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "orca-scripts"))

import transport  # noqa: E402


class FakeSocket:
    def __init__(self):
        self.closed = False
        self.sent: list[bytes] = []

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def close(self) -> None:
        self.closed = True


def test_disconnect_after_connection_drop_closes_socket_and_sender():
    tcp = transport.TCPTransport(serializer=None, address=("example.invalid", 6837))
    sock = FakeSocket()
    tcp.server_sock = sock
    sender = threading.Thread(target=tcp.send_queue, daemon=True)
    tcp.queue_thread = sender
    sender.start()
    # run() clears the flag before it disconnects.
    tcp.connected = False

    tcp._disconnect()

    sender.join(timeout=2)
    assert not sender.is_alive()
    assert sock.closed is True
    assert tcp.server_sock is None
    assert tcp.queue_thread is None


def test_eof_discards_an_unterminated_frame():
    tcp = transport.TCPTransport(serializer=None, address=("example.invalid", 6837))
    sock = mock.Mock()
    sock.recv.return_value = b""
    tcp.server_sock = sock
    tcp.buffer = b'{"type":"speak"'

    tcp.handle_server_data()

    assert tcp.server_sock is None
    assert tcp.buffer == b""


def test_disconnect_unblocks_sender_before_joining():
    tcp = transport.TCPTransport(serializer=None, address=("example.invalid", 6837))
    sock = mock.Mock()
    tcp.server_sock = sock
    sender = mock.Mock()
    tcp.queue_thread = sender
    sender.join.side_effect = lambda: sock.shutdown.assert_called_once()

    tcp._disconnect()

    sock.close.assert_called_once()


def test_non_string_message_type_does_not_kill_legacy_reader():
    tcp = transport.TCPTransport(serializer=mock.Mock(), address=("example.invalid", 6837))
    tcp.serializer.deserialize.side_effect = json.loads
    received = mock.Mock()
    tcp.callback_manager.register_callback("msg_ping", received)

    tcp.parse(b'{"type":[]}')
    tcp.parse(b'{"type":"ping"}')

    received.assert_called_once_with()


def test_tls_session_ticket_want_read_is_not_a_disconnect():
    tcp = transport.TCPTransport(serializer=None, address=("example.invalid", 6837))
    sock = mock.Mock()
    sock.gettimeout.return_value = 7
    sock.recv.side_effect = ssl.SSLWantReadError()
    tcp.server_sock = sock

    tcp.handle_server_data()

    assert tcp.server_sock is sock
    sock.setblocking.assert_called_once_with(False)
    sock.settimeout.assert_called_once_with(7)


def test_tls_io_is_serialized_between_reader_and_sender():
    tcp = transport.TCPTransport(serializer=None, address=("example.invalid", 6837))
    sock = mock.Mock()
    sock.recv.side_effect = lambda _size: b"partial" if tcp._io_lock.locked() else None
    sock.sendall.side_effect = lambda _data: locked.append(tcp._io_lock.locked())
    tcp.server_sock = sock
    locked = []
    tcp.queue.put(b"probe")
    tcp.queue.put(None)

    tcp.send_queue()
    tcp.handle_server_data()

    assert locked == [True]
    assert tcp.buffer == b"partial"


def test_reader_drains_decrypted_pending_bytes_before_select(monkeypatch):
    tcp = transport.TCPTransport(serializer=mock.Mock(), address=("example.invalid", 6837))
    tcp.serializer.deserialize.side_effect = json.loads
    sock = mock.Mock()
    sock.pending.return_value = 1
    sock.recv.side_effect = [b'{"type":"ping"}\n', b""]
    received = mock.Mock()
    tcp.callback_manager.register_callback("msg_ping", received)
    monkeypatch.setattr(tcp, "create_outbound_socket", lambda _address: sock)
    select = mock.Mock(side_effect=AssertionError("pending TLS plaintext should be read first"))
    monkeypatch.setattr(transport.select, "select", select)
    try:
        tcp.run()
    finally:
        tcp._disconnect()

    received.assert_called_once_with()
    select.assert_not_called()
