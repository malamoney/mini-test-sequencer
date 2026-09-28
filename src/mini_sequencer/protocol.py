"""Newline-delimited JSON framing shared by the device client and simulator.

Every message is one UTF-8 JSON object followed by ``\\n``. A frame may be at
most :data:`MAX_MESSAGE_BYTES` bytes, not counting the newline. See
``docs/protocol.md`` for the message shapes.
"""

from __future__ import annotations

import json
from typing import Any

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 64 * 1024
DELIMITER = b"\n"


class ProtocolError(Exception):
    """A message could not be framed, decoded, or understood."""


class FrameTooLargeError(ProtocolError):
    """A frame exceeded :data:`MAX_MESSAGE_BYTES`."""


class MalformedMessageError(ProtocolError):
    """A frame was not a UTF-8 JSON object."""


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name} is not allowed")


def encode_message(message: dict[str, Any], max_bytes: int = MAX_MESSAGE_BYTES) -> bytes:
    """Serialize a message as one frame, including the trailing newline."""
    body = json.dumps(message, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(body) > max_bytes:
        raise FrameTooLargeError(f"message is {len(body)} bytes; limit is {max_bytes}")
    return body + DELIMITER


def decode_message(frame: bytes) -> dict[str, Any]:
    """Decode one frame (without its newline) into a JSON object."""
    try:
        message = json.loads(frame.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise MalformedMessageError(f"malformed message: {exc}") from None
    if not isinstance(message, dict):
        raise MalformedMessageError(
            f"malformed message: expected a JSON object, got {type(message).__name__}"
        )
    return message


class FrameDecoder:
    """Reassemble frames from a byte stream.

    Handles partial reads (a frame split across ``recv`` calls) and coalesced
    reads (several frames in one ``recv``). Raises :class:`FrameTooLargeError`
    as soon as buffered data proves a frame is over the limit, so a peer that
    never sends a newline cannot grow the buffer without bound.
    """

    def __init__(self, max_bytes: int = MAX_MESSAGE_BYTES) -> None:
        self.max_bytes = max_bytes
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer.extend(data)
        frames: list[bytes] = []
        while True:
            index = self._buffer.find(DELIMITER)
            if index < 0:
                if len(self._buffer) > self.max_bytes:
                    raise FrameTooLargeError(
                        f"frame exceeds {self.max_bytes} bytes without a newline"
                    )
                return frames
            if index > self.max_bytes:
                raise FrameTooLargeError(f"frame is {index} bytes; limit is {self.max_bytes}")
            frames.append(bytes(self._buffer[:index]))
            del self._buffer[: index + 1]

    @property
    def pending(self) -> int:
        """Number of buffered bytes not yet forming a complete frame."""
        return len(self._buffer)


def make_request(request_id: str, command: str, args: dict[str, Any] | None = None) -> dict:
    return {"v": PROTOCOL_VERSION, "id": request_id, "cmd": command, "args": args or {}}


def make_result(request_id: str | None, result: dict[str, Any]) -> dict:
    return {"v": PROTOCOL_VERSION, "id": request_id, "ok": True, "result": result}


def make_error(request_id: str | None, code: str, message: str) -> dict:
    return {
        "v": PROTOCOL_VERSION,
        "id": request_id,
        "ok": False,
        "error": {"code": code, "message": message},
    }
