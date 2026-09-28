# Device protocol

Version 1. Implemented in `src/mini_sequencer/protocol.py` (framing),
`client.py` (client), and `simulator.py` (device).

## Transport and framing

- TCP. The simulator binds to `127.0.0.1` by default and warns when bound to a
  non-loopback address.
- Each message is one UTF-8 JSON **object** followed by a newline (`\n`).
- A message is at most **65,536 bytes**, not counting the newline.
- `NaN`, `Infinity`, and `-Infinity` are not valid JSON and are rejected.
- A connection carries any number of request/response pairs, strictly in order:
  the client sends one request and waits for its reply before sending the next.

Receivers must handle a message split across several reads and several messages
in one read. Both sides reject a frame as soon as the buffered bytes exceed the
limit without a newline, so a peer cannot make the other buffer grow without
bound.

## Requests

```json
{"v": 1, "id": "3", "cmd": "measure", "args": {"channel": "voltage"}}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `v` | integer | Protocol version, must be `1` |
| `id` | string | Chosen by the client; echoed in the reply |
| `cmd` | string | `identify`, `reset`, or `measure` |
| `args` | object | Command arguments; may be omitted or `{}` |

## Responses

Success:

```json
{"v": 1, "id": "3", "ok": true, "result": {"channel": "voltage", "value": 5.0279, "unit": "V"}}
```

Error:

```json
{"v": 1, "id": "3", "ok": false, "error": {"code": "invalid_argument", "message": "unknown channel 'pressure' (supported: voltage, current, temperature)"}}
```

If the request could not be parsed, `id` is `null`.

## Commands

### `identify`

Returns the device identity. `model` and `firmware` are required strings; other
fields are informational.

```json
→ {"v": 1, "id": "1", "cmd": "identify", "args": {}}
← {"v": 1, "id": "1", "ok": true, "result": {"model": "MTS-SIM-1", "firmware": "1.0.0", "simulator": "mini-sequencer 0.1.0", "profile": "nominal"}}
```

### `reset`

Restores the device to its initial state. On the simulator this rewinds the
measurement generator to its seed, so the same measurements follow.

```json
→ {"v": 1, "id": "2", "cmd": "reset", "args": {}}
← {"v": 1, "id": "2", "ok": true, "result": {}}
```

### `measure`

`args.channel` is one of `voltage` (V), `current` (mA), or `temperature` (C).
The result has a finite numeric `value` (not a boolean) and a non-empty `unit`.

```json
→ {"v": 1, "id": "3", "cmd": "measure", "args": {"channel": "current"}}
← {"v": 1, "id": "3", "ok": true, "result": {"channel": "current", "value": 61.0004, "unit": "mA"}}
```

## Error codes (device → client)

| Code | When |
| --- | --- |
| `malformed_request` | Frame is not a UTF-8 JSON object (`id` is `null`) |
| `invalid_request` | `id` is not a string, or `args` is not an object |
| `unsupported_version` | `v` is not `1` |
| `unknown_command` | `cmd` is not a supported command |
| `invalid_argument` | Unknown or missing `channel` |
| `frame_too_large` | Request exceeded the size limit; the device then closes the connection |

## Client rules

- Each request has one deadline (`device.timeout_s`) covering both send and
  receive. Bytes that trickle in without completing a frame do not extend it.
- The reply's `v` must be `1` and its `id` must equal the request's `id`. Any
  other message, including an unexpected extra one, is a protocol error.
- A timeout, disconnect, malformed frame, oversized frame, or invalid reply
  closes the connection. The client never reuses a connection in an unknown
  state and never retries.
- An `ok: false` reply is a well-formed exchange: it raises a device error but
  leaves the connection usable.

Client error categories, as stored in results: `connection`, `timeout`,
`protocol`, `device`.

## Simulator profiles

Profiles are chosen with `mini-seq simulator --profile NAME` or by the demo
through the Python API. They cannot be changed over the socket.

| Profile | Behavior |
| --- | --- |
| `nominal` | voltage 4.90–5.10 V, current 60–100 mA, temperature 22–35 C |
| `undervoltage` | voltage 4.40–4.70 V |
| `overcurrent` | current 130–180 mA |
| `overtemperature` | temperature 50–60 C |
| `timeout` | never answers a `temperature` measurement |
| `malformed` | answers a `current` measurement with invalid JSON |
