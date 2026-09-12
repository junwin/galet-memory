from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class TextSnippet:
    text: str
    truncated: bool = False


class TextLoader(Protocol):
    def load(self, path: str | Path, *, max_chars: int) -> TextSnippet: ...


class FileTextLoader:
    """Load a bounded UTF-8 text snippet from a caller-authorized path."""

    def load(self, path: str | Path, *, max_chars: int) -> TextSnippet:
        if max_chars < 0:
            raise ValueError("max_chars must be non-negative")
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
        return TextSnippet(text=text[:max_chars], truncated=len(text) > max_chars)
