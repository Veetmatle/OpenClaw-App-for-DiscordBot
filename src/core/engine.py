import os
import re
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

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
MAX_ITERATIONS = int(os.environ.get("MAX_ITERATIONS", "5"))
_SESSION_TIMEOUT_MINUTES = int(os.environ.get("AGENT_SESSION_TIMEOUT_MINUTES", "10"))
TIMEOUT_SECONDS = _SESSION_TIMEOUT_MINUTES * 60
MAX_CONCURRENT_TASKS = int(os.environ.get("MAX_CONCURRENT_TASKS", "2"))

# Keywords that hint the task needs live web data
_WEB_SEARCH_HINTS = re.compile(
    r'\b(znajdź|wyszukaj|sprawdź|aktualne?|obecne?|dzisiaj|teraz|latest|current|search|find|today|news|cena|price|kurs)\b',
    re.IGNORECASE,
)


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
    direct_response: Optional[str] = None
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
    task = get_task(task_id)
    return task is not None and task.status in (TaskStatus.RUNNING, TaskStatus.QUEUED)


def _needs_web_search(prompt: str) -> bool:
    """Heuristic: enable web_search only when the prompt clearly needs live data."""
    return bool(_WEB_SEARCH_HINTS.search(prompt))


# ---------------------------------------------------------------------------
# Agent execution
# ---------------------------------------------------------------------------

def execute_agent_task(task: AgentTask) -> None:
    """
    Agent oparty o natywny tool_use.
    Model sam używa narzędzi (write_file, run_bash, read_file, list_dir,
    mark_output, opcjonalnie web_search).

    Priorytet wyboru plików wyjściowych:
    1. Pliki oznaczone przez model przez mark_output — precyzyjne, bez zgadywania
    2. Fallback: validate_output_files + select_output_files — gdy model nie wywołał mark_output
    """
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
        web_search = _needs_web_search(task.prompt)

        user_content = task.prompt
        if task.document_content:
            user_content += f"\n\nAttached document:\n---\n{task.document_content}\n---"

        messages: list[dict] = [
            {"role": "user", "content": user_content}
        ]

        print(
            f"[Agent] Task {task.task_id} starting | web_search={web_search} | workspace: {workspace}",
            flush=True,
        )

        final_text, marked_outputs = call_claude_with_retry(
            system_prompt=SYSTEM_PROMPT,
            messages=messages,
            model=task.model,
            task=task,
            workspace=workspace,
            web_search=web_search,
        )

        if task.cancelled:
            task.status = TaskStatus.CANCELLED
            return

        if marked_outputs:
            # Model explicitly marked output files — use them directly
            task.output_files = marked_outputs
            task.direct_response = final_text if final_text else None
            task.status = TaskStatus.COMPLETED
            task.message = f"Completed. {len(marked_outputs)} output file(s) marked by agent."
            print(f"[Agent] Task {task.task_id}: COMPLETED via mark_output ({len(marked_outputs)} files)", flush=True)

        else:
            # Fallback: scan workspace for valid output files
            is_valid, validation_msg, _ = validate_output_files(workspace)

            if is_valid:
                task.output_files = select_output_files(workspace)
                task.direct_response = final_text if final_text else None
                task.status = TaskStatus.COMPLETED
                task.message = f"Completed. {validation_msg}"
                print(f"[Agent] Task {task.task_id}: COMPLETED via fallback scan ({len(task.output_files)} files)", flush=True)

            elif final_text:
                task.direct_response = final_text
                task.status = TaskStatus.COMPLETED
                task.message = "Completed with direct answer."
                print(f"[Agent] Task {task.task_id}: DIRECT_ANSWER", flush=True)

            else:
                task.status = TaskStatus.FAILED
                task.error = "Agent produced no output."
                print(f"[Agent] Task {task.task_id}: FAILED (no output)", flush=True)

    except RuntimeError as e:
        task.status = TaskStatus.FAILED
        task.error = str(e)
        print(f"[Agent] Task {task.task_id}: FAILED — {e}", flush=True)
    except Exception as e:
        task.status = TaskStatus.FAILED
        task.error = f"Unexpected error: {e}"
        print(f"[Agent] Task {task.task_id}: FAILED — {e}", flush=True)
    finally:
        task.completed_at = datetime.utcnow()
        update_task(task)
        _task_semaphore.release()


def run_task_async(task: AgentTask) -> None:
    thread = threading.Thread(target=execute_agent_task, args=(task,))
    thread.daemon = True
    thread.start()