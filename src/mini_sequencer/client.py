"""TCP client for devices speaking the newline-delimited JSON protocol.

Each request has one deadline covering send and receive. Any transport or
protocol failure closes the connection: after a timeout or garbled reply the
device state is uncertain, so the client never reuses the socket or retries.
"""

from __future__ import annotations

import itertools
import math
import socket
import time
from collections import deque
from typing import Any, NoReturn

from mini_sequencer.models import Measurement
from mini_sequencer.protocol import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    FrameDecoder,
    ProtocolError,
    decode_message,
    encode_message,
    make_request,
)

RECV_CHUNK_BYTES = 4096


class DeviceError(Exception):
    """Base class for device communication failures."""

    category = "device"

    def __init__(self, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


class DeviceConnectionError(DeviceError):
    category = "connection"


class DeviceTimeoutError(DeviceError):
    category = "timeout"


class DeviceProtocolError(DeviceError):
    category = "protocol"


class DeviceCommandError(DeviceError):
    """The device understood the request and reported an error."""

    category = "device"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"device error {code}: {message}", {"code": code})
        self.code = code


class DeviceClient:
    def __init__(
        self,
        host: str,
        port: int,
        timeout_s: float,
        max_message_bytes: int = MAX_MESSAGE_BYTES,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self.max_message_bytes = max_message_bytes
        self._sock: socket.socket | None = None
        self._decoder = FrameDecoder(max_message_bytes)
        self._inbox: deque[bytes] = deque()
        self._ids = itertools.count(1)

    def __enter__(self) -> DeviceClient:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def connect(self) -> None:
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
        except TimeoutError as exc:
            raise DeviceTimeoutError(
                f"timed out connecting to {self.host}:{self.port} after {self.timeout_s:g} s"
            ) from exc
        except OSError as exc:
            raise DeviceConnectionError(
                f"cannot connect to {self.host}:{self.port}: {exc.strerror or exc}"
            ) from exc
        self._decoder = FrameDecoder(self.max_message_bytes)
        self._inbox.clear()

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    # -- commands ---------------------------------------------------------

    def identify(self) -> dict[str, Any]:
        result = self.request("identify")
        for key in ("model", "firmware"):
            if not isinstance(result.get(key), str):
                self._fail(DeviceProtocolError(f"identify reply is missing string field {key!r}"))
        return result

    def reset(self) -> None:
        self.request("reset")

    def measure(self, channel: str) -> Measurement:
        result = self.request("measure", {"channel": channel})
        value = result.get("value")
        unit = result.get("unit")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            self._fail(
                DeviceProtocolError(
                    f"measure reply has invalid value {value!r}", {"value": repr(value)}
                )
            )
        if not math.isfinite(value):
            self._fail(DeviceProtocolError(f"measure reply has non-finite value {value!r}"))
        if not isinstance(unit, str) or not unit:
            self._fail(DeviceProtocolError(f"measure reply has invalid unit {unit!r}"))
        return Measurement(value=float(value), unit=unit)

    # -- transport --------------------------------------------------------

    def request(self, command: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one request and return its ``result`` object."""
        if self._sock is None:
            raise DeviceConnectionError("not connected")
        request_id = str(next(self._ids))
        deadline = time.monotonic() + self.timeout_s
        try:
            payload = encode_message(
                make_request(request_id, command, args), self.max_message_bytes
            )
        except ProtocolError as exc:
            raise DeviceProtocolError(str(exc)) from exc
        try:
            self._sock.settimeout(self.timeout_s)
            self._sock.sendall(payload)
        except TimeoutError as exc:
            self._fail(DeviceTimeoutError(f"timed out sending {command!r}"), exc)
        except OSError as exc:
            self._fail(DeviceConnectionError(f"send failed: {exc.strerror or exc}"), exc)

        frame = self._next_frame(command, deadline)
        try:
            reply = decode_message(frame)
        except ProtocolError as exc:
            self._fail(DeviceProtocolError(str(exc)), exc)
        return self._check_reply(reply, request_id, command)

    def _next_frame(self, command: str, deadline: float) -> bytes:
        assert self._sock is not None
        while not self._inbox:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._fail(
                    DeviceTimeoutError(
                        f"no reply to {command!r} within {self.timeout_s:g} s",
                        {"timeout_s": self.timeout_s},
                    )
                )
            try:
                self._sock.settimeout(remaining)
                chunk = self._sock.recv(RECV_CHUNK_BYTES)
            except TimeoutError as exc:
                self._fail(
                    DeviceTimeoutError(
                        f"no reply to {command!r} within {self.timeout_s:g} s",
                        {"timeout_s": self.timeout_s},
                    ),
                    exc,
                )
            except OSError as exc:
                self._fail(DeviceConnectionError(f"receive failed: {exc.strerror or exc}"), exc)
            if not chunk:
                self._fail(
                    DeviceConnectionError(f"device closed the connection during {command!r}")
                )
            try:
                self._inbox.extend(self._decoder.feed(chunk))
            except ProtocolError as exc:
                self._fail(DeviceProtocolError(str(exc)), exc)
        return self._inbox.popleft()

    def _check_reply(self, reply: dict[str, Any], request_id: str, command: str) -> dict:
        if reply.get("v") != PROTOCOL_VERSION:
            self._fail(DeviceProtocolError(f"unsupported protocol version {reply.get('v')!r}"))
        if reply.get("id") != request_id:
            self._fail(
                DeviceProtocolError(
                    f"reply id {reply.get('id')!r} does not match request id {request_id!r}",
                    {"expected_id": request_id, "received_id": reply.get("id")},
                )
            )
        ok = reply.get("ok")
        if ok is True:
            result = reply.get("result")
            if not isinstance(result, dict):
                self._fail(DeviceProtocolError(f"reply to {command!r} has no result object"))
            return result
        if ok is False:
            error = reply.get("error")
            if not isinstance(error, dict):
                self._fail(DeviceProtocolError(f"error reply to {command!r} has no error object"))
            code = error.get("code")
            message = error.get("message")
            # The exchange itself was well-formed, so the connection stays usable.
            raise DeviceCommandError(
                code if isinstance(code, str) else "unknown",
                message if isinstance(message, str) else "",
            )
        self._fail(DeviceProtocolError(f"reply to {command!r} has invalid 'ok' field {ok!r}"))

    def _fail(self, error: DeviceError, cause: BaseException | None = None) -> NoReturn:
        """Close the connection and raise ``error``; the socket is not reused."""
        self.close()
        raise error from cause
