from __future__ import annotations

import argparse
import json
from pathlib import Path

from .claude_code_adapter import ClaudeCodeAdapter
from .codex_adapter import CodexAdapter
from .cursor_adapter import CursorAdapter
from .models import VERIFICATION_POLICIES, InvalidTransition, TaskRequest
from .opencode_adapter import OpenCodeAdapter
from .service import Broker


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="recollect")
    p.add_argument("--home", type=Path, default=Path(".recollect"))
    p.add_argument(
        "--opencode-command", default=None,
        help=(
            "Advanced: override the opencode adapter's command prefix as a JSON array "
            "(e.g. to pin a specific opencode-ai version, or point at a deterministic "
            "stand-in binary for testing). Defaults to the built-in npx opencode-ai invocation."
        ),
    )
    p.add_argument(
        "--claude-command", default=None,
        help=(
            "Advanced: override the Claude Code adapter's command prefix as a JSON array "
            "(e.g. to point at a deterministic stand-in binary for testing). "
            "Defaults to the built-in `claude` CLI invocation."
        ),
    )
    p.add_argument(
        "--codex-command", default=None,
        help=(
            "Advanced: override the Codex adapter's command prefix as a JSON array "
            "(e.g. to point at a deterministic stand-in binary for testing). "
            "Defaults to the built-in `codex` CLI invocation."
        ),
    )
    p.add_argument(
        "--cursor-command", default=None,
        help=(
            "Advanced: override the Cursor adapter's command prefix as a JSON array "
            "(e.g. to point at a deterministic stand-in binary for testing). "
            "Defaults to the built-in `cursor-agent` CLI invocation."
        ),
    )
    p.add_argument(
        "--providers-config", type=Path, default=None,
        help="Path to a JSON provider configuration file (required for openai_compatible profile tasks).",
    )
    sub = p.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--task", required=True)
    create.add_argument("--workspace", required=True)
    create.add_argument("--mode", default="read_only")
    create.add_argument("--profile", default="mock")
    create.add_argument(
        "--provider", default=None,
        help="Named provider from --providers-config (required when --profile openai_compatible).",
    )
    create.add_argument("--timeout", type=int, default=1800)
    create.add_argument("--verification-policy", default="none", choices=VERIFICATION_POLICIES)
    create.add_argument(
        "--verify-command", dest="verify_commands", action="append", default=None,
        help="JSON-encoded argv array run as broker-verified evidence when this task is collected; may be repeated",
    )
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
    opencode_adapter = OpenCodeAdapter(command_prefix=tuple(json.loads(args.opencode_command))) if args.opencode_command else None
    claude_code_adapter = ClaudeCodeAdapter(command_prefix=tuple(json.loads(args.claude_command))) if args.claude_command else None
    codex_adapter = CodexAdapter(command_prefix=tuple(json.loads(args.codex_command))) if args.codex_command else None
    cursor_adapter = CursorAdapter(command_prefix=tuple(json.loads(args.cursor_command))) if args.cursor_command else None
    broker = Broker(
        args.home,
        opencode_adapter=opencode_adapter,
        claude_code_adapter=claude_code_adapter,
        codex_adapter=codex_adapter,
        cursor_adapter=cursor_adapter,
        providers_config=args.providers_config,
    )
    try:
        if args.command == "create":
            request = TaskRequest(args.task, args.workspace, args.mode, args.profile, args.provider, args.timeout, args.verification_policy)
            verify_commands = [json.loads(command) for command in args.verify_commands] if args.verify_commands else None
            output = broker.create(request, verify_commands=verify_commands).json()
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
