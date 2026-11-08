"""JRA公式の開催カレンダーから、その日の開催（回・場・日）を得る。"""

from __future__ import annotations

import re
from datetime import date

from ..config import TRACK_CODES


def calendar_url(day: date) -> str:
    return (
        f"https://www.jra.go.jp/keiba/calendar{day.year}/{day.year}/"
        f"{day.month}/{day.month:02d}{day.day:02d}.html"
    )


def parse_meetings(page: str, day: date) -> list[dict]:
    meetings: list[dict] = []
    seen: set[str] = set()
    for kai, track, nichi in re.findall(r"(\d+)回([^\d<\s]+?)(\d+)日", page):
        track = track.strip()
        if track not in TRACK_CODES:
            continue
        meeting_id = f"{day.year % 100:02d}{TRACK_CODES[track]}{int(kai):02d}{int(nichi):02d}"
        if meeting_id in seen:
            continue
        seen.add(meeting_id)
        meetings.append({
            "meeting_id": meeting_id,
            "date": day.isoformat(),
            "kai": int(kai),
            "track": track,
            "nichi": int(nichi),
        })
    return meetings
