from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anthropic

if TYPE_CHECKING:
    from core.engine import AgentTask

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_RETRY_MAX_ATTEMPTS = 5
CLAUDE_RETRY_BASE_DELAY_S = 2.0
CLAUDE_RETRY_MAX_DELAY_S = 60.0
MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "20"))
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "4096"))

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS_BASE = [
    {
        "name": "run_bash",
        "description": "Run a bash command in the task workspace. Returns stdout, stderr and exit code.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Bash command to execute"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 120)", "default": 120},
            },
            "required": ["command"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file in the workspace. Creates parent directories if needed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path, e.g. 'Program.cs' or 'src/app.py'"},
                "content": {"type": "string", "description": "File content to write"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file from the workspace. Returns file content as text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path to read"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_dir",
        "description": "List files and directories in the workspace (or a subdirectory).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to list (default: '.' = workspace root)",
                    "default": ".",
                },
            },
            "required": [],
        },
    },
    {
        "name": "mark_output",
        "description": (
            "Mark one or more files as the final output of this task. "
            "Call this when your work is done and the result files are ready. "
            "Only mark files that contain actual results — not intermediate scripts or temp files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of relative file paths to mark as output, e.g. ['output.txt', 'report.csv']",
                },
            },
            "required": ["files"],
        },
    },
]

TOOL_WEB_SEARCH = {
    "type": "web_search_20250305",
    "name": "web_search",
}


def _get_tools(web_search: bool = False) -> list[dict]:
    tools = list(TOOLS_BASE)
    if web_search:
        tools.append(TOOL_WEB_SEARCH)
    return tools


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def _execute_tool(
    name: str,
    inputs: dict[str, Any],
    workspace: Path,
    marked_outputs: list[str],
) -> str:
    """Execute a tool call and return result as string.
    marked_outputs is mutated in place when mark_output is called.
    """
    from utils.shell_executor import run_command

    try:
        if name == "run_bash":
            command = inputs["command"]
            timeout = inputs.get("timeout", 120)
            print(f"[tool] run_bash: {command[:80]}", flush=True)
            returncode, stdout, stderr = run_command(
                ["bash", "-c", command], cwd=workspace, timeout=timeout
            )
            parts = [f"exit_code: {returncode}"]
            if stdout:
                parts.append(f"stdout:\n{stdout[:3000]}")
            if stderr:
                parts.append(f"stderr:\n{stderr[:1000]}")
            return "\n".join(parts)

        elif name == "write_file":
            path = inputs["path"]
            content = inputs["content"]
            file_path = workspace / path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            lines = len(content.splitlines())
            print(f"[tool] write_file: {path} ({lines} lines)", flush=True)
            return f"OK: wrote {len(content)} chars to {path}"

        elif name == "read_file":
            path = inputs["path"]
            file_path = workspace / path
            if not file_path.exists():
                return f"ERROR: file not found: {path}"
            content = file_path.read_text(encoding="utf-8", errors="replace")
            print(f"[tool] read_file: {path}", flush=True)
            return content[:5000]

        elif name == "list_dir":
            path = inputs.get("path", ".")
            dir_path = workspace / path
            if not dir_path.exists():
                return f"ERROR: directory not found: {path}"
            entries = []
            for entry in sorted(dir_path.iterdir()):
                if entry.is_dir():
                    entries.append(f"DIR   {entry.name}/")
                else:
                    entries.append(f"FILE  {entry.name}  ({entry.stat().st_size} bytes)")
            print(f"[tool] list_dir: {path} ({len(entries)} entries)", flush=True)
            return "\n".join(entries) if entries else "(empty)"

        elif name == "mark_output":
            files = inputs.get("files", [])
            confirmed = []
            missing = []
            for f in files:
                file_path = workspace / f
                if file_path.exists():
                    abs_path = str(file_path.resolve())
                    if abs_path not in marked_outputs:
                        marked_outputs.append(abs_path)
                    confirmed.append(f)
                else:
                    missing.append(f)
            print(f"[tool] mark_output: {confirmed}", flush=True)
            result = f"OK: marked as output: {confirmed}"
            if missing:
                result += f"\nWARNING: files not found (not marked): {missing}"
            return result

        else:
            return f"ERROR: unknown local tool '{name}'"

    except Exception as e:
        return f"ERROR: {e}"


# ---------------------------------------------------------------------------
# Main Claude call
# ---------------------------------------------------------------------------

def call_claude_with_retry(
    system_prompt: str,
    messages: list[dict],
    model: str,
    task: "AgentTask",
    workspace: Path | None = None,
    web_search: bool = False,
) -> tuple[str, list[str]]:
    """
    Wywołuje Claude z natywnym tool_use.

    Model używa narzędzi (write_file, run_bash, read_file, list_dir, mark_output,
    opcjonalnie web_search) dopóki nie skończy i nie wróci end_turn.

    Zwraca (final_text, marked_output_paths).
    marked_output_paths to lista bezwzględnych ścieżek plików oznaczonych przez mark_output.
    Jeśli model nie wywołał mark_output, lista jest pusta — engine wtedy fallbackuje
    na validate_output_files.
    """
    from datetime import datetime

    attempt = 0
    delay = CLAUDE_RETRY_BASE_DELAY_S
    tools = _get_tools(web_search=web_search)
    ws = workspace or Path("/workspace")

    while attempt < CLAUDE_RETRY_MAX_ATTEMPTS:
        if task.cancelled:
            raise RuntimeError("Task was cancelled.")

        elapsed = (datetime.utcnow() - task.started_at).total_seconds()
        if elapsed > task.timeout_seconds:
            raise RuntimeError(f"Task timed out after {task.timeout_seconds}s.")

        try:
            working_messages = list(messages)
            tool_rounds = 0
            marked_outputs: list[str] = []

            while tool_rounds < MAX_TOOL_ROUNDS:
                response = client.messages.create(
                    model=model,
                    max_tokens=MAX_TOKENS,
                    system=system_prompt,
                    messages=working_messages,
                    tools=tools,
                )

                if response.stop_reason == "end_turn":
                    text = "\n".join(
                        block.text for block in response.content
                        if block.type == "text"
                    ).strip()
                    working_messages.append({
                        "role": "assistant",
                        "content": response.content,
                    })
                    return text, marked_outputs

                if response.stop_reason == "tool_use":
                    tool_rounds += 1
                    working_messages.append({
                        "role": "assistant",
                        "content": response.content,
                    })

                    tool_results = []
                    for block in response.content:
                        if block.type != "tool_use":
                            continue
                        if task.cancelled:
                            raise RuntimeError("Task cancelled during tool execution.")

                        result = _execute_tool(block.name, block.input, ws, marked_outputs)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })

                    if tool_results:
                        working_messages.append({
                            "role": "user",
                            "content": tool_results,
                        })
                    continue

                # Any other stop reason
                text = "\n".join(
                    block.text for block in response.content
                    if block.type == "text"
                ).strip()
                return text, marked_outputs

            return "Agent hit max tool rounds without completing.", marked_outputs

        except anthropic.RateLimitError as e:
            attempt += 1
            if attempt >= CLAUDE_RETRY_MAX_ATTEMPTS:
                raise RuntimeError(f"Rate limit after {CLAUDE_RETRY_MAX_ATTEMPTS} attempts: {e}") from e
            wait = min(delay * (2 ** (attempt - 1)), CLAUDE_RETRY_MAX_DELAY_S)
            print(f"[Claude] Rate limited, waiting {wait}s (retry {attempt})", flush=True)
            time.sleep(wait)

        except anthropic.APIStatusError as e:
            if e.status_code in (500, 503, 529):
                attempt += 1
                if attempt >= CLAUDE_RETRY_MAX_ATTEMPTS:
                    raise RuntimeError(f"API error after {CLAUDE_RETRY_MAX_ATTEMPTS} attempts: {e}") from e
                wait = min(delay * (2 ** (attempt - 1)), CLAUDE_RETRY_MAX_DELAY_S)
                print(f"[Claude] Server error {e.status_code}, waiting {wait}s (retry {attempt})", flush=True)
                time.sleep(wait)
            else:
                raise

        except Exception as e:
            raise RuntimeError(f"Claude API unexpected error: {e}") from e

    raise RuntimeError("Claude API: max retries exhausted")