"""
AI Agent Server - Flask HTTP API for task execution with Claude (Anthropic).
Provides endpoints for task submission, status checking, and cancellation.
"""

import os
import uuid
from datetime import datetime

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

# ---------------------------------------------------------------------------
# Startup initialisation
# ---------------------------------------------------------------------------

WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
cleanup_old_workspaces(is_task_active_fn=is_task_active)
start_cleanup_scheduler(is_task_active_fn=is_task_active)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

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
        "OutputFiles": task.output_files,
        "Error": task.error,
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
