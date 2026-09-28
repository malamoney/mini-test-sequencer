from __future__ import annotations

import pytest

from mini_sequencer.protocol import (
    FrameDecoder,
    FrameTooLargeError,
    MalformedMessageError,
    decode_message,
    encode_message,
    make_request,
)


def test_round_trip():
    request = make_request("7", "measure", {"channel": "voltage"})
    frame = encode_message(request)
    assert frame.endswith(b"\n") and frame.count(b"\n") == 1
    assert decode_message(frame[:-1]) == request


def test_partial_reads_are_reassembled():
    frame = encode_message({"a": 1, "text": "héllo"})
    decoder = FrameDecoder()
    frames = []
    for i in range(len(frame)):
        frames += decoder.feed(frame[i : i + 1])  # one byte at a time, splitting UTF-8
    assert [decode_message(f) for f in frames] == [{"a": 1, "text": "héllo"}]
    assert decoder.pending == 0


def test_coalesced_frames_are_split():
    data = encode_message({"n": 1}) + encode_message({"n": 2}) + b'{"n": 3'
    decoder = FrameDecoder()
    assert [decode_message(f) for f in decoder.feed(data)] == [{"n": 1}, {"n": 2}]
    assert decoder.pending == len(b'{"n": 3')
    assert [decode_message(f) for f in decoder.feed(b"}\n")] == [{"n": 3}]


def test_oversized_frame_without_newline_rejected_early():
    decoder = FrameDecoder(max_bytes=16)
    decoder.feed(b"x" * 16)
    with pytest.raises(FrameTooLargeError):
        decoder.feed(b"x")


def test_oversized_complete_frame_rejected():
    with pytest.raises(FrameTooLargeError):
        FrameDecoder(max_bytes=16).feed(b"x" * 17 + b"\n")


def test_frame_at_limit_accepted():
    assert FrameDecoder(max_bytes=16).feed(b"x" * 16 + b"\n") == [b"x" * 16]


def test_encode_rejects_oversized_and_non_finite():
    with pytest.raises(FrameTooLargeError):
        encode_message({"blob": "x" * 100}, max_bytes=50)
    with pytest.raises(ValueError):
        encode_message({"value": float("nan")})


@pytest.mark.parametrize(
    "frame", [b"{not json", b"[1, 2]", b'"str"', b"\xff\xfe", b'{"value": NaN}']
)
def test_malformed_frames(frame):
    with pytest.raises(MalformedMessageError):
        decode_message(frame)
