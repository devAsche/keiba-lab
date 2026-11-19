"""血統（父・母父）を集める。

結果ページには血統が無いので、出馬表ページを取りに行く。血統は馬ごとに1回わかれば十分なので、
「まだ血統が分からない馬がいるレース」だけを新しい順に取りに行く（全レースを取るより大幅に少なく済む）。
"""

from __future__ import annotations

import logging
import time

from . import config, db
from .http import BlockedError, NotFoundError, fetch
from .sources import yahoo

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS horse_pedigree (
    horse_id TEXT PRIMARY KEY, sire TEXT, damsire TEXT, source_race TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS pedigree_tried (race_id TEXT PRIMARY KEY, tried_at TEXT, found INTEGER);
"""


def collect(max_minutes: float | None = None, since: str | None = None) -> dict:
    deadline = time.monotonic() + max_minutes * 60 if max_minutes else None
    done = pages = 0
    with db.session() as conn:
        conn.executescript(SCHEMA)
        params: list = []
        where = "r.has_result = 1"
        if since:
            where += " AND r.date >= ?"
            params.append(since)
        races = [r[0] for r in conn.execute(
            f"""SELECT r.race_id FROM races r
                WHERE {where}
                  AND NOT EXISTS (SELECT 1 FROM pedigree_tried t WHERE t.race_id = r.race_id)
                  AND EXISTS (SELECT 1 FROM results x LEFT JOIN horse_pedigree p ON p.horse_id = x.horse_id
                              WHERE x.race_id = r.race_id AND x.horse_id IS NOT NULL AND p.horse_id IS NULL)
                ORDER BY r.date DESC, r.race_id""", params)]
        log.info("血統が未取得の馬がいるレース: %d", len(races))
        for race_id in races:
            if deadline and time.monotonic() > deadline:
                log.info("時間切れ。次回はここから再開します。")
                break
            missing = conn.execute(
                """SELECT COUNT(*) FROM results x LEFT JOIN horse_pedigree p ON p.horse_id = x.horse_id
                   WHERE x.race_id = ? AND x.horse_id IS NOT NULL AND p.horse_id IS NULL""", (race_id,)).fetchone()[0]
            if missing == 0:
                continue  # 先に取った別のレースで埋まった
            stamp = config.now_jst().isoformat(timespec="seconds")
            try:
                _, entries = yahoo.parse_denma(fetch(yahoo.denma_url(race_id)), race_id)
            except BlockedError:
                log.error("アクセスを拒否されました。止めます。")
                raise
            except NotFoundError:
                entries = []
            except Exception as error:
                log.warning("後回し %s: %s", race_id, error)
                continue
            pages += 1
            rows = [{"horse_id": e["horse_id"], "sire": e["sire"], "damsire": e["damsire"],
                     "source_race": race_id, "fetched_at": stamp}
                    for e in entries if e.get("horse_id") and (e.get("sire") or e.get("damsire"))]
            conn.executemany(
                "INSERT OR IGNORE INTO horse_pedigree VALUES (:horse_id, :sire, :damsire, :source_race, :fetched_at)",
                rows)
            conn.execute("INSERT OR REPLACE INTO pedigree_tried VALUES (?,?,?)", (race_id, stamp, len(rows)))
            conn.commit()
            done += len(rows)
            if pages % 200 == 0:
                known = conn.execute("SELECT COUNT(*) FROM horse_pedigree").fetchone()[0]
                log.info("血統: %dページ取得、判明した馬 %d頭", pages, known)
        known = conn.execute("SELECT COUNT(*) FROM horse_pedigree").fetchone()[0]
        total = conn.execute("SELECT COUNT(DISTINCT horse_id) FROM results WHERE horse_id IS NOT NULL").fetchone()[0]
    return {"pages": pages, "horses_known": known, "horses_total": total}
