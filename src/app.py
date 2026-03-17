"""
AI Agent Server - Flask HTTP API for task execution with Claude (Anthropic).
"""

import base64
import os
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify

from core.engine import (
    AgentTask,
    TaskStatus,
    ANTHROPIC_MODEL,
    MAX_ITERATIONS,
    TIMEOUT_SECONDS,
    get_task,
    update_task,
    run_task_async,
    is_task_active,
)
from utils.file_manager import (
    WORKSPACE_DIR,
    cleanup_workspace,
    cleanup_old_workspaces,
    start_cleanup_scheduler,
)

app = Flask(__name__)

WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
cleanup_old_workspaces(is_task_active_fn=is_task_active)
start_cleanup_scheduler(is_task_active_fn=is_task_active)

# Max file size returned inline as base64 (10 MB)
MAX_INLINE_FILE_BYTES = int(os.environ.get("MAX_INLINE_FILE_BYTES", str(10 * 1024 * 1024)))


@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "model": ANTHROPIC_MODEL,
    })


@app.route('/tasks', methods=['POST'])
def submit_task():
    data = request.json
    if not data:
        return jsonify({"error": "Empty request"}), 400

    prompt = data.get('Prompt') or data.get('prompt')
    task_id = data.get('TaskId') or data.get('taskId') or str(uuid.uuid4())[:8]
    doc_content = data.get('DocumentContent') or data.get('documentContent')
    max_iter = data.get('MaxIterations') or data.get('maxIterations') or MAX_ITERATIONS
    timeout = data.get('TimeoutSeconds') or data.get('timeoutSeconds') or TIMEOUT_SECONDS

    if not prompt:
        return jsonify({"error": "Missing field: prompt"}), 400

    task = AgentTask(
        task_id=task_id,
        prompt=prompt,
        document_content=doc_content,
        model=ANTHROPIC_MODEL,
        max_iterations=int(max_iter),
        timeout_seconds=int(timeout),
    )

    update_task(task)
    run_task_async(task)

    print(f"[Agent] Task {task_id} submitted with model: {ANTHROPIC_MODEL}", flush=True)

    return jsonify({
        "TaskId": task.task_id,
        "Status": task.status.value,
        "Message": "Task submitted successfully",
    }), 202


@app.route('/tasks/<task_id>', methods=['GET'])
def get_task_status(task_id: str):
    task = get_task(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404

    return jsonify({
        "TaskId": task.task_id,
        "Status": task.status.value,
        "Message": task.message,
        "DirectResponse": task.direct_response,
        "Error": task.error,
        # OutputFiles są teraz tylko metadane (nazwy + rozmiary)
        # Zawartość pobierana osobno przez GET /tasks/<id>/files
        "OutputFiles": [
            {
                "FileName": Path(p).name,
                "SizeBytes": Path(p).stat().st_size if Path(p).exists() else 0,
            }
            for p in (task.output_files or [])
        ],
    })


@app.route('/tasks/<task_id>/files', methods=['GET'])
def get_task_files(task_id: str):
    """
    Zwraca zawartość plików wyjściowych jako base64 w JSON.
    Bot pobiera to przez HTTP — zero shared volume, zero race conditions.
    """
    task = get_task(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404

    if task.status != TaskStatus.COMPLETED:
        return jsonify({"error": f"Task not completed (status: {task.status.value})"}), 400

    if not task.output_files:
        return jsonify({"TaskId": task_id, "Files": []}), 200

    files = []
    for file_path_str in task.output_files:
        file_path = Path(file_path_str)
        if not file_path.exists():
            print(f"[Agent] Warning: output file missing: {file_path}", flush=True)
            continue

        size = file_path.stat().st_size
        if size > MAX_INLINE_FILE_BYTES:
            # Plik za duży — zwróć metadane bez zawartości
            files.append({
                "FileName": file_path.name,
                "SizeBytes": size,
                "ContentBase64": None,
                "TooLarge": True,
            })
            continue

        content_b64 = base64.b64encode(file_path.read_bytes()).decode("utf-8")
        files.append({
            "FileName": file_path.name,
            "SizeBytes": size,
            "ContentBase64": content_b64,
            "TooLarge": False,
        })

    return jsonify({
        "TaskId": task_id,
        "Files": files,
    })


@app.route('/tasks/<task_id>', methods=['DELETE'])
def cancel_task(task_id: str):
    task = get_task(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404

    if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
        return jsonify({"error": f"Task already {task.status.value}"}), 400

    task.cancelled = True
    task.status = TaskStatus.CANCELLED
    task.completed_at = datetime.utcnow()
    update_task(task)
    cleanup_workspace(task_id)

    return jsonify({
        "TaskId": task.task_id,
        "Status": task.status.value,
        "Message": "Task cancelled",
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)