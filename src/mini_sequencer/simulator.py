"""A simulated device reachable over TCP.

The simulator answers ``identify``, ``reset``, and ``measure`` requests. Its
behavior is chosen by a named :class:`Profile` (nominal or one fault
scenario) set at startup or through :meth:`DeviceSimulator.configure`. Fault
scenarios are never selectable over the socket, so a client cannot make the
device misbehave by what it sends.

Measurements come from a seeded random generator that ``reset`` rewinds, so
the same profile and seed always yield the same values in the same order.
"""

from __future__ import annotations

import random
import socket
import socketserver
import threading
from dataclasses import dataclass, field
from typing import Any

from mini_sequencer import __version__
from mini_sequencer.protocol import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    FrameDecoder,
    FrameTooLargeError,
    MalformedMessageError,
    decode_message,
    encode_message,
    make_error,
    make_result,
)

MODEL = "MTS-SIM-1"
DRAIN_TIMEOUT_S = 1.0
DRAIN_LIMIT_BYTES = 1024 * 1024
FIRMWARE = "1.0.0"

# channel -> (unit, low, high) for a healthy unit; all inside the example limits.
NOMINAL_RANGES: dict[str, tuple[str, float, float]] = {
    "voltage": ("V", 4.90, 5.10),
    "current": ("mA", 60.0, 100.0),
    "temperature": ("C", 22.0, 35.0),
}


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    # channel -> (low, high) replacing the nominal range
    ranges: dict[str, tuple[float, float]] = field(default_factory=dict)
    # A transport fault triggered by measuring ``fault_channel``.
    fault: str | None = None
    fault_channel: str | None = None


PROFILES: dict[str, Profile] = {
    p.name: p
    for p in (
        Profile("nominal", "all channels within normal range"),
        Profile("undervoltage", "supply voltage below 4.75 V", ranges={"voltage": (4.40, 4.70)}),
        Profile("overcurrent", "idle current above 120 mA", ranges={"current": (130.0, 180.0)}),
        Profile("overtemperature", "temperature above 45 C", ranges={"temperature": (50.0, 60.0)}),
        Profile(
            "timeout",
            "never answers a temperature measurement",
            fault="timeout",
            fault_channel="temperature",
        ),
        Profile(
            "malformed",
            "answers a current measurement with invalid JSON",
            fault="malformed",
            fault_channel="current",
        ),
    )
}


class DeviceState:
    """Simulated device state shared by all connections."""

    def __init__(self, profile: str = "nominal", seed: int = 0) -> None:
        self._lock = threading.Lock()
        self.configure(profile, seed)

    def configure(self, profile: str, seed: int) -> None:
        if profile not in PROFILES:
            raise ValueError(f"unknown profile {profile!r} (choose from {', '.join(PROFILES)})")
        with self._lock:
            self.profile = PROFILES[profile]
            self.seed = seed
            self._rng = random.Random(seed)

    def reset(self) -> None:
        with self._lock:
            self._rng = random.Random(self.seed)

    def identity(self) -> dict[str, Any]:
        return {
            "model": MODEL,
            "firmware": FIRMWARE,
            "simulator": f"mini-sequencer {__version__}",
            "profile": self.profile.name,
        }

    def measure(self, channel: str) -> tuple[str | None, dict[str, Any] | None]:
        """Return ``(fault, result)``; ``fault`` names a transport fault to inject."""
        with self._lock:
            profile = self.profile
            if profile.fault is not None and channel == profile.fault_channel:
                return profile.fault, None
            unit, low, high = NOMINAL_RANGES[channel]
            low, high = profile.ranges.get(channel, (low, high))
            value = round(self._rng.uniform(low, high), 4)
        return None, {"channel": channel, "value": value, "unit": unit}


class _Handler(socketserver.BaseRequestHandler):
    server: _Server

    def handle(self) -> None:
        sock: socket.socket = self.request
        decoder = FrameDecoder(MAX_MESSAGE_BYTES)
        while True:
            try:
                chunk = sock.recv(4096)
            except OSError:
                return
            if not chunk:
                return
            try:
                frames = decoder.feed(chunk)
            except FrameTooLargeError as exc:
                # Framing is lost; report once and drop the connection.
                self._send(make_error(None, "frame_too_large", str(exc)))
                self._close_gracefully(sock)
                return
            for frame in frames:
                if not self._handle_frame(frame):
                    return

    def _handle_frame(self, frame: bytes) -> bool:
        """Answer one request. Return False to close the connection."""
        try:
            request = decode_message(frame)
        except MalformedMessageError as exc:
            return self._send(make_error(None, "malformed_request", str(exc)))
        request_id = request.get("id")
        if not isinstance(request_id, str):
            return self._send(make_error(None, "invalid_request", "'id' must be a string"))
        if request.get("v") != PROTOCOL_VERSION:
            return self._send(
                make_error(request_id, "unsupported_version", f"expected v={PROTOCOL_VERSION}")
            )
        args = request.get("args", {})
        if not isinstance(args, dict):
            return self._send(make_error(request_id, "invalid_request", "'args' must be an object"))

        state = self.server.state
        command = request.get("cmd")
        if command == "identify":
            return self._send(make_result(request_id, state.identity()))
        if command == "reset":
            state.reset()
            return self._send(make_result(request_id, {}))
        if command == "measure":
            channel = args.get("channel")
            if channel not in NOMINAL_RANGES:
                return self._send(
                    make_error(
                        request_id,
                        "invalid_argument",
                        f"unknown channel {channel!r} (supported: {', '.join(NOMINAL_RANGES)})",
                    )
                )
            fault, result = state.measure(channel)
            if fault == "timeout":
                return True  # stay silent; the client's deadline must expire
            if fault == "malformed":
                return self._send_raw(b'{"v": 1, "id": ' + request_id.encode() + b", oops\n")
            return self._send(make_result(request_id, result))
        return self._send(make_error(request_id, "unknown_command", f"unknown command {command!r}"))

    @staticmethod
    def _close_gracefully(sock: socket.socket) -> None:
        """Half-close, then briefly discard unread input.

        Closing with unread data makes the OS send a reset, which can destroy
        the error reply before the peer reads it.
        """
        try:
            sock.shutdown(socket.SHUT_WR)
            sock.settimeout(DRAIN_TIMEOUT_S)
            drained = 0
            while drained < DRAIN_LIMIT_BYTES:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                drained += len(chunk)
        except OSError:
            pass

    def _send(self, message: dict[str, Any]) -> bool:
        return self._send_raw(encode_message(message))

    def _send_raw(self, data: bytes) -> bool:
        try:
            self.request.sendall(data)
        except OSError:
            return False
        return True


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False

    def __init__(self, address: tuple[str, int], state: DeviceState) -> None:
        self.state = state
        super().__init__(address, _Handler)


class DeviceSimulator:
    """Run the simulated device on a TCP port (``port=0`` picks a free one)."""

    def __init__(
        self, host: str = "127.0.0.1", port: int = 0, profile: str = "nominal", seed: int = 0
    ) -> None:
        self.state = DeviceState(profile, seed)
        self._server = _Server((host, port), self.state)
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def configure(self, profile: str, seed: int) -> None:
        """Switch profile and seed; takes effect immediately for all connections."""
        self.state.configure(profile, seed)

    def serve_forever(self) -> None:
        self._server.serve_forever(poll_interval=0.1)

    def start(self) -> DeviceSimulator:
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join()
            self._thread = None
        self._server.server_close()

    def __enter__(self) -> DeviceSimulator:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
