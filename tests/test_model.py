"""モデル部分のテスト。合成データで「仕組みが正しく動くか」だけを確かめる（勝てるかどうかの検証ではない）。"""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from keibalab import db
from keibalab.features import build_features
from keibalab.model import clogit_probs, fit_clogit, pseudo_r2


def test_clogit_recovers_coefficients():
    rng = np.random.default_rng(0)
    races, size = 3000, 12
    codes = np.repeat(np.arange(races), size)
    X = rng.normal(size=(races * size, 2))
    true = np.array([0.8, -0.4])
    p = clogit_probs(true, X, codes)
    y = np.zeros(len(codes))
    for r in range(races):
        idx = np.arange(r * size, (r + 1) * size)
        y[rng.choice(idx, p=p[idx])] = 1
    est = fit_clogit(X, codes, y)
    assert np.allclose(est, true, atol=0.08)
    assert pseudo_r2(clogit_probs(est, X, codes), y, codes) > 0


def _runs_frame(rows):
    df = pd.DataFrame(rows)
    defaults = {"finish_text": "", "frame": 1, "sex": "牡", "age": 4, "weight": 480, "weight_diff": 0,
                "time_sec": 100.0, "last3f": 35.0, "carried": 56.0, "popularity": 1, "win_odds": 3.0,
                "track": "東京", "race_no": 1, "post_time": "10:00", "surface": "turf", "distance": 1600,
                "going": "良", "class_rank": 2, "handicap": 0, "grade": None, "is_target": 0}
    for key, value in defaults.items():
        if key not in df:
            df[key] = value
    df["is_target"] = df["is_target"].fillna(0)
    df["post_time"] = df["post_time"].fillna("10:00")
    df["date"] = pd.to_datetime(df["date"])
    return df


def test_features_use_only_the_past():
    rows = []
    # 馬A は1戦目1着、2戦目は対象レース（結果なし）。騎手Jは同じ日に別のレースで勝つ
    for horse_no, horse, jockey, finish in [(1, "A", "J", 1), (2, "B", "K", 2)]:
        rows.append({"race_id": "2405010101", "horse_no": horse_no, "horse_id": horse, "jockey_id": jockey,
                     "trainer_id": "T", "finish": finish, "date": "2024-01-06"})
    for horse_no, horse, jockey, finish in [(1, "C", "J", 1), (2, "D", "K", 2)]:
        rows.append({"race_id": "2405010201", "horse_no": horse_no, "horse_id": horse, "jockey_id": jockey,
                     "trainer_id": "T", "finish": finish, "date": "2024-01-13"})
    for horse_no, horse, jockey in [(1, "A", "J"), (2, "B", "K")]:
        rows.append({"race_id": "2405010202", "horse_no": horse_no, "horse_id": horse, "jockey_id": jockey,
                     "trainer_id": "T", "finish": None, "date": "2024-01-13", "post_time": "11:00",
                     "is_target": 1, "win_odds": None, "popularity": None, "time_sec": None, "last3f": None})
    feats = build_features(_runs_frame(rows)).set_index(["race_id", "horse_no"])
    target_a = feats.loc[("2405010202", 1)]
    assert target_a["n_starts"] == 1
    assert target_a["prev_finish_pct"] == 0.0
    assert target_a["days_since"] == 7
    # 騎手Jの成績は「前の日まで」の1戦1勝だけ（同じ日の2405010201の勝ちは含めない）
    assert target_a["jockey_starts"] == 1
    first_c = feats.loc[("2405010201", 1)]
    assert first_c["n_starts"] == 0 and np.isnan(first_c["prev_finish_pct"])
    assert first_c["jockey_starts"] == 1  # 1/6 の1戦だけ（その日の自分の結果は入らない）


def _synthetic_db(path, n_days=110, seed=1):
    """能力のある馬が勝ちやすく、オッズは能力をある程度だけ反映する、という作り物の世界。"""
    rng = np.random.default_rng(seed)
    horses = {f"H{i:04d}": rng.normal() for i in range(900)}
    jockeys = [f"J{i:02d}" for i in range(40)]
    ids = list(horses)
    conn = db.connect(path)
    start = date(2023, 1, 7)
    races, results, payouts = [], [], []
    for d in range(n_days):
        day = start + timedelta(days=7 * (d // 2) + (d % 2))
        for rno in range(1, 13):
            race_id = f"{day.year % 100:02d}05{1 + d // 16:02d}{d % 16 + 1:02d}{rno:02d}"
            field = rng.choice(ids, size=12, replace=False)
            ability = np.array([horses[h] for h in field])
            strength = np.exp(1.2 * ability)
            p_true = strength / strength.sum()
            winner = rng.choice(12, p=p_true)
            order = [winner] + [i for i in rng.permutation(12) if i != winner]
            noisy = np.exp(0.7 * ability + rng.normal(scale=0.5, size=12))
            q = noisy / noisy.sum()
            odds = np.round(np.maximum(0.8 / q, 1.1), 1)
            races.append({"race_id": race_id, "meeting_id": race_id[:8], "date": day.isoformat(), "track": "東京",
                          "race_no": rno, "post_time": f"{9 + rno // 2:02d}:{(rno % 2) * 30:02d}",
                          "surface": "turf" if rno % 2 else "dirt", "distance": 1200 + 200 * (rno % 5),
                          "going": "良", "class_rank": 2, "handicap": 0, "n_runners": 12, "has_result": 1})
            for pos, idx in enumerate(order, start=1):
                results.append({"race_id": race_id, "horse_no": int(idx) + 1, "finish": pos, "finish_text": str(pos),
                                "frame": int(idx) // 2 + 1, "horse_id": field[idx], "sex": "牡", "age": 4,
                                "weight": 480, "weight_diff": 0, "time_sec": 90 + pos * 0.2, "last3f": 35 + pos * 0.1,
                                "jockey_id": jockeys[(int(idx) + rno) % 40], "carried": 56.0,
                                "popularity": int(np.argsort(np.argsort(odds))[idx]) + 1,
                                "win_odds": float(odds[idx]), "trainer_id": "T1"})
            payouts.append({"race_id": race_id, "bet_type": "単勝", "combo": str(int(winner) + 1),
                            "payout": int(odds[winner] * 100), "popularity": 1})
    db.upsert(conn, "races", races)
    db.upsert(conn, "results", results, db.table_columns(conn, "results"))
    db.upsert(conn, "payouts", payouts)
    conn.commit()
    conn.close()


@pytest.mark.slow
def test_pipeline_end_to_end(tmp_path, monkeypatch):
    lgb = pytest.importorskip("lightgbm")  # noqa: F841
    from keibalab import backtest, config, model

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "keiba.sqlite")
    monkeypatch.setattr(config, "MODEL_DIR", tmp_path / "models")
    monkeypatch.setattr(config, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(model, "WARMUP_MONTHS", 1)
    _synthetic_db(tmp_path / "keiba.sqlite")
    out = backtest.run_backtest(first_test="2023-12-01", test_months=2, valid_months=2)
    r2 = out["pseudo_r2_test_overall"]
    # 作り物の世界では、過去成績に能力が出るので「市場＋モデル」が「市場だけ」を上回るはず
    assert r2["combined"] > r2["market"]
    assert list((tmp_path / "reports").glob("backtest_*.md"))
    trained = model.train_and_save(valid_from="2023-11-01")
    assert trained["stage2"]["coef_combined"][0] > 0
    bundle, booster = model.load_bundle()
    assert bundle["features"] and booster.num_trees() > 0
