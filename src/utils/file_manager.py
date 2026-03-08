import os
import shutil
import threading
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

WORKSPACE_DIR = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))
WORKSPACE_MAX_AGE_HOURS = int(os.environ.get("WORKSPACE_MAX_AGE_HOURS", "24"))

FAILURE_INDICATORS = [
    "no offers found", "no results found", "no data found", "api error",
    "error occurred", "failed to fetch", "could not find", "unable to retrieve",
    "access denied", "403 forbidden", "404 not found", "connection refused",
    "timeout", "empty response", "no matching", "0 results", "zero results",
    "nothing found", "brak wyników", "brak ofert", "nie znaleziono",
    "no jobs found", "no positions found", "request failed", "fetch error",
    "scraping failed", "rate limited", "too many requests", "blocked",
    "captcha", "not available", "service unavailable", "empty array", "empty list",
]

PLACEHOLDER_PATTERNS = [
    "example data", "sample output", "placeholder", "lorem ipsum", "test data", "mock data",
]


def create_workspace(task_id: str) -> Path:
    task_workspace = WORKSPACE_DIR / task_id
    task_workspace.mkdir(parents=True, exist_ok=True)
    return task_workspace


def cleanup_workspace(task_id: str) -> None:
    task_workspace = WORKSPACE_DIR / task_id
    if task_workspace.exists():
        shutil.rmtree(task_workspace, ignore_errors=True)


def zip_directory(source_dir: Path, output_path: Path, include_extensions: Optional[set] = None) -> None:
    """Zip directory, optionally filtering by file extensions."""
    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for file_path in source_dir.rglob('*'):
            if file_path.is_file() and file_path.name != 'project.zip':
                if include_extensions is None or file_path.suffix.lower() in include_extensions:
                    arcname = file_path.relative_to(source_dir)
                    zipf.write(file_path, arcname)


def check_file_content_validity(file_path: Path) -> tuple[bool, str]:
    try:
        content = file_path.read_text(errors='ignore').strip().lower()
        if not content:
            return False, "File is empty"

        for indicator in FAILURE_INDICATORS:
            if indicator in content:
                lines = content.split('\n')
                if len(lines) <= 3:
                    return False, f"File contains failure message: '{indicator}'"
                actual_data_lines = [l for l in lines if l.strip() and indicator not in l]
                if len(actual_data_lines) < 2:
                    return False, f"File contains mostly failure messages: '{indicator}'"

        for pattern in PLACEHOLDER_PATTERNS:
            if pattern in content:
                return False, f"File contains placeholder data: '{pattern}'"

        lines = [l.strip() for l in content.split('\n') if l.strip()]
        if len(lines) < 2 and len(content) < 100:
            return False, "File contains too little data (less than 2 lines, under 100 chars)"

        return True, "Content appears valid"
    except Exception as e:
        return False, f"Could not read file: {e}"


def validate_output_files(workspace: Path) -> tuple[bool, str, list[Path]]:
    data_extensions = {'.txt', '.json', '.csv', '.xml', '.html', '.md', '.yaml', '.yml', '.xlsx', '.xls'}
    code_extensions = {'.py', '.cs', '.js', '.ts', '.java', '.cpp', '.c', '.h'}

    data_files, code_files, all_files = [], [], []

    for f in workspace.rglob('*'):
        if f.is_file() and not f.name.startswith('.') and f.name != 'project.zip':
            all_files.append(f)
            ext = f.suffix.lower()
            if ext in data_extensions:
                data_files.append(f)
            elif ext in code_extensions:
                code_files.append(f)

    if not all_files:
        return False, "No output files were created.", []

    empty_files, small_files, invalid_content_files = [], [], []

    for f in data_files:
        try:
            size = f.stat().st_size
            if size == 0:
                empty_files.append(f.name)
                continue
            if size < 50:
                content = f.read_text(errors='ignore').strip()
                if not content or content in ('[]', '{}', 'null', 'None', ''):
                    small_files.append(f.name)
                    continue
            is_valid, reason = check_file_content_validity(f)
            if not is_valid:
                invalid_content_files.append((f.name, reason))
        except Exception:
            pass

    if empty_files:
        return False, f"Empty data files detected: {', '.join(empty_files)}.", data_files
    if small_files:
        return False, f"Files with no meaningful data: {', '.join(small_files)}.", data_files
    if invalid_content_files:
        details = "; ".join([f"{name}: {reason}" for name, reason in invalid_content_files])
        return False, f"Files contain error messages or invalid data: {details}.", data_files
    if data_files:
        return True, f"Found {len(data_files)} valid data file(s) with actual content.", data_files
    if code_files and not data_files:
        return True, f"Found {len(code_files)} code file(s) (no data files, which may be expected).", code_files

    return True, f"Found {len(all_files)} file(s).", all_files


def select_output_files(workspace: Path) -> list[str]:
    """
    Wybiera pliki do zwrócenia użytkownikowi według priorytetu:

    1. Są pliki danych (.txt, .csv, .json itp.)
       -> zwróć TYLKO dane, ignoruj skrypty .py
       -> jeśli > 3 pliki danych: spakuj tylko dane do zip
       -> jeśli 1-3 pliki danych: zwróć bezpośrednio

    2. Są TYLKO pliki kodu (.py, .cs itp.), brak danych
       -> zwróć kod (lub zip jeśli > 3)

    3. Fallback: spakuj wszystko
    """
    data_extensions = {'.txt', '.json', '.csv', '.xml', '.html', '.md', '.yaml', '.yml', '.xlsx', '.xls'}
    code_extensions = {'.py', '.cs', '.js', '.ts', '.java', '.cpp', '.c', '.h'}

    data_files = []
    code_files = []

    for f in workspace.rglob('*'):
        if f.is_file() and not f.name.startswith('.') and f.name != 'project.zip':
            ext = f.suffix.lower()
            if ext in data_extensions:
                data_files.append(f)
            elif ext in code_extensions:
                code_files.append(f)

    def to_output_path(p: Path) -> str:
        return str(p).replace(str(WORKSPACE_DIR), '/app/agent-output')

    if data_files:
        if len(data_files) > 3:
            zip_path = workspace / "project.zip"
            zip_directory(workspace, zip_path, include_extensions=data_extensions)
            return [to_output_path(zip_path)]
        return [to_output_path(f) for f in data_files]

    if code_files:
        if len(code_files) > 3:
            zip_path = workspace / "project.zip"
            zip_directory(workspace, zip_path, include_extensions=code_extensions)
            return [to_output_path(zip_path)]
        return [to_output_path(f) for f in code_files]

    zip_path = workspace / "project.zip"
    zip_directory(workspace, zip_path)
    return [to_output_path(zip_path)]


def cleanup_old_workspaces(is_task_active_fn=None) -> None:
    """Remove workspaces older than WORKSPACE_MAX_AGE_HOURS.

    Args:
        is_task_active_fn: Optional callable(task_id: str) -> bool that returns
            True when the task is still running/queued and must not be removed.
    """
    if not WORKSPACE_DIR.exists():
        return

    cutoff = datetime.utcnow() - timedelta(hours=WORKSPACE_MAX_AGE_HOURS)
    removed = 0

    for entry in WORKSPACE_DIR.iterdir():
        if not entry.is_dir():
            continue
        try:
            mtime = datetime.utcfromtimestamp(entry.stat().st_mtime)
            if mtime >= cutoff:
                continue
            if is_task_active_fn and is_task_active_fn(entry.name):
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
        except Exception:
            pass

    if removed:
        print(f"[cleanup] Removed {removed} old workspace(s) older than {WORKSPACE_MAX_AGE_HOURS}h", flush=True)


def start_cleanup_scheduler(is_task_active_fn=None) -> None:
    """Start background thread that periodically runs cleanup_old_workspaces."""
    def loop():
        while True:
            time.sleep(3600)
            try:
                cleanup_old_workspaces(is_task_active_fn)
            except Exception as e:
                print(f"[cleanup] Error during scheduled cleanup: {e}", flush=True)

    thread = threading.Thread(target=loop, daemon=True, name="workspace-cleanup")
    thread.start()
