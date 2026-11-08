"""開催日のオッズ（単勝・複勝）を、発走までの残り時間に応じた間隔で記録し続ける。

発走までの残り   取得間隔
  90分超          30分
  30〜90分        10分
  10〜30分         5分
  10分以内         1分
  発走後           5分（確定オッズが取れたら終了）

確定オッズだけでは「買う時点のオッズ」で検証できないため、この記録が後で効いてくる。
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Callable

from . import config, db
from .http import BlockedError, NotFoundError, fetch
from .sources import jra, netkeiba, yahoo

log = logging.getLogger(__name__)

INTERVALS = [(10, 1), (30, 5), (90, 10), (24 * 60, 30)]  # (残り分の上限, 間隔分)
AFTER_POST_INTERVAL_MIN = 5
GIVE_UP_AFTER_POST_MIN = 60
FIRST_CAPTURE_BEFORE_MIN = 150
TICK_SEC = 15


def interval_minutes(minutes_to_post: float) -> float:
    if minutes_to_post <= 0:
        return AFTER_POST_INTERVAL_MIN
    for upper, interval in INTERVALS:
        if minutes_to_post <= upper:
            return interval
    return INTERVALS[-1][1]


def post_datetime(day: str, post_time: str) -> datetime:
    hour, minute = (int(x) for x in post_time.split(":"))
    d = date.fromisoformat(day)
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=config.JST)


def build_schedule(conn, day: date) -> list[dict]:
    """その日のレースと発走時刻。DBになければJRAカレンダー→スポーツナビの一覧で作る。"""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM schedule WHERE date=? ORDER BY post_time, race_id", (day.isoformat(),))]
    if rows:
        return rows
    try:
        meetings = jra.parse_meetings(fetch(jra.calendar_url(day)), day)
    except NotFoundError:
        meetings = []
    if not meetings:
        return []
    for meeting in meetings:
        conn.execute(
            "INSERT OR IGNORE INTO meetings (meeting_id, date, kai, track, nichi) VALUES (?,?,?,?,?)",
            (meeting["meeting_id"], meeting["date"], meeting["kai"], meeting["track"], meeting["nichi"]),
        )
        try:
            races = yahoo.parse_race_list(fetch(yahoo.race_list_url(meeting["meeting_id"])))
        except NotFoundError:
            log.warning("レース一覧がまだありません: %s", meeting["meeting_id"])
            continue
        db.upsert(conn, "schedule", [
            {"race_id": r["race_id"], "date": day.isoformat(), "post_time": r["post_time"],
             "meeting_id": meeting["meeting_id"], "race_no": r["race_no"]}
            for r in races
        ])
    conn.commit()
    return [dict(r) for r in conn.execute(
        "SELECT * FROM schedule WHERE date=? ORDER BY post_time, race_id", (day.isoformat(),))]


def snapshot(conn, race: dict, now: datetime) -> tuple[int, bool]:
    """1レース分を記録。戻り値は (保存した行数, 確定オッズか)。"""
    try:
        parsed = netkeiba.parse_odds(fetch(netkeiba.odds_url(race["race_id"]), attempts=2))
        parsed["source"] = "netkeiba"
    except BlockedError:
        raise
    except Exception as error:
        log.info("netkeibaで取れず（%s）。スポーツナビで取得します: %s", error, race["race_id"])
        parsed = yahoo.parse_odds(fetch(yahoo.odds_url(race["race_id"])))
        parsed["source"] = "yahoo"
    minutes_to_post = (post_datetime(race["date"], race["post_time"]) - now).total_seconds() / 60
    stamp = now.isoformat(timespec="seconds")
    rows = [{
        "race_id": race["race_id"], "snapshot_at": stamp, "minutes_to_post": round(minutes_to_post, 2),
        "status": parsed["status"], "official_at": parsed["official_at"], "source": parsed["source"], **row,
    } for row in parsed["rows"]]
    db.upsert(conn, "odds_snapshots", rows)
    conn.commit()
    return len(rows), parsed["status"] == netkeiba.FINAL_STATUS and bool(rows)


def finalized_races(conn, day: date) -> set[str]:
    return {r[0] for r in conn.execute(
        """SELECT DISTINCT s.race_id FROM odds_snapshots s JOIN schedule c ON c.race_id = s.race_id
           WHERE c.date=? AND s.status=?""", (day.isoformat(), netkeiba.FINAL_STATUS))}


def run_day(day: date | None = None, *, max_minutes: float | None = None,
            on_tick: Callable | None = None) -> dict:
    """開催日を1日通して記録する。on_tick(conn, schedule, now) は毎周回呼ばれる（仮想購入用）。"""
    day = day or config.now_jst().date()
    deadline = time.monotonic() + max_minutes * 60 if max_minutes else None
    with db.session() as conn:
        schedule = build_schedule(conn, day)
        if not schedule:
            log.info("%s は開催なし", day)
            return {"date": day.isoformat(), "races": 0}
        log.info("%s 開催 %dレース（最初の発走 %s）", day, len(schedule), schedule[0]["post_time"])
        finalized = finalized_races(conn, day)
        last_capture: dict[str, datetime] = {}
        first_post = min(post_datetime(r["date"], r["post_time"]) for r in schedule)
        start_at = first_post - timedelta(minutes=FIRST_CAPTURE_BEFORE_MIN)
        snapshots = 0
        while True:
            now = config.now_jst()
            active = [r for r in schedule if r["race_id"] not in finalized and
                      now <= post_datetime(r["date"], r["post_time"]) + timedelta(minutes=GIVE_UP_AFTER_POST_MIN)]
            if not active:
                break
            if deadline and time.monotonic() > deadline:
                log.info("時間上限に達したので終了します")
                break
            if now < start_at:
                time.sleep(min(60, (start_at - now).total_seconds()))
                continue
            due = []
            for race in active:
                mtp = (post_datetime(race["date"], race["post_time"]) - now).total_seconds() / 60
                last = last_capture.get(race["race_id"])
                if last is None or (now - last).total_seconds() >= interval_minutes(mtp) * 60 - TICK_SEC:
                    due.append((abs(mtp), race))
            for _, race in sorted(due, key=lambda x: x[0]):
                try:
                    count, final = snapshot(conn, race, config.now_jst())
                except BlockedError:
                    log.error("オッズ取得を拒否されました。記録を止めます。")
                    raise
                except Exception as error:  # 1レースの失敗で全体を止めない
                    log.warning("取得失敗 %s: %s", race["race_id"], error)
                    count, final = 0, False
                last_capture[race["race_id"]] = config.now_jst()
                snapshots += 1
                if final:
                    finalized.add(race["race_id"])
                log.info("%s R%s 発走%s 行数%d%s", race["race_id"], race.get("race_no"),
                         race["post_time"], count, " 確定" if final else "")
            if on_tick is not None:
                try:
                    on_tick(conn, schedule, config.now_jst())
                except Exception:
                    log.exception("仮想購入の処理でエラー（記録は続けます）")
            time.sleep(TICK_SEC)
    return {"date": day.isoformat(), "races": len(schedule), "snapshots": snapshots,
            "finalized": len(finalized)}


def snapshot_once(day: date) -> dict:
    """指定日の全レースを1回だけ記録する（前日夜の発売開始後オッズ用）。"""
    with db.session() as conn:
        schedule = build_schedule(conn, day)
        total = 0
        for race in schedule:
            try:
                count, _ = snapshot(conn, race, config.now_jst())
            except BlockedError:
                raise
            except Exception as error:
                log.warning("取得失敗 %s: %s", race["race_id"], error)
                count = 0
            total += count
    log.info("%s: %dレース、%d行を記録", day, len(schedule), total)
    return {"date": day.isoformat(), "races": len(schedule), "rows": total}
