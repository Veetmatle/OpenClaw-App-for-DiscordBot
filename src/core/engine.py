import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from core.anthropic_client import call_claude_with_retry
from core.prompts import SYSTEM_PROMPT
from utils.file_manager import (
    create_workspace,
    validate_output_files,
    select_output_files,
)
from utils.shell_executor import run_command

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
MAX_ITERATIONS = int(os.environ.get("MAX_ITERATIONS", "7"))
_SESSION_TIMEOUT_MINUTES = int(os.environ.get("AGENT_SESSION_TIMEOUT_MINUTES", "10"))
TIMEOUT_SECONDS = _SESSION_TIMEOUT_MINUTES * 60
MAX_CONCURRENT_TASKS = int(os.environ.get("MAX_CONCURRENT_TASKS", "2"))


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class AgentTask:
    task_id: str
    prompt: str
    document_content: Optional[str] = None
    model: str = ANTHROPIC_MODEL
    max_iterations: int = MAX_ITERATIONS
    timeout_seconds: int = TIMEOUT_SECONDS
    status: TaskStatus = TaskStatus.QUEUED
    message: Optional[str] = None
    error: Optional[str] = None
    output_files: list = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    cancelled: bool = False


# ---------------------------------------------------------------------------
# In-memory task store
# ---------------------------------------------------------------------------

_tasks: dict[str, AgentTask] = {}
_task_lock = threading.Lock()
_task_semaphore = threading.Semaphore(MAX_CONCURRENT_TASKS)


def get_task(task_id: str) -> Optional[AgentTask]:
    with _task_lock:
        return _tasks.get(task_id)


def update_task(task: AgentTask) -> None:
    with _task_lock:
        _tasks[task.task_id] = task


def is_task_active(task_id: str) -> bool:
    """Return True when the task is still running or queued (used by cleanup)."""
    task = get_task(task_id)
    return task is not None and task.status in (TaskStatus.RUNNING, TaskStatus.QUEUED)


# ---------------------------------------------------------------------------
# Agent execution
# ---------------------------------------------------------------------------

def execute_agent_task(task: AgentTask) -> None:
    """Main agent execution loop implementing the ReAct pattern with Claude."""
    if not _task_semaphore.acquire(blocking=False):
        task.status = TaskStatus.FAILED
        task.error = f"Server at capacity (max {MAX_CONCURRENT_TASKS} concurrent tasks)"
        task.completed_at = datetime.utcnow()
        update_task(task)
        return

    try:
        task.status = TaskStatus.RUNNING
        task.started_at = datetime.utcnow()
        update_task(task)

        workspace = create_workspace(task.task_id)

        user_prompt = task.prompt
        if task.document_content:
            user_prompt += f"\n\nAttached document:\n---\n{task.document_content}\n---"

        conversation_history = user_prompt
        iteration = 0
        consecutive_empty_iterations = 0

        while iteration < task.max_iterations and not task.cancelled:
            iteration += 1
            print(f"[ReAct] Task {task.task_id}: Starting iteration {iteration}/{task.max_iterations}", flush=True)

            elapsed = (datetime.utcnow() - task.started_at).total_seconds()
            if elapsed > task.timeout_seconds:
                task.status = TaskStatus.FAILED
                task.error = f"Task timed out after {task.timeout_seconds}s"
                break

            assistant_message = call_claude_with_retry(
                SYSTEM_PROMPT, conversation_history, task.model, task
            )

            execution_results = []
            command_errors = []
            curl_commands_run = []
            python_scripts_run = []
            lines = assistant_message.split('\n')
            i = 0

            while i < len(lines):
                line = lines[i]
                if line.startswith('```') and not line.startswith('```bash'):
                    filename = line[3:].strip()
                    if filename and '.' in filename:
                        content_lines = []
                        i += 1
                        while i < len(lines) and not lines[i].startswith('```'):
                            content_lines.append(lines[i])
                            i += 1

                        if content_lines and content_lines[0].strip() == filename:
                            content_lines.pop(0)

                        file_path = workspace / filename
                        file_path.parent.mkdir(parents=True, exist_ok=True)
                        file_path.write_text('\n'.join(content_lines))
                        execution_results.append(f"[FILE CREATED] {filename} ({len(content_lines)} lines)")

                elif line.startswith('```bash'):
                    cmd_lines = []
                    i += 1
                    while i < len(lines) and not lines[i].startswith('```'):
                        cmd_lines.append(lines[i])
                        i += 1

                    python_keywords = ['import ', 'def ', 'class ', 'from ', 'print(', 'with open(']
                    python_lines_detected = sum(
                        1 for cmd in cmd_lines if any(kw in cmd for kw in python_keywords)
                    )

                    if python_lines_detected >= 3:
                        execution_results.append(
                            "[CRITICAL ERROR] You put Python code inside a ```bash block!\n"
                            "Create a .py file first, then run it with: python3 script.py"
                        )
                        command_errors.append("Python code detected in bash block")
                    else:
                        for cmd in cmd_lines:
                            cmd = cmd.strip()
                            if cmd and not cmd.startswith('#'):
                                if cmd.startswith('curl '):
                                    curl_commands_run.append(cmd)
                                elif 'python' in cmd and '.py' in cmd:
                                    python_scripts_run.append(cmd)

                                returncode, stdout, stderr = run_command(
                                    ["bash", "-c", cmd], cwd=workspace, timeout=120
                                )
                                result = f"[COMMAND] $ {cmd}\n[EXIT CODE] {returncode}"
                                if stdout:
                                    result += f"\n[STDOUT]\n{stdout[:3000]}"
                                if stderr:
                                    result += f"\n[STDERR]\n{stderr[:2000]}"
                                execution_results.append(result)

                                if returncode != 0:
                                    command_errors.append(
                                        f"Command '{cmd[:50]}...' failed with exit code {returncode}"
                                    )
                                    if ('python' in cmd and '.py' in cmd) and not curl_commands_run:
                                        execution_results.append(
                                            "\n[STRATEGY ERROR] Python failed without prior curl exploration!\n"
                                            "Use curl to find working endpoints FIRST."
                                        )
                i += 1

            wants_to_complete = "TASK COMPLETE" in assistant_message.upper()

            if wants_to_complete:
                is_valid, validation_msg, _ = validate_output_files(workspace)
                print(
                    f"[ReAct] Task {task.task_id}: Completion requested. "
                    f"Validation: {is_valid} - {validation_msg}",
                    flush=True,
                )

                if is_valid:
                    task.output_files = select_output_files(workspace)
                    task.status = TaskStatus.COMPLETED
                    task.message = f"Task completed after {iteration} iteration(s). {validation_msg}"
                    print(
                        f"[ReAct] Task {task.task_id}: COMPLETED with {len(task.output_files)} file(s)",
                        flush=True,
                    )
                    break
                else:
                    feedback = (
                        f"[VALIDATION FAILED] Output not valid: {validation_msg}\n"
                        "You MUST try a different approach. Do not give up."
                    )
                    conversation_history += (
                        f"\n\n[Assistant]:\n{assistant_message}\n\n[System Feedback]:\n{feedback}"
                    )
                    continue

            if not execution_results:
                consecutive_empty_iterations += 1
                if consecutive_empty_iterations >= 2:
                    conversation_history += (
                        f"\n\n[Assistant]:\n{assistant_message}\n\n"
                        "[System Warning]: No code executed for 2 iterations. You MUST write and run code!"
                    )
                continue
            else:
                consecutive_empty_iterations = 0

            feedback_lines = ["[EXECUTION RESULTS]:"] + execution_results

            if command_errors:
                feedback_lines.append(
                    f"\n[ERRORS]: {len(command_errors)} command(s) failed. Try a different approach."
                )
                if python_scripts_run and not curl_commands_run:
                    feedback_lines.append("[HINT]: Test with curl first before writing Python code!")
            else:
                is_valid, validation_msg, _ = validate_output_files(workspace)
                if not is_valid:
                    feedback_lines.append(
                        f"\n[DATA WARNING]: {validation_msg}\nTry a different approach."
                    )
                else:
                    feedback_lines.append(
                        "\n[STATUS]: Success. Verify output with `cat`, then say TASK COMPLETE."
                    )

            conversation_history += (
                f"\n\n[Assistant]:\n{assistant_message}\n\n{chr(10).join(feedback_lines)}"
            )

        if task.status == TaskStatus.RUNNING:
            task.status = TaskStatus.FAILED
            task.error = f"Max iterations ({task.max_iterations}) reached without completion"

    except RuntimeError as e:
        task.status = TaskStatus.FAILED
        task.error = str(e)
    except Exception as e:
        task.status = TaskStatus.FAILED
        task.error = f"Unexpected error: {str(e)}"
    finally:
        task.completed_at = datetime.utcnow()
        update_task(task)
        _task_semaphore.release()


def run_task_async(task: AgentTask) -> None:
    thread = threading.Thread(target=execute_agent_task, args=(task,))
    thread.daemon = True
    thread.start()
