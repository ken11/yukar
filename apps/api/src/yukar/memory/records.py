"""Parse / append the source-of-truth project.jsonl.

Entry format (one JSON object per line):
    {"id":"mem-0001","content":"...","category":"fact","epic_id":"-","task_id":"-",
     "repo":"-","created":"2026-06-18","source":"remember"}

- content is a JSON string (newlines escaped as \\n, so it fits on one line).
  The body may contain ## / source: / code fences without conflicting with boundaries (bulletproof).
- Human-editable (one JSON per line). Blank lines are ignored. Malformed JSON lines are
  skipped with a warning log (resilient to manual edits: one broken line does not kill the rest).
- append goes through atomic_write_text under the lock, including ID assignment.
- Duplicate content is fully skipped via a content hash (case/whitespace normalisation).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from yukar.storage.atomic import atomic_write_text

_JST = ZoneInfo("Asia/Tokyo")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

VALID_CATEGORIES = frozenset({"convention", "fact", "lesson"})


@dataclass
class MemoryRecord:
    """A single memory record."""

    id: str
    content: str
    category: str = "fact"
    epic_id: str = "-"
    task_id: str = "-"
    repo: str = "-"
    created: str = ""
    source: str = "remember"
    metadata: dict[str, Any] = field(default_factory=dict)

    def content_hash(self) -> str:
        """SHA-256 digest of normalised content (used for duplicate detection).

        E1: shares the same logic as make_content_hash.
        """
        return make_content_hash(self.content)

    def to_jsonl_line(self) -> str:
        """Serialise to a single JSONL string (no trailing newline)."""
        obj = {
            "id": self.id,
            "content": self.content,
            "category": self.category,
            "epic_id": self.epic_id,
            "task_id": self.task_id,
            "repo": self.repo,
            "created": self.created,
            "source": self.source,
        }
        return json.dumps(obj, ensure_ascii=False)

    @staticmethod
    def from_jsonl(line: str) -> MemoryRecord | None:
        """Parse a single JSONL string into a MemoryRecord.

        Returns None for malformed lines (JSON parse failure or missing required fields).
        """
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("memory/records: JSON parse failed, skipping line: %r", line[:80])
            return None
        if not isinstance(obj, dict):
            logger.warning("memory/records: line is not a JSON object, skipping: %r", line[:80])
            return None
        record_id = obj.get("id")
        content = obj.get("content")
        if not isinstance(record_id, str) or not isinstance(content, str):
            logger.warning(
                "memory/records: missing required fields id/content, skipping: %r", line[:80]
            )
            return None
        return MemoryRecord(
            id=record_id,
            content=content,
            category=str(obj.get("category", "fact")),
            epic_id=str(obj.get("epic_id", "-")),
            task_id=str(obj.get("task_id", "-")),
            repo=str(obj.get("repo", "-")),
            created=str(obj.get("created", "")),
            source=str(obj.get("source", "remember")),
        )


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


def parse_records(text: str) -> list[MemoryRecord]:
    """Convert the text of project.jsonl into a list of MemoryRecord objects.

    - Blank lines are ignored.
    - Malformed JSON lines are skipped with a warning log (resilient to manual edits).
    - Records with empty/whitespace-only content are skipped with a warning log.
      Manually emptied content lines would produce zero vectors when embedded and pollute
      top-k results, so they are structurally excluded from all consumers
      (rebuild / _rebuild_under_lock / store.add dedup).
    """
    records: list[MemoryRecord] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        record = MemoryRecord.from_jsonl(stripped)
        if record is None:
            continue
        if not record.content.strip():
            logger.warning(
                "memory/records: empty/whitespace content, skipping record id=%r", record.id
            )
            continue
        records.append(record)
    return records


# ---------------------------------------------------------------------------
# Append
# ---------------------------------------------------------------------------


def _next_id(records: list[MemoryRecord]) -> str:
    """Assign the next ID based on existing records (format: mem-0001)."""
    max_n = 0
    for r in records:
        m = re.match(r"^mem-(\d+)$", r.id)
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"mem-{max_n + 1:04d}"


async def append_record(
    jsonl_path: Path,
    content: str,
    *,
    category: str = "fact",
    epic_id: str = "-",
    task_id: str = "-",
    repo: str = "-",
    source: str = "remember",
) -> MemoryRecord | None:
    """Append a new entry to the source-of-truth project.jsonl.

    - Full duplicate detection via content hash (case/whitespace normalisation).
      Returns None on duplicate.
    - Writes via atomic_write_text.
    - The caller must protect this call with a per-project lock
      (to guarantee concurrent add consistency).

    Returns:
        The appended MemoryRecord, or None on duplicate.
    """
    existing_text = ""
    if jsonl_path.exists():
        existing_text = jsonl_path.read_text(encoding="utf-8")

    records = parse_records(existing_text)

    # Hash-based duplicate check
    candidate_hash = make_content_hash(content)
    for r in records:
        if r.content_hash() == candidate_hash:
            return None  # skip duplicate

    new_id = _next_id(records)
    created = datetime.now(_JST).date().isoformat()
    record = MemoryRecord(
        id=new_id,
        content=content.strip(),
        category=category if category in VALID_CATEGORIES else "fact",
        epic_id=epic_id,
        task_id=task_id,
        repo=repo if repo else "-",
        created=created,
        source=source,
    )

    # JSONL: append one line separated by a newline from the end of the existing text
    new_line = record.to_jsonl_line()
    if existing_text and not existing_text.endswith("\n"):
        new_text = existing_text + "\n" + new_line + "\n"
    else:
        new_text = existing_text + new_line + "\n"

    await atomic_write_text(jsonl_path, new_text)
    return record


def make_content_hash(content: str) -> str:
    """Return the SHA-256 digest of normalised (case/whitespace) content (for duplicate detection).

    E1: both MemoryRecord.content_hash() and append_record use this helper.
    """
    normalized = " ".join(content.strip().lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()

