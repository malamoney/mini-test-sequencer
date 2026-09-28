"""Client behavior over real sockets: the simulator, plus a scripted server for faults."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from mini_sequencer.client import (
    DeviceClient,
    DeviceCommandError,
    DeviceConnectionError,
    DeviceProtocolError,
    DeviceTimeoutError,
)
from mini_sequencer.protocol import encode_message, make_request

TIMEOUT = 0.5

Script = Callable[[socket.socket, dict], None]


@pytest.fixture
def scripted_server() -> Iterator[Callable[[Script], tuple[str, int]]]:
    """A one-connection server that reads one request and runs ``script(conn, request)``."""
    listeners: list[socket.socket] = []
    threads: list[threading.Thread] = []

    def start(script: Script) -> tuple[str, int]:
        listener = socket.create_server(("127.0.0.1", 0))
        listeners.append(listener)

        def serve() -> None:
            conn, _ = listener.accept()
            with conn:
                data = b""
                while b"\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    data += chunk
                script(conn, json.loads(data.split(b"\n")[0]))
                # Hold the connection open until the client hangs up.
                conn.settimeout(2)
                try:
                    while conn.recv(4096):
                        pass
                except OSError:
                    pass

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        threads.append(thread)
        return listener.getsockname()[:2]

    yield start
    for listener in listeners:
        listener.close()
    for thread in threads:
        thread.join(timeout=3)


def reply(request: dict, **fields) -> bytes:
    return encode_message({"v": 1, "id": request["id"], "ok": True, "result": {}, **fields})


def run_script(scripted_server, script: Script, command=("identify",)):
    host, port = scripted_server(script)
    with DeviceClient(host, port, TIMEOUT) as client:
        if command[0] == "measure":
            return client.measure("voltage")
        return client.request(*command)


# -- against the real simulator -------------------------------------------


def test_identify_reset_measure(simulator):
    with DeviceClient(*simulator.address, TIMEOUT) as client:
        identity = client.identify()
        assert identity["model"] == "MTS-SIM-1"
        client.reset()
        m = client.measure("voltage")
        assert m.unit == "V" and 4.9 <= m.value <= 5.1


def test_simulator_is_deterministic_after_reset(simulator):
    with DeviceClient(*simulator.address, TIMEOUT) as client:
        client.reset()
        first = [client.measure(c).value for c in ("voltage", "current", "temperature")]
        client.reset()
        second = [client.measure(c).value for c in ("voltage", "current", "temperature")]
    assert first == second


def test_unknown_channel_is_a_device_error_and_connection_survives(simulator):
    with DeviceClient(*simulator.address, TIMEOUT) as client:
        with pytest.raises(DeviceCommandError) as info:
            client.measure("pressure")
        assert info.value.code == "invalid_argument"
        assert client.measure("voltage").unit == "V"


def test_unknown_command(simulator):
    with (
        DeviceClient(*simulator.address, TIMEOUT) as client,
        pytest.raises(DeviceCommandError, match="unknown_command"),
    ):
        client.request("self_destruct")


def test_simulator_timeout_profile_hits_deadline(simulator):
    simulator.configure("timeout", seed=1)
    with DeviceClient(*simulator.address, 0.2) as client:
        client.measure("voltage")
        started = time.monotonic()
        with pytest.raises(DeviceTimeoutError):
            client.measure("temperature")
        assert time.monotonic() - started < 2  # bounded, not hung


def test_simulator_malformed_profile(simulator):
    simulator.configure("malformed", seed=1)
    with DeviceClient(*simulator.address, TIMEOUT) as client, pytest.raises(DeviceProtocolError):
        client.measure("current")


def test_simulator_handles_fragmented_and_coalesced_requests(simulator):
    with socket.create_connection(simulator.address, timeout=2) as sock:
        both = encode_message(make_request("a", "identify")) + encode_message(
            make_request("b", "measure", {"channel": "voltage"})
        )
        for i in range(0, len(both), 5):
            sock.sendall(both[i : i + 5])
            time.sleep(0.001)
        data = b""
        while data.count(b"\n") < 2:
            data += sock.recv(4096)
    replies = [json.loads(line) for line in data.splitlines()]
    assert [r["id"] for r in replies] == ["a", "b"]
    assert all(r["ok"] for r in replies)


def test_simulator_answers_malformed_request_and_keeps_going(simulator):
    with socket.create_connection(simulator.address, timeout=2) as sock:
        sock.sendall(b"{nope\n" + encode_message(make_request("ok", "identify")))
        data = b""
        while data.count(b"\n") < 2:
            data += sock.recv(4096)
    first, second = (json.loads(line) for line in data.splitlines())
    assert first["error"]["code"] == "malformed_request"
    assert second["id"] == "ok" and second["ok"] is True


def test_simulator_drops_oversized_request(simulator):
    with socket.create_connection(simulator.address, timeout=2) as sock:
        sock.sendall(b"x" * (70 * 1024))
        data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    assert json.loads(data)["error"]["code"] == "frame_too_large"


# -- protocol faults from a scripted server ---------------------------------


def test_fragmented_reply(scripted_server):
    def script(conn, req):
        frame = reply(req, result={"model": "M", "firmware": "F"})
        for byte in frame:
            conn.sendall(bytes([byte]))

    host, port = scripted_server(script)
    with DeviceClient(host, port, TIMEOUT) as client:
        assert client.identify() == {"model": "M", "firmware": "F"}


def test_mismatched_reply_id(scripted_server):
    def script(conn, req):
        conn.sendall(encode_message({"v": 1, "id": "999", "ok": True, "result": {}}))

    with pytest.raises(DeviceProtocolError, match="does not match"):
        run_script(scripted_server, script)


def test_unexpected_extra_reply_is_detected_on_next_request(scripted_server):
    def script(conn, req):
        # A stray extra message coalesced after the valid reply.
        conn.sendall(reply(req) + encode_message({"v": 1, "id": "stray", "ok": True, "result": {}}))

    host, port = scripted_server(script)
    with DeviceClient(host, port, TIMEOUT) as client:
        client.request("reset")
        with pytest.raises(DeviceProtocolError, match="'stray'"):
            client.request("reset")


def test_malformed_json_reply(scripted_server):
    with pytest.raises(DeviceProtocolError, match="malformed"):
        run_script(scripted_server, lambda conn, req: conn.sendall(b"{broken\n"))


def test_oversized_reply(scripted_server):
    def script(conn, req):
        conn.sendall(b"x" * (65 * 1024))

    with pytest.raises(DeviceProtocolError, match="exceeds"):
        run_script(scripted_server, script)


def test_disconnect_mid_request(scripted_server):
    def script(conn, req):
        conn.sendall(b'{"v": 1, "id"')
        conn.shutdown(socket.SHUT_RDWR)

    with pytest.raises(DeviceConnectionError, match="closed the connection"):
        run_script(scripted_server, script)


def test_silent_device_times_out_within_bound(scripted_server):
    started = time.monotonic()
    with pytest.raises(DeviceTimeoutError):
        run_script(scripted_server, lambda conn, req: None)
    assert time.monotonic() - started < TIMEOUT + 1.5


def test_trickling_bytes_cannot_extend_deadline(scripted_server):
    def script(conn, req):
        for _ in range(40):
            try:
                conn.sendall(b" ")
            except OSError:
                return
            time.sleep(0.05)

    started = time.monotonic()
    with pytest.raises(DeviceTimeoutError):
        run_script(scripted_server, script)
    assert time.monotonic() - started < TIMEOUT + 1.0


@pytest.mark.parametrize(
    ("result_json", "match"),
    [
        ('{"value": "5.0", "unit": "V"}', "invalid value"),
        ('{"value": true, "unit": "V"}', "invalid value"),
        ('{"unit": "V"}', "invalid value"),
        ('{"value": 5.0}', "invalid unit"),
        ('{"value": 1e999, "unit": "V"}', "non-finite"),  # JSON overflow decodes to inf
    ],
)
def test_invalid_measurement_payloads(scripted_server, result_json, match):
    def script(conn, req):
        frame = f'{{"v": 1, "id": "{req["id"]}", "ok": true, "result": {result_json}}}\n'
        conn.sendall(frame.encode())

    with pytest.raises(DeviceProtocolError, match=match):
        run_script(scripted_server, script, command=("measure",))


def test_error_reply_without_error_object(scripted_server):
    def script(conn, req):
        conn.sendall(encode_message({"v": 1, "id": req["id"], "ok": False}))

    with pytest.raises(DeviceProtocolError, match="no error object"):
        run_script(scripted_server, script)


def test_connection_refused():
    with socket.create_server(("127.0.0.1", 0)) as s:
        port = s.getsockname()[1]
    with pytest.raises((DeviceConnectionError, DeviceTimeoutError)):
        DeviceClient("127.0.0.1", port, TIMEOUT).connect()
