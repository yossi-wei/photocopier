"""Command line interface.

Phase 1 ships `doctor`, `ingest`, and a minimal `status`. `process` and `flush` arrive
in phases 2 and 3.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from . import config as config_module
from .errors import GuardError, PhotocopierError
from .guards import (
    Check,
    check_mount_live,
    check_same_filesystem,
    check_spool_not_in_sync_root,
    human_bytes,
)
from .ingest import ingest, render
from .ledger import Ledger, State
from .rclone import Rclone
from .spool import Spool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="photocopier",
        description="Move phone camera uploads from OneDrive into a NAS photo library.",
    )
    parser.add_argument("--version", action="version", version=f"photocopier {__version__}")
    parser.add_argument("-c", "--config", metavar="PATH", help="path to config.toml")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check the environment and configuration")

    ingest_parser = sub.add_parser("ingest", help="download new files from OneDrive into the spool")
    ingest_parser.add_argument(
        "--dry-run", action="store_true", help="report what would be ingested"
    )
    ingest_parser.add_argument("--source", metavar="ID", help="limit to one configured source")

    sub.add_parser("status", help="show spool and ledger state")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            return cmd_doctor(args)
        if args.command == "ingest":
            return cmd_ingest(args)
        if args.command == "status":
            return cmd_status(args)
    except PhotocopierError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    parser.error(f"unknown command {args.command!r}")
    return 2


# -- commands ------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    checks: list[Check] = []

    try:
        cfg = config_module.load(args.config)
    except PhotocopierError as exc:
        print(Check("config", False, str(exc)).render())
        return 1
    checks.append(Check("config", True, f"loaded {cfg.source_file}"))

    spool = Spool.from_config(cfg.spool)

    try:
        check_spool_not_in_sync_root(spool.root)
        spool.ensure()
        check_same_filesystem(spool.incoming, spool.outbox)
        checks.append(
            Check("spool", True, f"{spool.root} ({human_bytes(spool.usage_bytes())} of "
                                 f"{human_bytes(spool.cap_bytes)} used)")
        )
    except GuardError as exc:
        checks.append(Check("spool", False, str(exc)))

    rclone = Rclone(cfg.rclone)
    if not rclone.available():
        checks.append(Check("rclone", False, f"binary {cfg.rclone.binary!r} not found on PATH"))
    else:
        try:
            version = rclone.version()
            remotes = rclone.remotes()
            checks.append(Check("rclone", True, version))
            if cfg.rclone.remote in remotes:
                checks.append(Check("remote", True, f"{cfg.rclone.remote!r} is configured"))
            else:
                known = ", ".join(remotes) or "none"
                checks.append(
                    Check("remote", False, f"{cfg.rclone.remote!r} not found. Configured: {known}")
                )
        except PhotocopierError as exc:
            checks.append(Check("rclone", False, str(exc)))

    checks.append(
        Check("sources", True, ", ".join(f"{s.id} -> {s.path!r}" for s in cfg.sources))
    )

    # The destination is not needed to ingest, so an unmounted share is a warning here
    # rather than a failure. Delivery (phase 3) treats it as fatal.
    try:
        check_mount_live(cfg.destination.mount_point, cfg.destination.mount_marker)
        checks.append(Check("destination", True, f"{cfg.destination.mount_point} mounted"))
    except GuardError as exc:
        checks.append(Check("destination", False, str(exc), fatal=False))

    for check in checks:
        print(check.render())

    failed = [c for c in checks if not c.ok and c.fatal]
    if failed:
        print(f"\n{len(failed)} check(s) failed", file=sys.stderr)
        return 1
    print("\nall required checks passed")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    cfg = config_module.load(args.config)
    spool = Spool.from_config(cfg.spool)

    check_spool_not_in_sync_root(spool.root)
    spool.ensure()
    check_same_filesystem(spool.incoming, spool.outbox)

    rclone = Rclone(cfg.rclone)
    with Ledger(spool.ledger_path) as ledger:
        result = ingest(cfg, rclone, ledger, spool, only=args.source, dry_run=args.dry_run)
        print(render(result, spool))

    if result.total_failed:
        return 1
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = config_module.load(args.config)
    spool = Spool.from_config(cfg.spool)

    print(f"spool     {spool.root}")
    if not spool.ledger_path.exists():
        print("ledger    not created yet — run `photocopier ingest`")
        return 0

    used = spool.usage_bytes()
    pct = (used / spool.cap_bytes * 100) if spool.cap_bytes else 0
    print(f"          {human_bytes(used)} of {human_bytes(spool.cap_bytes)} used ({pct:.1f}%)")

    with Ledger(spool.ledger_path) as ledger:
        counts = ledger.counts()
        print("\nledger")
        for state in State:
            print(f"  {state.value:<12} {counts.get(state.value, 0)}")

        awaiting = ledger.count_in_state(State.INGESTED)
        if awaiting:
            size = ledger.total_bytes_in_state(State.INGESTED)
            print(f"\n{awaiting} file(s) in the spool awaiting processing ({human_bytes(size)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
