from __future__ import annotations

import argparse
import json
from pathlib import Path

from .models import InvalidTransition, TaskRequest
from .service import Broker


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="recollect")
    p.add_argument("--home", type=Path, default=Path(".recollect"))
    sub = p.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--task", required=True)
    create.add_argument("--workspace", required=True)
    create.add_argument("--mode", default="read_only")
    create.add_argument("--profile", default="mock")
    create.add_argument("--timeout", type=int, default=1800)
    for name in ("start", "status", "complete", "collect", "cancel", "timeout", "reconcile"):
        cmd = sub.add_parser(name)
        cmd.add_argument("task_id")
        if name == "complete":
            cmd.add_argument("--summary", required=True)
        if name in {"cancel", "timeout"}:
            cmd.add_argument("--reason", default="Cancelled or timed out by caller")
    verify = sub.add_parser("verify")
    verify.add_argument("task_id")
    verify.add_argument(
        "--command", dest="commands", action="append", required=True,
        help="JSON-encoded argv array, e.g. '[\"pytest\", \"-q\"]'; may be repeated",
    )
    sub.add_parser("list")
    sub.add_parser("reconcile-all")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    broker = Broker(args.home)
    try:
        if args.command == "create":
            output = broker.create(TaskRequest(args.task, args.workspace, args.mode, args.profile, args.timeout)).json()
        elif args.command == "start":
            output = broker.start(args.task_id).json()
        elif args.command == "complete":
            output = broker.complete(args.task_id, args.summary).json()
        elif args.command == "collect":
            output = broker.collect(args.task_id).json()
        elif args.command == "cancel":
            output = broker.cancel(args.task_id, args.reason).json()
        elif args.command == "timeout":
            output = broker.timeout(args.task_id, args.reason).json()
        elif args.command == "verify":
            output = broker.verify(args.task_id, [json.loads(command) for command in args.commands])
        elif args.command == "status":
            output = broker.status(args.task_id)
        elif args.command == "reconcile":
            output = broker.reconcile(args.task_id).json()
        elif args.command == "reconcile-all":
            output = [record.json() for record in broker.reconcile_pending()]
        else:
            output = [record.json() for record in broker.store.list()]
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    except (KeyError, ValueError, InvalidTransition) as error:
        print(json.dumps({"error": {"code": type(error).__name__, "message": str(error)}}, sort_keys=True))
        return 2
    finally:
        broker.close()
