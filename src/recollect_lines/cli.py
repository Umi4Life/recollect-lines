from __future__ import annotations

import argparse
import json
from pathlib import Path

from .claude_code_adapter import ClaudeCodeAdapter
from .codex_adapter import CodexAdapter
from .cursor_adapter import CursorAdapter
from .models import VERIFICATION_POLICIES, InvalidTransition, TaskRequest
from .opencode_adapter import OpenCodeAdapter
from .doctor import format_human_report, run_doctor
from .service import Broker


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="recollect-lines")
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
    sub.add_parser("discover")
    select = sub.add_parser("select")
    select.add_argument("--mode", dest="execution_mode", required=True)
    select.add_argument("--allowed-runtime", dest="allowed_runtimes", action="append", default=None)
    select.add_argument("--allowed-provider", dest="allowed_providers", action="append", default=None)
    select.add_argument("--require-runtime-capability", dest="runtime_capabilities", action="append", default=None,
                        help='JSON object fragment like \'{"isolated_worktree": true}\' (repeatable, merged)')
    select.add_argument("--require-provider-capability", dest="provider_capabilities", action="append", default=None,
                        help='JSON object fragment like \'{"chat_completions": true}\' (repeatable, merged)')
    select.add_argument("--include-unavailable", action="store_true", help="Do not exclude unavailable candidates")
    council = sub.add_parser("council")
    council_sub = council.add_subparsers(dest="council_command", required=True)
    council_validate = council_sub.add_parser("validate")
    council_validate.add_argument("--plan", required=True, help="JSON council plan")
    council_execute = council_sub.add_parser("execute")
    council_execute.add_argument("--plan", required=True, help="JSON council plan")
    doctor = sub.add_parser("doctor", help="Offline-safe operational diagnostics")
    doctor.add_argument("--json", action="store_true", help="Emit stable machine-readable JSON")
    doctor.add_argument("--workspace", type=Path, default=None, help="Optional workspace path to validate")
    return p


def _merge_capability_flags(fragments: list[str] | None) -> dict[str, bool] | None:
    if not fragments:
        return None
    merged: dict[str, bool] = {}
    for fragment in fragments:
        parsed = json.loads(fragment)
        if not isinstance(parsed, dict):
            raise ValueError("capability requirements must be JSON objects")
        merged.update(parsed)
    return merged


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    opencode_adapter = OpenCodeAdapter(command_prefix=tuple(json.loads(args.opencode_command))) if args.opencode_command else None
    claude_code_adapter = ClaudeCodeAdapter(command_prefix=tuple(json.loads(args.claude_command))) if args.claude_command else None
    codex_adapter = CodexAdapter(command_prefix=tuple(json.loads(args.codex_command))) if args.codex_command else None
    cursor_adapter = CursorAdapter(command_prefix=tuple(json.loads(args.cursor_command))) if args.cursor_command else None
    if args.command == "doctor":
        report, exit_code = run_doctor(
            home=args.home,
            workspace=args.workspace,
            providers_config=args.providers_config,
            opencode_adapter=opencode_adapter,
            claude_code_adapter=claude_code_adapter,
            codex_adapter=codex_adapter,
            cursor_adapter=cursor_adapter,
        )
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(format_human_report(report))
        return exit_code
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
        elif args.command == "discover":
            output = broker.discover_capabilities()
        elif args.command == "select":
            output = broker.select_candidates(
                execution_mode=args.execution_mode,
                required_runtime_capabilities=_merge_capability_flags(args.runtime_capabilities),
                required_provider_capabilities=_merge_capability_flags(args.provider_capabilities),
                allowed_runtimes=args.allowed_runtimes,
                allowed_providers=args.allowed_providers,
                require_available=not args.include_unavailable,
            )
        elif args.command == "council":
            plan = json.loads(args.plan)
            if args.council_command == "validate":
                output = broker.validate_council(plan)
            else:
                output = broker.execute_council(plan)
        else:
            output = [record.json() for record in broker.store.list()]
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    except (KeyError, ValueError, InvalidTransition) as error:
        print(json.dumps({"error": {"code": type(error).__name__, "message": str(error)}}, sort_keys=True))
        return 2
    finally:
        broker.close()
