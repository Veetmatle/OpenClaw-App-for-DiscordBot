from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

import anthropic

if TYPE_CHECKING:
    from core.engine import AgentTask

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

CLAUDE_RETRY_MAX_ATTEMPTS = 5
CLAUDE_RETRY_BASE_DELAY_S = 2.0
CLAUDE_RETRY_MAX_DELAY_S = 60.0

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def call_claude_with_retry(
    system_prompt: str,
    user_prompt: str,
    model: str,
    task: AgentTask,
) -> str:
    from datetime import datetime

    attempt = 0
    delay = CLAUDE_RETRY_BASE_DELAY_S

    while attempt < CLAUDE_RETRY_MAX_ATTEMPTS:
        if task.cancelled:
            raise RuntimeError("Task was cancelled before Claude call.")

        elapsed = (datetime.utcnow() - task.started_at).total_seconds()
        if elapsed > task.timeout_seconds:
            raise RuntimeError(f"Task timed out after {task.timeout_seconds}s.")

        try:
            response = client.messages.create(
                model=model,
                max_tokens=8192,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return response.content[0].text
        except anthropic.RateLimitError as e:
            attempt += 1
            if attempt >= CLAUDE_RETRY_MAX_ATTEMPTS:
                raise RuntimeError(
                    f"Claude API rate limit after {CLAUDE_RETRY_MAX_ATTEMPTS} attempts: {e}"
                ) from e
            wait = min(delay * (2 ** (attempt - 1)), CLAUDE_RETRY_MAX_DELAY_S)
            print(f"[Claude] Rate limited, waiting {wait}s before retry {attempt}", flush=True)
            time.sleep(wait)
        except anthropic.APIStatusError as e:
            if e.status_code in (500, 503, 529):
                attempt += 1
                if attempt >= CLAUDE_RETRY_MAX_ATTEMPTS:
                    raise RuntimeError(
                        f"Claude API error after {CLAUDE_RETRY_MAX_ATTEMPTS} attempts: {e}"
                    ) from e
                wait = min(delay * (2 ** (attempt - 1)), CLAUDE_RETRY_MAX_DELAY_S)
                print(f"[Claude] Server error {e.status_code}, waiting {wait}s before retry {attempt}", flush=True)
                time.sleep(wait)
            else:
                raise
        except Exception as e:
            raise RuntimeError(f"Claude API unexpected error: {e}") from e

    raise RuntimeError("Claude API: max retries exhausted")
