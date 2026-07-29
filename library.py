"""Everything already transcribed: browse, read, search, play back.

The client only ever sends a job id. Paths come from history.jsonl and are
resolved here, so no endpoint can be talked into opening an arbitrary file.
"""

from __future__ import annotations

import json
from pathlib import Path

from config import HISTORY
from transcribe import parse_srt

MAX_ENTRIES = 500
MAX_SEARCH_HITS = 200


def _rows(limit: int = MAX_ENTRIES) -> list[dict]:
    try:
        lines = HISTORY.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows, seen = [], set()
    for line in reversed(lines[-limit * 2:]):  # newest first
        try:
            row = json.loads(line)
        except ValueError:
            continue
        # A file transcribed twice appears twice; keep the most recent run of each id.
        if row.get("id") in seen or not row.get("outputs"):
            continue
        seen.add(row.get("id"))
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def entries() -> list[dict]:
    """Completed transcripts, newest first, with whether the files still exist."""
    out = []
    for row in _rows():
        if row.get("status") != "completed":
            continue
        outputs = row.get("outputs") or {}
        txt, srt = outputs.get("txt", ""), outputs.get("srt", "")
        out.append({
            "id": row["id"],
            "name": Path(row["source"]).name,
            "source": row["source"],
            "language": row.get("language", ""),
            "duration": row.get("duration"),
            "ended_at": row.get("ended_at"),
            "txt": txt,
            "srt": srt,
            "has_text": bool(txt and Path(txt).exists()),
            "has_cues": bool(srt and Path(srt).exists()),
            "has_media": Path(row["source"]).exists(),
        })
    return out


def find(entry_id: str) -> dict | None:
    return next((e for e in entries() if e["id"] == entry_id), None)


def detail(entry_id: str) -> dict | None:
    """One transcript: its cues if an .srt survives, otherwise the plain text."""
    entry = find(entry_id)
    if entry is None:
        return None
    cues = [{"start": s, "end": e, "text": t} for s, e, t in parse_srt(Path(entry["srt"]))] \
        if entry["has_cues"] else []
    text = ""
    if entry["has_text"]:
        try:
            with Path(entry["txt"]).open(encoding="utf-8", errors="replace") as fh:
                text = fh.read(400_000)
        except OSError:
            text = ""
    return {**entry, "cues": cues, "text": text}


def media_path(entry_id: str) -> Path | None:
    """The source file for playback, if it is still where we left it."""
    entry = find(entry_id)
    if entry is None or not entry["has_media"]:
        return None
    return Path(entry["source"])


def search(query: str) -> list[dict]:
    """Substring match across transcripts, with the timestamp of each hit."""
    needle = query.strip().casefold()
    if len(needle) < 2:
        return []
    hits = []
    for entry in entries():
        if not entry["has_cues"]:
            continue
        for start, _end, text in parse_srt(Path(entry["srt"])):
            if needle in text.casefold():
                hits.append({"id": entry["id"], "name": entry["name"],
                             "start": start, "text": text})
                if len(hits) >= MAX_SEARCH_HITS:
                    return hits
    return hits
