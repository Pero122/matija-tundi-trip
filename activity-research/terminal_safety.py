"""Terminal-safe rendering for text originating outside the repository."""

from __future__ import annotations

import re
from typing import Any


TERMINAL_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def terminal_line(value: Any, *, limit: int | None = None) -> str:
    """Return one printable line with C0/C1 terminal controls removed."""

    rendered = TERMINAL_CONTROL_RE.sub("", str(value))
    rendered = rendered.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return rendered[:limit] if limit is not None else rendered


__all__ = ["TERMINAL_CONTROL_RE", "terminal_line"]
