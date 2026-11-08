"""集めたデータの整合性チェック。おかしな行を数えて、例を少し見せる。"""

from __future__ import annotations

from . import db

CHECKS = {
    "結果が0頭のレース":
        """SELECT r.race_id FROM races r LEFT JOIN results x ON x.race_id=r.race_id
           WHERE r.has_result=1 GROUP BY r.race_id HAVING COUNT(x.horse_no)=0""",
    "1着がいない、または2頭以上いるレース":
        """SELECT race_id FROM results GROUP BY race_id
           HAVING SUM(CASE WHEN finish=1 THEN 1 ELSE 0 END) <> 1""",
    "着順が重複しているレース（同着を除く誤り）":
        """SELECT race_id FROM (SELECT race_id, finish, COUNT(*) c FROM results WHERE finish IS NOT NULL
           GROUP BY race_id, finish) WHERE c > 2""",
    "完走したのに単勝オッズが無い馬":
        "SELECT race_id || '-' || horse_no FROM results WHERE finish IS NOT NULL AND win_odds IS NULL",
    "単勝の払戻が無いレース":
        """SELECT r.race_id FROM races r WHERE r.has_result=1 AND NOT EXISTS
           (SELECT 1 FROM payouts p WHERE p.race_id=r.race_id AND p.bet_type='単勝')""",
    "距離・芝ダが読めていないレース":
        "SELECT race_id FROM races WHERE has_result=1 AND (distance IS NULL OR surface IS NULL)",
    "馬IDが無い出走":
        "SELECT race_id || '-' || horse_no FROM results WHERE horse_id IS NULL",
    "勝ち馬の単勝オッズと払戻が合わないレース（10円以上の差）":
        """SELECT x.race_id FROM results x JOIN payouts p ON p.race_id=x.race_id AND p.bet_type='単勝'
           AND p.combo = CAST(x.horse_no AS TEXT)
           WHERE x.finish=1 AND x.win_odds IS NOT NULL AND ABS(x.win_odds*100 - p.payout) >= 10""",
}


def run_checks(examples: int = 5) -> dict:
    out: dict = {}
    with db.session() as conn:
        out["レース数"] = conn.execute("SELECT COUNT(*) FROM races WHERE has_result=1").fetchone()[0]
        for name, sql in CHECKS.items():
            rows = [r[0] for r in conn.execute(sql)]
            out[name] = {"件数": len(rows), "例": rows[:examples]}
        # 単勝オッズの逆数の合計は、控除率20%なら 1/0.8 = 1.25 前後になるはず
        sums = [r[0] for r in conn.execute(
            """SELECT SUM(1.0/win_odds) FROM results WHERE win_odds > 0 GROUP BY race_id""")]
        if sums:
            sums.sort()
            out["単勝オッズ逆数の合計（中央値・期待値は約1.25）"] = round(sums[len(sums) // 2], 3)
    return out
