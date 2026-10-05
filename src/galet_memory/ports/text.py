from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class TextSnippet:
    text: str
    truncated: bool = False


class TextLoader(Protocol):
    def load(self, path: str | Path, *, max_chars: int | None) -> TextSnippet: ...


class FileTextLoader:
    """Load UTF-8 text, optionally bounded from a caller-authorized path."""

    def load(self, path: str | Path, *, max_chars: int | None) -> TextSnippet:
        if max_chars is None:
            return TextSnippet(Path(path).read_text(encoding="utf-8", errors="ignore"))
        if max_chars < 0:
            raise ValueError("max_chars must be non-negative")
        with Path(path).open("r", encoding="utf-8", errors="ignore") as source:
            text = source.read(max_chars + 1)
        return TextSnippet(text=text[:max_chars], truncated=len(text) > max_chars)
