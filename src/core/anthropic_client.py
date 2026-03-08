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

MAX_TOOL_ROUNDS = 10

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

TOOLS = [
    {
        "type": "web_search_20250305",
        "name": "web_search",
    }
]


def _extract_text_from_response(response: anthropic.types.Message) -> str:
    """
    Wyciąga tekst z odpowiedzi. Obsługuje zarówno zwykłe odpowiedzi tekstowe,
    jak i odpowiedzi po użyciu narzędzi (tool_use -> tool_result -> text).
    """
    text_parts = []
    for block in response.content:
        if block.type == "text":
            text_parts.append(block.text)
    return "\n".join(text_parts).strip()


def call_claude_with_retry(
    system_prompt: str,
    messages: list[dict],
    model: str,
    task: AgentTask,
) -> str:
    """
    Wywołuje Claude z obsługą web_search tool.

    Gdy Claude użyje web_search, API zwraca stop_reason='tool_use'.
    Funkcja automatycznie kontynuuje rozmowę podając wyniki wyszukiwania
    z powrotem do Claude, az do uzyskania finalnej odpowiedzi tekstowej.

    `messages` to lista {"role": "user"|"assistant", "content": ...} zgodna z Anthropic API.
    """
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
            working_messages = list(messages)
            tool_rounds = 0

            while tool_rounds < MAX_TOOL_ROUNDS:
                response = client.messages.create(
                    model=model,
                    max_tokens=8192,
                    system=system_prompt,
                    messages=working_messages,
                    tools=TOOLS,
                )

                if response.stop_reason == "end_turn":
                    return _extract_text_from_response(response)

                if response.stop_reason == "tool_use":
                    tool_rounds += 1

                    working_messages.append({
                        "role": "assistant",
                        "content": response.content,  
                    })

                    tool_results = []
                    for block in response.content:
                        if block.type == "tool_use":
                            print(
                                f"[Claude] web_search: {block.input.get('query', '?')}",
                                flush=True,
                            )
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": block.input.get("content", ""),
                            })

                    working_messages.append({
                        "role": "user",
                        "content": tool_results,
                    })
                    continue

                return _extract_text_from_response(response)

            return _extract_text_from_response(response)

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
                print(
                    f"[Claude] Server error {e.status_code}, waiting {wait}s before retry {attempt}",
                    flush=True,
                )
                time.sleep(wait)
            else:
                raise

        except Exception as e:
            raise RuntimeError(f"Claude API unexpected error: {e}") from e

    raise RuntimeError("Claude API: max retries exhausted")