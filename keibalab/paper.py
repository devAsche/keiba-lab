"""仮想購入（ペーパートレード）。お金は動かさず、発走前に買い目を時刻つきで台帳に残す。

開催日の記録ループ（odds_recorder.run_day）から毎周回呼ばれ、
発走 decide_minutes_before 分前〜 lead_minutes 分前の間に、各レース1回だけ判断する。
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime

import numpy as np
import pandas as pd

from . import config, db
from .features import build_features, load_runs, market_probability
from .http import fetch
from .model import Stage2, clogit_probs, lgbm_logit, load_bundle
from .odds_recorder import post_datetime, snapshot
from .sources import yahoo

log = logging.getLogger(__name__)

DECISIONS_SQL = """
CREATE TABLE IF NOT EXISTS paper_decisions (
    race_id TEXT PRIMARY KEY, decided_at TEXT, minutes_to_post REAL,
    n_candidates INTEGER, n_bets INTEGER, note TEXT, model_version TEXT
);
"""
LEDGER_FIELDS = ["decided_at", "race_id", "post_time", "minutes_to_post", "horse_no", "horse_name",
                 "odds_at_decision", "p_model", "p_market", "p_final", "ev", "stake", "model_version"]


def current_bankroll(conn, initial: float) -> float:
    row = conn.execute("SELECT COALESCE(SUM(payout - stake), 0) FROM paper_bets WHERE settled=1").fetchone()
    return initial + float(row[0])


def latest_win_odds(conn, race_id: str) -> tuple[dict[int, float], str | None]:
    row = conn.execute("SELECT MAX(snapshot_at) FROM odds_snapshots WHERE race_id=? AND bet_type='win'",
                       (race_id,)).fetchone()
    if not row or not row[0]:
        return {}, None
    odds = {r["horse_no"]: r["odds"] for r in conn.execute(
        "SELECT horse_no, odds FROM odds_snapshots WHERE race_id=? AND bet_type='win' AND snapshot_at=?",
        (race_id, row[0]))}
    return odds, row[0]


def kelly_stake(p: float, odds: float, bankroll: float, rules: dict) -> float:
    f = rules["kelly_fraction"] * (p * odds - 1) / (odds - 1)
    f = min(max(f, 0.0), rules["max_stake_frac"])
    stake = np.floor(f * bankroll / 100) * 100
    return float(max(stake, 100.0)) if f > 0 else 0.0


def decide_race(conn, race: dict, now: datetime, bundle: dict, booster) -> dict:
    rules = bundle["betting"]
    race_id = race["race_id"]
    mtp = (post_datetime(race["date"], race["post_time"]) - now).total_seconds() / 60

    info, entries = yahoo.parse_denma(fetch(yahoo.denma_url(race_id)), race_id)
    stamp = config.now_jst().isoformat(timespec="seconds")
    existing = conn.execute("SELECT has_result FROM races WHERE race_id=?", (race_id,)).fetchone()
    if not existing:
        info.update({"meeting_id": race.get("meeting_id"), "race_no": race.get("race_no"),
                     "track": config.TRACK_NAMES.get(race_id[2:4]), "has_result": 0, "fetched_at": stamp,
                     "date": info.get("date") or race["date"], "post_time": info.get("post_time") or race["post_time"]})
        db.upsert(conn, "races", [info], [c for c in db.table_columns(conn, "races") if c in info])
    for entry in entries:
        entry["fetched_at"] = stamp
    db.upsert(conn, "entries", entries)
    conn.commit()

    odds, snap_at = latest_win_odds(conn, race_id)
    stale = snap_at is None or (now - datetime.fromisoformat(snap_at)).total_seconds() > 180
    if stale:
        snapshot(conn, race, config.now_jst())
        odds, snap_at = latest_win_odds(conn, race_id)

    feats = build_features(load_runs(conn, [race_id]))
    target = feats[(feats["is_target"] == 1) & (feats["race_id"] == race_id)].copy()
    note = ""
    bets: list[dict] = []
    if target.empty:
        note = "出走馬を読めず"
    elif target["surface"].iloc[0] not in ("turf", "dirt") or len(target) < 5:
        note = "対象外（障害戦または少頭数）"
    else:
        target["odds_dec"] = target["horse_no"].astype(int).map(odds)
        target = target[target["odds_dec"].notna()]
        if len(target) < 5:
            note = "オッズが足りない"
        else:
            target["q_market"] = market_probability(target["odds_dec"], target["race_id"])
            logit = lgbm_logit(booster, target)
            codes = np.zeros(len(target), dtype=int)
            stage2 = Stage2(**bundle["stage2"])
            lq = np.log(target["q_market"].to_numpy())
            p_final = clogit_probs(np.array(stage2.coef_combined), np.column_stack([logit, lq]), codes)
            p_model = clogit_probs(np.array(stage2.coef_model), logit[:, None], codes)
            bankroll = current_bankroll(conn, rules["virtual_bankroll"])
            for row, pf, pm in zip(target.itertuples(), p_final, p_model):
                ev = pf * row.odds_dec
                if ev < rules["ev_min"] or row.odds_dec > rules["max_odds"] or pf < rules["min_p"]:
                    continue
                stake = kelly_stake(pf, row.odds_dec, bankroll, rules)
                if stake <= 0:
                    continue
                name = next((e["horse_name"] for e in entries if e["horse_no"] == row.horse_no), "")
                bets.append({
                    "decided_at": stamp, "race_id": race_id, "post_time": race["post_time"],
                    "minutes_to_post": round(mtp, 2), "horse_no": int(row.horse_no), "horse_name": name,
                    "bet_type": "win", "odds_at_decision": float(row.odds_dec), "p_model": round(float(pm), 5),
                    "p_market": round(float(row.q_market), 5), "p_final": round(float(pf), 5),
                    "ev": round(float(ev), 4), "stake": stake, "model_version": bundle["version"],
                })
    if bets:
        db.upsert(conn, "paper_bets", bets)
        ledger = config.DATA_DIR / "ledger"
        ledger.mkdir(parents=True, exist_ok=True)
        path = ledger / "paper_bets.csv"
        new = not path.exists()
        with path.open("a", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=LEDGER_FIELDS, extrasaction="ignore")
            if new:
                writer.writeheader()
            writer.writerows(bets)
    conn.execute(
        "INSERT OR REPLACE INTO paper_decisions VALUES (?,?,?,?,?,?,?)",
        (race_id, stamp, round(mtp, 2), int(len(target)), len(bets), note, bundle["version"]),
    )
    conn.commit()
    log.info("仮想購入 %s: 候補%d頭 → %d点 %s", race_id, len(target), len(bets), note)
    return {"race_id": race_id, "bets": len(bets), "note": note}


def make_hook():
    bundle, booster = load_bundle()
    rules = bundle["betting"]

    def hook(conn, schedule, now):
        conn.executescript(DECISIONS_SQL)
        decided = {r[0] for r in conn.execute("SELECT race_id FROM paper_decisions")}
        for race in schedule:
            if race["race_id"] in decided:
                continue
            mtp = (post_datetime(race["date"], race["post_time"]) - now).total_seconds() / 60
            if rules["lead_minutes"] < mtp <= rules["decide_minutes_before"]:
                try:
                    decide_race(conn, race, now, bundle, booster)
                except Exception:
                    log.exception("仮想購入の判断に失敗: %s", race["race_id"])
                    conn.execute("INSERT OR REPLACE INTO paper_decisions (race_id, decided_at, note) VALUES (?,?,?)",
                                 (race["race_id"], config.now_jst().isoformat(timespec="seconds"), "エラー"))
                    conn.commit()

    log.info("仮想購入を有効にしました（モデル %s、期待値%s以上）", bundle["version"], rules["ev_min"])
    return hook


def settle() -> dict:
    """結果が出たレースの仮想購入を精算する（単勝の払戻表を使う）。"""
    with db.session() as conn:
        rows = conn.execute(
            """SELECT b.bet_id, b.stake, b.race_id, b.horse_no, x.finish, x.win_odds,
                      (SELECT payout FROM payouts p WHERE p.race_id=b.race_id AND p.bet_type='単勝'
                        AND p.combo=CAST(b.horse_no AS TEXT)) AS tansho
               FROM paper_bets b JOIN races r ON r.race_id=b.race_id AND r.has_result=1
               LEFT JOIN results x ON x.race_id=b.race_id AND x.horse_no=b.horse_no
               WHERE b.settled=0""").fetchall()
        for row in rows:
            if row["finish"] is None and row["win_odds"] is None:
                payout = row["stake"]  # 取消・除外は返還
            elif row["tansho"]:
                payout = row["stake"] * row["tansho"] / 100
            else:
                payout = 0.0
            conn.execute("UPDATE paper_bets SET settled=1, final_odds=?, payout=? WHERE bet_id=?",
                         (row["win_odds"], payout, row["bet_id"]))
    return {"settled": len(rows)}


def report() -> dict:
    with db.session() as conn:
        df = pd.read_sql_query("SELECT * FROM paper_bets", conn)
    if df.empty:
        return {"bets": 0, "note": "まだ仮想購入はありません"}
    done = df[df["settled"] == 1].copy()
    out = {"bets_total": int(len(df)), "settled": int(len(done))}
    if not done.empty:
        done["won"] = done["payout"] > done["stake"]
        flat_ret = np.where(done["won"], done["final_odds"], 0.0)
        out.update({
            "stake_sum": float(done["stake"].sum()),
            "payout_sum": round(float(done["payout"].sum()), 0),
            "roi_kelly": round(float(done["payout"].sum() / done["stake"].sum()), 4),
            "roi_flat100": round(float(flat_ret.mean()), 4),
            "se_flat100": round(float(flat_ret.std(ddof=1) / np.sqrt(len(flat_ret))), 4) if len(done) > 1 else None,
            "hits": int(done["won"].sum()),
            "avg_odds_at_decision": round(float(done["odds_at_decision"].mean()), 2),
            "avg_odds_final": round(float(done["final_odds"].mean()), 2),
        })
        done["week"] = pd.to_datetime(done["decided_at"].str[:10]).dt.to_period("W").astype(str)
        weekly = done.groupby("week").agg(bets=("bet_id", "size"), stake=("stake", "sum"), payout=("payout", "sum"))
        out["weekly"] = {k: {"bets": int(v.bets), "roi": round(v.payout / v.stake, 3)}
                         for k, v in weekly.iterrows()}
    return out
