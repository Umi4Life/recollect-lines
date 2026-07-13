from __future__ import annotations

import argparse
import json
from pathlib import Path

from .models import TaskRequest
from .service import Broker


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sidecar")
    p.add_argument("--home", type=Path, default=Path(".sidecar"))
    sub = p.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--task", required=True)
    create.add_argument("--workspace", required=True)
    create.add_argument("--mode", default="read_only")
    create.add_argument("--profile", default="mock")
    create.add_argument("--timeout", type=int, default=1800)
    for name in ("start", "status", "complete", "cancel"):
        cmd = sub.add_parser(name)
        cmd.add_argument("task_id")
        if name == "complete":
            cmd.add_argument("--summary", required=True)
        if name == "cancel":
            cmd.add_argument("--reason", default="Cancelled by caller")
    sub.add_parser("list")
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
        elif args.command == "cancel":
            output = broker.cancel(args.task_id, args.reason).json()
        elif args.command == "status":
            output = broker.status(args.task_id)
        else:
            output = [record.json() for record in broker.store.list()]
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    finally:
        broker.close()
