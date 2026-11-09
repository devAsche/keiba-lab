"""過去のレース結果を集める。途中で止めても、次回は続きから再開する。

流れ: 月間日程 → 開催ごとのレース一覧 → 各レースの結果ページ
保存先: meetings / races / results / payouts テーブル
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime

from . import config, db
from .http import BlockedError, NotFoundError, fetch
from .sources import yahoo

log = logging.getLogger(__name__)


def month_range(start: str, end: str) -> list[tuple[int, int]]:
    sy, sm = (int(x) for x in start.split("-"))
    ey, em = (int(x) for x in end.split("-"))
    months = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


def refresh_schedule(conn, months: list[tuple[int, int]], *, today: date) -> None:
    """未取得の月と、直近2か月（新しい開催が増える）の日程を取りに行く。"""
    recent = {(today.year, today.month)}
    prev = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    recent.add(prev)
    fetched = {row["ym"] for row in conn.execute("SELECT ym FROM months")}
    for year, month in months:
        ym = f"{year}-{month:02d}"
        if ym in fetched and (year, month) not in recent:
            continue
        if (year, month) > (today.year, today.month):
            continue
        page = fetch(yahoo.monthly_schedule_url(year, month))
        meetings = yahoo.parse_monthly_schedule(page, year, month)
        for meeting in meetings:
            conn.execute(
                "INSERT OR IGNORE INTO meetings (meeting_id, date, kai, track, nichi) VALUES (?,?,?,?,?)",
                (meeting["meeting_id"], meeting["date"], meeting["kai"], meeting["track"], meeting["nichi"]),
            )
        conn.execute(
            "INSERT OR REPLACE INTO months (ym, fetched_at) VALUES (?, ?)",
            (ym, config.now_jst().isoformat(timespec="seconds")),
        )
        conn.commit()
        log.info("日程 %s: 開催 %d件", ym, len(meetings))


def collect_meeting(conn, meeting: dict) -> bool:
    """1開催分を集める。全レースの結果がそろえば True。"""
    races = yahoo.parse_race_list(fetch(yahoo.race_list_url(meeting["meeting_id"])))
    if not races:
        log.warning("レース一覧が空: %s", meeting["meeting_id"])
        return False
    db.upsert(conn, "schedule", [
        {"race_id": r["race_id"], "date": meeting["date"], "post_time": r["post_time"],
         "meeting_id": meeting["meeting_id"], "race_no": r["race_no"]}
        for r in races
    ])
    complete = True
    for race in races:
        row = conn.execute("SELECT has_result, cancelled FROM races WHERE race_id=?", (race["race_id"],)).fetchone()
        if row and (row["has_result"] or row["cancelled"]):
            continue
        try:
            page = fetch(yahoo.result_url(race["race_id"]))
        except NotFoundError:
            log.warning("結果ページなし（中止の可能性）: %s", race["race_id"])
            db.upsert(conn, "races", [{
                "race_id": race["race_id"], "meeting_id": meeting["meeting_id"], "date": meeting["date"],
                "track": meeting["track"], "race_no": race["race_no"], "post_time": race["post_time"],
                "name": "NOT_FOUND", "has_result": 0,
                "fetched_at": config.now_jst().isoformat(timespec="seconds"),
            }])
            conn.commit()
            continue
        info, runners, payouts = yahoo.parse_result(page, race["race_id"])
        has_result = any(r["finish"] is not None for r in runners)
        info.update({
            "meeting_id": meeting["meeting_id"],
            "track": meeting["track"],
            "race_no": race["race_no"],
            "post_time": info.get("post_time") or race["post_time"],
            "date": info.get("date") or meeting["date"],
            "has_result": 1 if has_result else 0,
            "fetched_at": config.now_jst().isoformat(timespec="seconds"),
        })
        db.upsert(conn, "races", [info], [c for c in db.table_columns(conn, "races") if c in info])
        conn.execute("DELETE FROM results WHERE race_id=?", (race["race_id"],))
        db.upsert(conn, "results", runners, [c for c in db.table_columns(conn, "results")])
        db.upsert(conn, "payouts", payouts)
        conn.commit()
        if not has_result:
            if info.get("cancelled"):
                log.info("中止になったレース: %s", race["race_id"])
                continue
            complete = False
            log.warning("結果が未確定または解析できず: %s（%d頭）", race["race_id"], len(runners))
    return complete


def backfill(start: str = "2023-01", end: str | None = None, *, max_minutes: float | None = None,
             newest_first: bool = True) -> dict:
    today = config.now_jst().date()
    end = end or f"{today.year}-{today.month:02d}"
    deadline = time.monotonic() + max_minutes * 60 if max_minutes else None
    done = 0
    with db.session() as conn:
        refresh_schedule(conn, month_range(start, end), today=today)
        order = "DESC" if newest_first else "ASC"
        pending = [dict(r) for r in conn.execute(
            f"SELECT * FROM meetings WHERE complete=0 AND date < ? AND date >= ? ORDER BY date {order}, meeting_id",
            (today.isoformat(), f"{start}-01"),
        )]
        log.info("未収集の開催: %d件", len(pending))
        for meeting in pending:
            if deadline and time.monotonic() > deadline:
                log.info("時間切れ。次回はここから再開します。")
                break
            try:
                ok = collect_meeting(conn, meeting)
            except BlockedError:
                log.error("アクセスを拒否されました。収集を止めます。時間をおいて再実行してください。")
                raise
            except Exception as error:  # 一時的なサーバーエラーなどは、その開催だけ飛ばして次回に回す
                conn.rollback()
                log.warning("開催 %s を後回しにします: %s", meeting["meeting_id"], error)
                continue
            if ok:
                conn.execute("UPDATE meetings SET complete=1 WHERE meeting_id=?", (meeting["meeting_id"],))
                conn.commit()
                done += 1
                log.info("開催 %s %s%d日 完了", meeting["date"], meeting["track"], meeting["nichi"])
        remaining = conn.execute(
            "SELECT COUNT(*) FROM meetings WHERE complete=0 AND date < ? AND date >= ?",
            (today.isoformat(), f"{start}-01"),
        ).fetchone()[0]
    return {"meetings_done": done, "meetings_remaining": remaining}


def status() -> dict:
    with db.session() as conn:
        out = {}
        out["meetings_total"] = conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]
        out["meetings_complete"] = conn.execute("SELECT COUNT(*) FROM meetings WHERE complete=1").fetchone()[0]
        out["races_with_result"] = conn.execute("SELECT COUNT(*) FROM races WHERE has_result=1").fetchone()[0]
        out["date_range"] = tuple(conn.execute(
            "SELECT MIN(date), MAX(date) FROM races WHERE has_result=1").fetchone())
        out["odds_snapshot_rows"] = conn.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0]
        out["odds_snapshot_races"] = conn.execute(
            "SELECT COUNT(DISTINCT race_id) FROM odds_snapshots").fetchone()[0]
        out["paper_bets"] = conn.execute("SELECT COUNT(*) FROM paper_bets").fetchone()[0]
        out["checked_at"] = datetime.now(config.JST).isoformat(timespec="seconds")
    return out
