"""Local stdio MCP interface exposing the Recollect Lines broker to parent agents.

Transport: newline-delimited JSON-RPC 2.0 over stdin/stdout (the MCP stdio
transport) — every stdout line is exactly one complete JSON-RPC message, and
nothing else is ever written there. Diagnostics go to stderr only.

Error model:
- JSON-RPC protocol errors (top-level `error` object) cover things the
  protocol itself rejects before any broker call is made: malformed JSON,
  a malformed envelope, an unknown top-level method, an unknown tool name,
  or a `tools/call` whose `name`/`arguments` are the wrong JSON type.
- Tool-result `isError: true` (see `_tool_result`) covers everything a
  *known* tool rejects once it starts running: a malformed argument value
  (e.g. missing `task`), an unknown task id, an illegal state transition, or
  any other broker/policy error. This keeps a single bad `delegate_batch`
  item from turning the whole call into a protocol failure.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

from .models import TaskRequest
from .service import Broker

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "recollect-lines-mcp"
SERVER_VERSION = "0.1.0"
ENVELOPE_VERSION = 1

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

EXECUTION_MODES = ("read_only", "isolated_worktree")
PROFILES = ("mock", "opencode")


class ProtocolError(Exception):
    """A JSON-RPC-level error: the message/request itself is invalid, before any tool runs."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --- tool-result envelope -------------------------------------------------


def _envelope(tool: str, ok: bool, data: Any = None, error: dict | None = None) -> dict:
    body = {"envelope_version": ENVELOPE_VERSION, "tool": tool, "ok": ok}
    body["data" if ok else "error"] = data if ok else error
    return body


def _tool_result(tool: str, ok: bool, data: Any = None, error: dict | None = None) -> dict:
    body = _envelope(tool, ok, data=data, error=error)
    return {"content": [{"type": "text", "text": json.dumps(body, indent=2, sort_keys=True)}], "isError": not ok}


# --- shared argument helpers -----------------------------------------------


def _validate_verify_commands(commands: Any) -> None:
    valid = isinstance(commands, list) and all(
        isinstance(command, list) and command and all(isinstance(part, str) for part in command)
        for command in commands
    )
    if not valid:
        raise ValueError("verify_commands must be an array of non-empty argv arrays of strings")


def _build_task_request(item: Any) -> tuple[TaskRequest, list | None]:
    """Validate one delegate-shaped item, raising ValueError on any bad field.

    Shared by `delegate` (a single item) and `delegate_batch` (many items). A
    ValueError raised here is reported as a business/tool error, never a raw
    JSON-RPC protocol error, so one bad `delegate_batch` item never disturbs
    another item's already-started task.
    """
    if not isinstance(item, dict):
        raise ValueError("Each delegate item must be an object")
    task = item.get("task")
    workspace = item.get("workspace")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("'task' must be a non-empty string")
    if not isinstance(workspace, str) or not workspace.strip():
        raise ValueError("'workspace' must be a non-empty string")
    execution_mode = item.get("execution_mode", "read_only")
    if execution_mode not in EXECUTION_MODES:
        raise ValueError(f"execution_mode must be one of {EXECUTION_MODES}, got {execution_mode!r}")
    profile = item.get("profile", "mock")
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of {PROFILES}, got {profile!r}")
    timeout_seconds = item.get("timeout_seconds", 1800)
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or timeout_seconds < 1:
        raise ValueError("timeout_seconds must be a positive integer")
    verify_commands = item.get("verify_commands")
    if verify_commands is not None:
        _validate_verify_commands(verify_commands)
    return TaskRequest(task, workspace, execution_mode, profile, timeout_seconds), verify_commands


def _require_task_id(args: dict) -> str:
    task_id = args.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("'task_id' must be a non-empty string")
    return task_id


def _task_summary(record) -> dict:
    return {
        "task_id": record.id,
        "state": record.state.value,
        "workspace": record.workspace,
        "execution_mode": record.execution_mode,
        "profile": record.profile,
    }


def _read_json_artifact(broker: Broker, task_id: str, name: str) -> Any:
    path = broker.store.artifacts / task_id / name
    return json.loads(path.read_text()) if path.is_file() else None


# --- tool handlers -----------------------------------------------------


def _create_and_start(broker: Broker, item: Any) -> tuple[Any, Exception | None]:
    """Validate, create, and start one delegate item.

    Returns (record, None) for any normal outcome of start() — including a
    broker-returned FAILED record (e.g. a bad workspace) — since that's not
    an unexpected failure. Returns (record, error) only if start() itself
    raised after the task was already durably created: the record (and its
    task_id) must never be lost just because the caller now needs to know
    something went wrong with an already-persisted task.
    """
    request, verify_commands = _build_task_request(item)
    record = broker.create(request)
    if verify_commands is not None:
        broker.store.write_artifact(record.id, "verify_commands.json", json.dumps(verify_commands, indent=2) + "\n")
    try:
        record = broker.start(record.id)
    except Exception as error:
        print(f"recollect_lines.mcp_server: task {record.id} start() raised unexpectedly: {error!r}", file=sys.stderr)
        return record, error
    return record, None


def handle_delegate(broker: Broker, args: dict) -> dict:
    record, start_error = _create_and_start(broker, args)
    if start_error is not None:
        raise ValueError(f"Task {record.id} was created but start() raised unexpectedly: {start_error}")
    return _task_summary(record)


def handle_delegate_batch(broker: Broker, args: dict) -> dict:
    tasks = args.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("'tasks' must be a non-empty array")
    outcomes = []
    for index, item in enumerate(tasks):
        try:
            record, start_error = _create_and_start(broker, item)
        except Exception as error:
            # A bad item must never lose the outcomes already recorded for earlier,
            # independently-started items in this batch.
            outcomes.append({"index": index, "accepted": False, "error": {"code": type(error).__name__, "message": str(error)}})
            continue
        if start_error is not None:
            # The task was created (it has a real task_id the caller can still act on
            # via status/cancel) but start() itself raised unexpectedly.
            outcomes.append({
                "index": index,
                "accepted": False,
                "task_id": record.id,
                "error": {"code": type(start_error).__name__, "message": f"Task was created but start() raised unexpectedly: {start_error}"},
            })
        else:
            outcomes.append({"index": index, "accepted": True, **_task_summary(record)})
    return {"outcomes": outcomes}


def handle_status(broker: Broker, args: dict) -> dict:
    return broker.status(_require_task_id(args))


def handle_collect(broker: Broker, args: dict) -> dict:
    task_id = _require_task_id(args)
    verify_commands = _read_json_artifact(broker, task_id, "verify_commands.json")
    broker_verification = None
    if verify_commands is not None:
        try:
            broker_verification = broker.verify(task_id, verify_commands)
        except Exception as error:
            # Best-effort: a verify failure (e.g. the isolated worktree was already
            # released) must never block returning the runtime-collected result below.
            broker_verification = {"ran": False, "error": str(error)}
    record = broker.collect(task_id)
    return {
        "task_id": record.id,
        "state": record.state.value,
        "runtime_result": _read_json_artifact(broker, task_id, "result.json"),
        "broker_verification": broker_verification,
    }


def handle_cancel(broker: Broker, args: dict) -> dict:
    task_id = _require_task_id(args)
    reason = args.get("reason", "Cancelled by MCP caller")
    if not isinstance(reason, str):
        raise ValueError("'reason' must be a string")
    return _task_summary(broker.cancel(task_id, reason))


def handle_message(broker: Broker, args: dict) -> dict:
    task_id = _require_task_id(args)
    content = args.get("content")
    if not isinstance(content, str):
        raise ValueError("'content' must be a string")
    record = broker.store.get(task_id)  # raises KeyError for an unknown task, same as every other tool
    return {
        "task_id": task_id,
        "status": "unsupported",
        "reason": (
            "Recollect Lines has no in-flight steering channel for any adapter: mock and "
            "OpenCode tasks both run to completion (or are cancelled outright). OpenCode "
            "itself does not support injecting a message into an already-running task."
        ),
        "profile": record.profile,
        "state": record.state.value,
    }


# --- tool schemas and registry ----------------------------------------------

DELEGATE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "description": "Natural-language description of the work to delegate."},
        "workspace": {
            "type": "string",
            "description": "Path to the source workspace. Must be a Git repository or worktree when execution_mode is isolated_worktree.",
        },
        "execution_mode": {
            "type": "string",
            "enum": list(EXECUTION_MODES),
            "default": "read_only",
            "description": "read_only runs directly against workspace; isolated_worktree runs in a broker-owned Git worktree branched from workspace's current HEAD.",
        },
        "profile": {
            "type": "string",
            "enum": list(PROFILES),
            "default": "mock",
            "description": "mock is a deterministic no-op adapter for testing; opencode runs the real OpenCode CLI as a supervised subprocess.",
        },
        "timeout_seconds": {
            "type": "integer",
            "minimum": 1,
            "default": 1800,
            "description": "Maximum seconds this task may run under the selected profile's policy.",
        },
        "verify_commands": {
            "type": "array",
            "items": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "description": "Optional argv-array commands (never shell strings) run as broker-verified evidence when this task is collected.",
        },
    },
    "required": ["task", "workspace"],
}

DELEGATE_BATCH_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": DELEGATE_INPUT_SCHEMA,
            "minItems": 1,
            "description": "Independent delegate requests. Each is validated and started independently; one invalid item never affects another.",
        },
    },
    "required": ["tasks"],
}

_TASK_ID_PROPERTY = {"task_id": {"type": "string", "description": "Task id returned by delegate or delegate_batch."}}

STATUS_INPUT_SCHEMA = {"type": "object", "properties": dict(_TASK_ID_PROPERTY), "required": ["task_id"]}
COLLECT_INPUT_SCHEMA = {"type": "object", "properties": dict(_TASK_ID_PROPERTY), "required": ["task_id"]}
CANCEL_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        **_TASK_ID_PROPERTY,
        "reason": {"type": "string", "default": "Cancelled by MCP caller", "description": "Human-readable reason recorded on the task."},
    },
    "required": ["task_id"],
}
MESSAGE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        **_TASK_ID_PROPERTY,
        "content": {"type": "string", "description": "Message that would have been steered into the running task."},
    },
    "required": ["task_id", "content"],
}

TOOLS = {
    "delegate": {
        "description": "Create and start one bounded delegated task. Returns the task id, resulting state, and workspace context — never a fabricated completion.",
        "inputSchema": DELEGATE_INPUT_SCHEMA,
        "handler": handle_delegate,
    },
    "delegate_batch": {
        "description": "Create and start a batch of delegated tasks independently. Each item is validated and started on its own; one rejected item does not affect the others.",
        "inputSchema": DELEGATE_BATCH_INPUT_SCHEMA,
        "handler": handle_delegate_batch,
    },
    "status": {
        "description": "Return a task's durable state, its event history, and its artifact manifest.",
        "inputSchema": STATUS_INPUT_SCHEMA,
        "handler": handle_status,
    },
    "collect": {
        "description": "Collect a completed task's runtime-reported result, plus broker-verified command evidence for any verify_commands supplied at delegate time.",
        "inputSchema": COLLECT_INPUT_SCHEMA,
        "handler": handle_collect,
    },
    "cancel": {
        "description": "Request cancellation of a task and return the factual resulting state and cancellation evidence.",
        "inputSchema": CANCEL_INPUT_SCHEMA,
        "handler": handle_cancel,
    },
    "message": {
        "description": "Always returns an explicit unsupported response: Recollect Lines has no in-flight steering channel for any adapter.",
        "inputSchema": MESSAGE_INPUT_SCHEMA,
        "handler": handle_message,
    },
}


def _dispatch_tool_call(broker: Broker, name: str, arguments: dict) -> dict:
    try:
        data = TOOLS[name]["handler"](broker, arguments)
    except (ValueError, KeyError) as error:
        return _tool_result(name, False, error={"code": type(error).__name__, "message": str(error)})
    except Exception as error:
        print(f"recollect_lines.mcp_server: unexpected error in tool {name!r}: {error!r}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return _tool_result(name, False, error={"code": "InternalError", "message": "Unexpected server error while executing this tool"})
    return _tool_result(name, True, data=data)


# --- JSON-RPC methods --------------------------------------------------


def _handle_initialize(params: Any) -> dict:
    requested = params.get("protocolVersion") if isinstance(params, dict) else None
    version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def _handle_tools_list() -> dict:
    return {"tools": [{"name": name, "description": tool["description"], "inputSchema": tool["inputSchema"]} for name, tool in TOOLS.items()]}


def _handle_tools_call(broker: Broker, params: Any) -> dict:
    if not isinstance(params, dict):
        raise ProtocolError(INVALID_PARAMS, "tools/call params must be an object")
    name = params.get("name")
    if not isinstance(name, str) or not name:
        raise ProtocolError(INVALID_PARAMS, "tools/call requires a non-empty string 'name'")
    if name not in TOOLS:
        raise ProtocolError(INVALID_PARAMS, f"Unknown tool: {name}")
    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        raise ProtocolError(INVALID_PARAMS, "tools/call 'arguments' must be an object")
    return _dispatch_tool_call(broker, name, arguments)


def _error_response(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _success_response(request_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _process_message(broker: Broker, message: Any) -> dict | None:
    """Return a JSON-RPC response, or None if none should be sent (a notification)."""
    if not isinstance(message, dict):
        return _error_response(None, INVALID_REQUEST, "Each message must be a JSON object")
    is_notification = "id" not in message
    request_id = message.get("id")
    if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return None if is_notification else _error_response(request_id, INVALID_REQUEST, "Message must be a JSON-RPC 2.0 request with a string 'method'")
    method = message["method"]
    params = message.get("params", {})
    try:
        if method == "initialize":
            result = _handle_initialize(params)
        elif method == "notifications/initialized":
            return None
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = _handle_tools_list()
        elif method == "tools/call":
            result = _handle_tools_call(broker, params)
        else:
            return None if is_notification else _error_response(request_id, METHOD_NOT_FOUND, f"Unknown method: {method}")
    except ProtocolError as error:
        return None if is_notification else _error_response(request_id, error.code, error.message)
    return None if is_notification else _success_response(request_id, result)


def _write_message(outstream, message: dict) -> None:
    outstream.write(json.dumps(message) + "\n")
    outstream.flush()


def serve(broker: Broker, instream, outstream) -> None:
    for raw_line in instream:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            _write_message(outstream, _error_response(None, PARSE_ERROR, f"Invalid JSON on a request line: {error}"))
            continue
        try:
            response = _process_message(broker, message)
        except Exception as error:
            print(f"recollect_lines.mcp_server: internal error handling message: {error!r}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            response = _error_response(message.get("id") if isinstance(message, dict) else None, INTERNAL_ERROR, "Unexpected internal server error")
        if response is not None:
            _write_message(outstream, response)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="recollect-mcp", description="Local stdio MCP interface for the Recollect Lines broker.")
    parser.add_argument("--home", type=Path, default=Path(".recollect"), help="Broker home directory (matches `recollect --home`).")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    broker = Broker(args.home)
    try:
        serve(broker, sys.stdin, sys.stdout)
    finally:
        broker.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
