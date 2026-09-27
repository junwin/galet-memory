"""Package-owned, complete-coverage session digest generation via Galet."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

from .curation import DigestGenerationError, DigestGenerationRequest


_INSTRUCTIONS = (
    "Create a faithful, concise session digest. Preserve decisions, outcomes, "
    "important facts, and open questions. Do not invent facts. Cover every "
    "supplied item. If an item is unclear, say so."
)


@dataclass(frozen=True)
class GaletDigestPolicy:
    model: str
    temperature: float = 0.0
    include_tool_events: bool = True


class GaletDigestGenerator:
    """Summarize every selected event, merging bounded chunks as necessary.

    The caller supplies a Galet LLMApi-compatible object. No prompt builder or
    application-specific code is involved. Oversized individual events fail
    explicitly rather than disappearing from the digest.
    """

    def __init__(self, llm_api: Any, policy: GaletDigestPolicy) -> None:
        self.llm_api = llm_api
        self.policy = policy

    def generate(self, request: DigestGenerationRequest) -> str:
        if request.max_chars <= 0:
            raise DigestGenerationError("max_chars must be positive")
        events = [event for event in request.events if event.kind == "session_digest" or self.policy.include_tool_events
                  or event.role in ("user", "assistant")]
        if not events:
            raise DigestGenerationError("no eligible events to digest")
        lines = []
        for event in events:
            content = event.content if isinstance(event.content, str) else json.dumps(
                event.content, ensure_ascii=False, sort_keys=True
            )
            lines.append(f"[{event.event_id}] {event.role}/{event.kind}: {content}")
        chunks = self._chunks(lines, request.max_chars)
        summaries = [self._call("Summarize all events in this interval:\n" + "\n".join(chunk))
                     for chunk in chunks]
        while len(summaries) > 1:
            groups = self._chunks(summaries, request.max_chars)
            if len(groups) == len(summaries):
                raise DigestGenerationError("model summaries did not shrink enough to merge")
            summaries = [self._call("Merge these consecutive interval digests, preserving "
                                    "all distinct facts:\n" + "\n".join(group))
                         for group in groups]
        return summaries[0]

    @staticmethod
    def _chunks(lines: Sequence[str], limit: int) -> list[list[str]]:
        chunks: list[list[str]] = []
        current: list[str] = []
        size = 0
        for line in lines:
            if len(line) > limit:
                raise DigestGenerationError("an event or intermediate digest exceeds max_chars")
            cost = len(line) + (1 if current else 0)
            if current and size + cost > limit:
                chunks.append(current)
                current, size, cost = [], 0, len(line)
            current.append(line)
            size += cost
        if current:
            chunks.append(current)
        return chunks

    def _call(self, prompt: str) -> str:
        try:
            response = self.llm_api.create_response(
                model=self.policy.model,
                input=[{"role": "system", "content": _INSTRUCTIONS},
                       {"role": "user", "content": prompt}],
                temperature=self.policy.temperature,
            )
            text = response.output_text
        except Exception as exc:
            raise DigestGenerationError("digest model call failed") from exc
        if not isinstance(text, str) or not text.strip():
            raise DigestGenerationError("digest model returned empty text")
        return text.strip()
