"""Command-line interface: ``mini-seq <command>``.

Exit codes for ``run``: 0 pass, 1 limit failure, 2 configuration or execution
error, 130 user interruption. Other commands return 0 on success and 2 on error.
"""

from __future__ import annotations

import argparse
import ipaddress
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from mini_sequencer import __version__
from mini_sequencer.config import ConfigError, config_hash, load_config
from mini_sequencer.demo import default_config, run_demo
from mini_sequencer.html_report import render_html
from mini_sequencer.models import RunStatus
from mini_sequencer.reporting import (
    HASH_DISPLAY_CHARS,
    format_run_list,
    format_run_report,
    format_summary,
    summary_to_json,
)
from mini_sequencer.sequencer import run_sequence, utc_now
from mini_sequencer.simulator import PROFILES, DeviceSimulator
from mini_sequencer.storage import SQLiteResultStore, StorageError, SummaryFilter

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_ERROR = 2
EXIT_INTERRUPTED = 130

_RUN_EXIT_CODES = {
    RunStatus.PASS: EXIT_PASS,
    RunStatus.FAIL: EXIT_FAIL,
    RunStatus.ERROR: EXIT_ERROR,
    RunStatus.ABORTED: EXIT_INTERRUPTED,
}

MAX_SERIAL_LENGTH = 64


def _serial(value: str) -> str:
    value = value.strip()
    if not value or len(value) > MAX_SERIAL_LENGTH or not value.isprintable():
        raise argparse.ArgumentTypeError(
            f"serial must be 1-{MAX_SERIAL_LENGTH} printable characters"
        )
    return value


def _port(value: str, lowest: int = 1) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid port {value!r}") from None
    if not lowest <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port must be {lowest}-65535")
    return port


def _listen_port(value: str) -> int:
    """Like :func:`_port`, but 0 asks the OS for a free port."""
    return _port(value, lowest=0)


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid number {value!r}") from None
    if not number > 0 or number == float("inf"):
        raise argparse.ArgumentTypeError("must be a positive, finite number")
    return number


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid integer {value!r}") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _timestamp(value: str) -> str:
    """Parse an ISO 8601 date/time (naive values are UTC) into the stored format."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid timestamp {value!r}; use ISO 8601, e.g. 2026-01-31 or 2026-01-31T14:00:00Z"
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mini-seq",
        description="Run YAML-defined test sequences against a TCP device and record results.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("validate", help="validate a sequence configuration file")
    p.add_argument("config", type=Path, help="YAML sequence file")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("simulator", help="run the simulated device until interrupted")
    p.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    p.add_argument(
        "--port", type=_listen_port, default=9000, help="TCP port, 0 for any free (default: 9000)"
    )
    p.add_argument("--seed", type=int, default=0, help="measurement seed (default: 0)")
    p.add_argument(
        "--profile",
        choices=sorted(PROFILES),
        default="nominal",
        help="device behavior (default: nominal)",
    )
    p.set_defaults(func=cmd_simulator)

    p = sub.add_parser("run", help="run a sequence against one unit")
    p.add_argument("--serial", type=_serial, required=True, help="unit serial number")
    p.add_argument("--config", type=Path, required=True, help="YAML sequence file")
    p.add_argument("--db", type=Path, required=True, help="SQLite result database")
    p.add_argument("--host", help="override device.host")
    p.add_argument("--port", type=_port, help="override device.port")
    p.add_argument("--timeout", type=_positive_float, help="override device.timeout_s")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("demo", help="run a seeded batch against a private simulator")
    p.add_argument("--units", type=_positive_int, default=100, help="units to test (default: 100)")
    p.add_argument("--seed", type=int, default=42, help="batch seed (default: 42)")
    p.add_argument("--db", type=Path, required=True, help="SQLite result database")
    p.add_argument("--config", type=Path, help="YAML sequence (default: bundled example)")
    p.add_argument(
        "--timeout",
        type=_positive_float,
        default=0.5,
        help="device timeout in seconds, keeps timeout faults quick (default: 0.5)",
    )
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("summary", help="yield, failure Pareto, and error summary")
    p.add_argument("--db", type=Path, required=True, help="SQLite result database")
    _add_filter_args(p)
    p.add_argument("--json", action="store_true", help="print JSON instead of text")
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser(
        "html",
        help="write a self-contained HTML results page",
        description="Write the summary, charts, and run log to one HTML file that works offline.",
    )
    p.add_argument("--db", type=Path, required=True, help="SQLite result database")
    p.add_argument("--out", type=Path, required=True, help="HTML file to write (replaced)")
    _add_filter_args(p)
    p.set_defaults(func=cmd_html)

    p = sub.add_parser("report", help="show one stored run")
    p.add_argument("run_id", help="run ID (unique prefix allowed)")
    p.add_argument("--db", type=Path, required=True, help="SQLite result database")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("runs", help="list stored runs, newest first")
    p.add_argument("--db", type=Path, required=True, help="SQLite result database")
    p.add_argument("--status", choices=[s.value for s in RunStatus], help="only this status")
    p.add_argument("--limit", type=_positive_int, default=20, help="rows to show (default: 20)")
    p.set_defaults(func=cmd_runs)

    p = sub.add_parser(
        "abort",
        help="mark an unfinished RUNNING run as ABORTED",
        description="Recover a run left RUNNING by a killed process. Only RUNNING runs change.",
    )
    p.add_argument("run_id", help="run ID (unique prefix allowed)")
    p.add_argument("--db", type=Path, required=True, help="SQLite result database")
    p.add_argument(
        "--reason", default="marked aborted by operator", help="note stored with the run"
    )
    p.set_defaults(func=cmd_abort)
    return parser


def _add_filter_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--since", type=_timestamp, help="include runs started at or after (UTC)")
    p.add_argument("--until", type=_timestamp, help="include runs started before (UTC)")
    p.add_argument("--sequence", help="only this sequence name")
    p.add_argument("--config-hash", help="only this configuration hash (prefix allowed)")


def _summary_filter(args: argparse.Namespace) -> SummaryFilter:
    return SummaryFilter(
        since=args.since,
        until=args.until,
        sequence_name=args.sequence,
        config_hash=args.config_hash,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    except (ConfigError, StorageError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


def _require_db(path: Path) -> None:
    if not path.exists():
        raise StorageError(f"result database {path} does not exist")


def cmd_validate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    print(
        f"{args.config}: OK - sequence {config.name!r}, {len(config.steps)} steps, "
        f"config {config_hash(config)[:HASH_DISPLAY_CHARS]}"
    )
    return EXIT_PASS


def cmd_simulator(args: argparse.Namespace) -> int:
    try:
        loopback = ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        loopback = args.host == "localhost"
    if not loopback:
        print(f"warning: binding to non-loopback address {args.host}", file=sys.stderr)
    try:
        simulator = DeviceSimulator(args.host, args.port, profile=args.profile, seed=args.seed)
    except OSError as exc:
        print(f"error: cannot listen on {args.host}:{args.port}: {exc.strerror}", file=sys.stderr)
        return EXIT_ERROR
    host, port = simulator.address
    print(
        f"simulator listening on {host}:{port} (profile {args.profile}, seed {args.seed}); "
        "Ctrl+C to stop",
        flush=True,
    )
    try:
        simulator.serve_forever()
    finally:
        simulator.stop()
    return EXIT_PASS


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config).with_device(args.host, args.port, args.timeout)
    with SQLiteResultStore(args.db) as store:
        try:
            run = run_sequence(config, args.serial, store)
        except KeyboardInterrupt:
            print("interrupted; run recorded as ABORTED", file=sys.stderr)
            return EXIT_INTERRUPTED
    print(format_run_report(run))
    return _RUN_EXIT_CODES[run.status]


def cmd_demo(args: argparse.Namespace) -> int:
    config = load_config(args.config) if args.config else default_config()
    started_at = utc_now()

    def on_unit(unit, run) -> None:
        if run.status is not RunStatus.PASS:
            print(f"  {unit.serial}  profile {unit.profile:<15} {run.status.value}")

    print(f"Running {args.units} units (seed {args.seed}); non-passing units:")
    with SQLiteResultStore(args.db) as store:
        runs = run_demo(store, args.units, args.seed, config, args.timeout, on_unit=on_unit)
        flt = SummaryFilter(since=started_at, config_hash=runs[0].config_hash)
        print()
        print(format_summary(store.summarize(flt), flt, str(args.db)))
    return EXIT_PASS


def cmd_summary(args: argparse.Namespace) -> int:
    _require_db(args.db)
    flt = _summary_filter(args)
    with SQLiteResultStore(args.db) as store:
        groups = store.summarize(flt)
    print(summary_to_json(groups) if args.json else format_summary(groups, flt, str(args.db)))
    return EXIT_PASS


def cmd_html(args: argparse.Namespace) -> int:
    _require_db(args.db)
    flt = _summary_filter(args)
    with SQLiteResultStore(args.db) as store:
        groups = store.summarize(flt)
        runs = store.get_runs(flt)
    page = render_html(groups, runs, flt, source=args.db.name, generated_at=utc_now())
    try:
        args.out.write_text(page, encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot write {args.out}: {exc.strerror or exc}", file=sys.stderr)
        return EXIT_ERROR
    print(f"wrote {args.out} ({len(runs)} runs)")
    return EXIT_PASS


def cmd_report(args: argparse.Namespace) -> int:
    _require_db(args.db)
    with SQLiteResultStore(args.db) as store:
        run = store.get_run(args.run_id)
    if run is None:
        raise StorageError(f"no run matches {args.run_id!r}")
    print(format_run_report(run))
    return EXIT_PASS


def cmd_runs(args: argparse.Namespace) -> int:
    _require_db(args.db)
    status = RunStatus(args.status) if args.status else None
    with SQLiteResultStore(args.db) as store:
        runs = store.list_runs(status=status, limit=args.limit)
    print(format_run_list(runs))
    return EXIT_PASS


def cmd_abort(args: argparse.Namespace) -> int:
    _require_db(args.db)
    with SQLiteResultStore(args.db) as store:
        run = store.abort_stale_run(args.run_id, utc_now(), args.reason)
    print(f"run {run.id} marked ABORTED")
    return EXIT_PASS
