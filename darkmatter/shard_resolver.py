"""
Live code resolution and health tracking for file-anchored shards.

Ported from Glance (https://github.com/dadukhankevin/Glance) and adapted
for DarkMatter's push-synced shard system.

Depends on: (none — pure utility)
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Function detection patterns (multi-language)
# ---------------------------------------------------------------------------

FUNC_PATTERNS = [
    re.compile(r"^\s*(async\s+)?def\s+(\w+)\s*\("),           # Python
    re.compile(r"^\s*(export\s+)?(async\s+)?function\s+(\w+)\s*\("),  # JS/TS
    re.compile(r"^\s*(pub\s+)?(async\s+)?fn\s+(\w+)\s*[\(<]"),  # Rust
    re.compile(r"^\s*(public|private|protected|static|\s)+[\w<>\[\]]+\s+(\w+)\s*\("),  # Java/C#
    re.compile(r"^\s*func\s+(\w+)\s*\("),                      # Go
]


def detect_function_name(line: str) -> Optional[str]:
    """Try to extract a function name from a line."""
    for pattern in FUNC_PATTERNS:
        match = pattern.match(line)
        if match:
            groups = [g for g in match.groups() if g and g.strip()]
            if groups:
                return groups[-1].strip()
    return None


# ---------------------------------------------------------------------------
# Region resolution
# ---------------------------------------------------------------------------

@dataclass
class ResolvedRegion:
    content: str
    start_line: int   # 1-indexed
    end_line: int     # 1-indexed, inclusive
    function_anchor: Optional[str] = None


def find_text_in_lines(lines: list[str], text: str, start_from: int = 0) -> Optional[int]:
    """Find a line index where `text` appears (0-indexed)."""
    text_stripped = text.strip()
    for i in range(start_from, len(lines)):
        if text_stripped in lines[i].strip() or text_stripped in lines[i]:
            return i
    return None


def find_function_by_name(lines: list[str], func_name: str) -> Optional[int]:
    """Find a function definition line by name (0-indexed)."""
    for i, line in enumerate(lines):
        if detect_function_name(line) == func_name:
            return i
    return None


def _find_block_end(lines: list[str], start_idx: int) -> Optional[int]:
    """Find end of a code block using indentation heuristic."""
    if start_idx >= len(lines):
        return None
    start_line = lines[start_idx]
    start_indent = len(start_line) - len(start_line.lstrip())
    if detect_function_name(start_line):
        for i in range(start_idx + 1, min(start_idx + 200, len(lines))):
            line = lines[i]
            if not line.strip():
                continue
            if len(line) - len(line.lstrip()) <= start_indent and line.strip():
                return i - 1
        return min(start_idx + 50, len(lines) - 1)
    return None


def resolve_region(
    file_path: str, from_text: str, to_text: str,
    function_anchor: Optional[str] = None,
) -> Optional[ResolvedRegion]:
    """Resolve a shard region in a file. Returns None if file or region not found."""
    path = Path(file_path)
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError):
        return None

    lines = content.splitlines()
    if not lines:
        return None

    # Find from_text
    start_idx = find_text_in_lines(lines, from_text)

    # Fallback: function anchor
    if start_idx is None and function_anchor:
        func_line = find_function_by_name(lines, function_anchor)
        if func_line is not None:
            start_idx = find_text_in_lines(lines, from_text, start_from=max(0, func_line - 5))
            if start_idx is None:
                start_idx = func_line

    if start_idx is None:
        return None

    # Find to_text after start
    end_idx = find_text_in_lines(lines, to_text, start_from=start_idx)
    if end_idx is None:
        end_idx = _find_block_end(lines, start_idx)
    if end_idx is None or end_idx < start_idx:
        end_idx = min(start_idx + 20, len(lines) - 1)

    region_content = "\n".join(lines[start_idx:end_idx + 1])
    func_anchor = function_anchor or detect_function_name(lines[start_idx])

    return ResolvedRegion(
        content=region_content,
        start_line=start_idx + 1,
        end_line=end_idx + 1,
        function_anchor=func_anchor,
    )


# ---------------------------------------------------------------------------
# Health scoring
# ---------------------------------------------------------------------------

HEALTHY_THRESHOLD = 0.8
STALE_THRESHOLD = 0.4
MAX_STALE_VIEWS = 2


def hash_content(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def compute_health(original: str, current: str) -> float:
    """Similarity score 0.0–1.0 between original and current content."""
    if original == current:
        return 1.0
    if not original or not current:
        return 0.0
    orig_n = _normalize(original)
    curr_n = _normalize(current)
    if orig_n == curr_n:
        return 0.99
    return round(SequenceMatcher(None, orig_n, curr_n).ratio(), 3)


def _normalize(text: str) -> str:
    lines = text.splitlines()
    return "\n".join(line.strip() for line in lines if line.strip())


@dataclass
class ShardHealth:
    score: float
    status: str     # healthy | degraded | stale | expired | broken
    message: str

    def should_show_summary(self) -> bool:
        return self.status == "healthy"

    def should_delete(self) -> bool:
        return self.status in ("expired", "broken")


def assess_health(
    original_content: str, original_hash: str,
    current_content: Optional[str], stale_views: int = 0,
) -> ShardHealth:
    """Assess a code shard's health given current resolved content."""
    if current_content is None:
        return ShardHealth(0.0, "broken", "Could not resolve region in file")

    if hash_content(current_content) == original_hash:
        return ShardHealth(1.0, "healthy", "Unchanged")

    score = compute_health(original_content, current_content)

    if score >= HEALTHY_THRESHOLD:
        return ShardHealth(score, "healthy", "Minor changes, summary still valid")
    elif score >= STALE_THRESHOLD:
        return ShardHealth(score, "degraded", "Notable changes — showing raw content instead of summary")
    else:
        views_left = MAX_STALE_VIEWS - stale_views
        if views_left <= 0:
            return ShardHealth(score, "expired", "Major changes. Shard will be deleted. Re-create to keep alive.")
        return ShardHealth(score, "stale", f"Major changes. Deleted after {views_left} more view(s) unless re-created.")
