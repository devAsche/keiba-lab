"""予測モデル。

第一段階: LightGBM で各馬の「勝つ確率っぽい値」を出す（当日のオッズは使わない）
第二段階: 条件付きロジットで、第一段階の値と大衆の確率（オッズの逆数）を組み合わせる
          p_i = exp(a*logit(p_lgbm_i) + b*log(q_i)) / Σ_j exp(...)   （レースごとに正規化）
          Benter (1994) と同じ考え方。b が大きく a が0に近ければ「市場以上の情報は無い」という意味。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd
from scipy.optimize import minimize

from . import config, db
from .features import FEATURES, build_features, load_runs, market_probability, modeling_frame

log = logging.getLogger(__name__)

LGB_PARAMS = {
    "objective": "binary",
    "learning_rate": 0.03,
    "num_leaves": 31,
    "min_data_in_leaf": 200,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 5.0,
    "verbose": -1,
    "seed": 42,
}
WARMUP_MONTHS = 6  # 収集開始直後は過去成績が薄いので学習に使わない

DEFAULT_BETTING = {
    "ev_min": 1.15,          # 期待値（確率×オッズ）がこれ以上の馬だけ買う
    "max_odds": 50.0,        # 大穴は買わない（本命-大穴バイアス）
    "min_p": 0.02,
    "kelly_fraction": 0.25,  # 1/4ケリー
    "max_stake_frac": 0.02,  # 1点あたり資金の2%まで
    "virtual_bankroll": 100000,
    "decide_minutes_before": 9,
    "lead_minutes": 4,       # 発走この分前より後には決めない
}


# ------------------------------------------------------------ 条件付きロジット
def _race_codes(race_ids: pd.Series) -> np.ndarray:
    return pd.factorize(race_ids)[0]


def clogit_probs(coef: np.ndarray, X: np.ndarray, codes: np.ndarray) -> np.ndarray:
    u = X @ coef
    u = u - pd.Series(u).groupby(codes).transform("max").to_numpy()
    e = np.exp(u)
    return e / pd.Series(e).groupby(codes).transform("sum").to_numpy()


def fit_clogit(X: np.ndarray, codes: np.ndarray, y: np.ndarray, init: np.ndarray | None = None) -> np.ndarray:
    """レースごとに1頭だけが勝つ前提の多項ロジットを最尤推定する。"""
    k = X.shape[1]
    n_groups = codes.max() + 1

    def nll(coef):
        u = X @ coef
        m = np.zeros(n_groups)
        np.maximum.at(m, codes, u)
        e = np.exp(u - m[codes])
        s = np.zeros(n_groups)
        np.add.at(s, codes, e)
        logp = u - m[codes] - np.log(s[codes])
        p = e / s[codes]
        grad_u = y - p
        g = -(X * grad_u[:, None]).sum(axis=0)
        return -(logp * y).sum(), g

    result = minimize(nll, init if init is not None else np.ones(k) * 0.5, jac=True, method="L-BFGS-B")
    return result.x


def log_likelihood(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.log(np.clip(p[y == 1], 1e-12, None)).sum())


def pseudo_r2(p: np.ndarray, y: np.ndarray, codes: np.ndarray) -> float:
    """Benter と同じ定義: 1 - LL(モデル) / LL(全馬同確率)。"""
    n = pd.Series(np.ones_like(y)).groupby(codes).transform("sum").to_numpy()
    ll0 = log_likelihood(1.0 / n, y)
    return 1.0 - log_likelihood(p, y) / ll0


# ------------------------------------------------------------ データの準備
def winner_races_only(df: pd.DataFrame) -> pd.DataFrame:
    """勝ち馬がちょうど1頭いて、全馬にオッズがあるレースだけ（同着などを除く）。"""
    wins = df.groupby("race_id")["won"].transform("sum")
    odds_ok = df.groupby("race_id")["win_odds"].transform(lambda s: s.notna().all())
    return df[(wins == 1) & odds_ok].copy()


def decision_odds(conn, frame: pd.DataFrame, lead_minutes: float) -> tuple[pd.Series, pd.Series]:
    """買う時点のオッズ。記録があれば発走 lead 分前より前の最後の単勝オッズ、なければ確定オッズ。"""
    snaps = pd.read_sql_query(
        """SELECT race_id, horse_no, odds, snapshot_at FROM odds_snapshots
           WHERE bet_type='win' AND minutes_to_post >= ?""", conn, params=[lead_minutes])
    out = frame["win_odds"].copy()
    used = pd.Series(False, index=frame.index)
    if not snaps.empty:
        snaps = snaps.sort_values("snapshot_at").groupby(["race_id", "horse_no"]).tail(1)
        snap_map = dict(zip(snaps["race_id"] + "-" + snaps["horse_no"].astype(str), snaps["odds"]))
        has = (frame["race_id"] + "-" + frame["horse_no"].astype(int).astype(str)).map(snap_map)
        used = has.notna()
        out[used] = has[used]
    return out, used


def prepare(conn=None) -> pd.DataFrame:
    """学習・検証用の表。odds_dec（買う時点のオッズ）から q_market（大衆の確率）を作る。"""
    own = conn is None
    conn = conn or db.connect()
    try:
        runs = load_runs(conn)
        feats = build_features(runs)
        frame = modeling_frame(feats[feats["is_target"] == 0])
        frame = winner_races_only(frame)
        start = frame["date"].min() + pd.DateOffset(months=WARMUP_MONTHS)
        frame = frame[frame["date"] >= start].copy()
        frame["odds_dec"], frame["from_snapshot"] = decision_odds(conn, frame, DEFAULT_BETTING["lead_minutes"])
    finally:
        if own:
            conn.close()
    frame["q_market"] = market_probability(frame["odds_dec"], frame["race_id"])
    return frame


# ------------------------------------------------------------ 第一段階
def train_lgbm(train: pd.DataFrame, num_rounds: int | None = None):
    import lightgbm as lgb

    train = train.sort_values("date")
    if num_rounds is None:
        cut = train["date"].quantile(0.85)
        fit, val = train[train["date"] < cut], train[train["date"] >= cut]
        booster = lgb.train(
            LGB_PARAMS, lgb.Dataset(fit[FEATURES], fit["won"]), num_boost_round=3000,
            valid_sets=[lgb.Dataset(val[FEATURES], val["won"])],
            callbacks=[lgb.early_stopping(100, verbose=False)],
        )
        num_rounds = max(50, booster.best_iteration or 300)
    booster = lgb.train(LGB_PARAMS, lgb.Dataset(train[FEATURES], train["won"]), num_boost_round=num_rounds)
    return booster, num_rounds


def lgbm_logit(booster, df: pd.DataFrame) -> np.ndarray:
    p = np.clip(booster.predict(df[FEATURES]), 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


@dataclass
class Stage2:
    coef_combined: list[float] = field(default_factory=list)   # [a(モデル), b(市場)]
    coef_model: list[float] = field(default_factory=list)      # [a]
    coef_market: list[float] = field(default_factory=list)     # [b]


def fit_stage2(df: pd.DataFrame, logit: np.ndarray) -> Stage2:
    codes = _race_codes(df["race_id"])
    y = df["won"].to_numpy()
    lq = np.log(df["q_market"].to_numpy())
    combined = fit_clogit(np.column_stack([logit, lq]), codes, y, init=np.array([0.3, 1.0]))
    model_only = fit_clogit(logit[:, None], codes, y, init=np.array([1.0]))
    market_only = fit_clogit(lq[:, None], codes, y, init=np.array([1.0]))
    return Stage2(list(map(float, combined)), list(map(float, model_only)), list(map(float, market_only)))


def stage2_probs(stage2: Stage2, df: pd.DataFrame, logit: np.ndarray) -> dict[str, np.ndarray]:
    codes = _race_codes(df["race_id"])
    lq = np.log(df["q_market"].to_numpy())
    return {
        "combined": clogit_probs(np.array(stage2.coef_combined), np.column_stack([logit, lq]), codes),
        "model": clogit_probs(np.array(stage2.coef_model), logit[:, None], codes),
        "market": clogit_probs(np.array(stage2.coef_market), lq[:, None], codes),
        "market_raw": df["q_market"].to_numpy(),
    }


def r2_table(df: pd.DataFrame, probs: dict[str, np.ndarray]) -> dict[str, float]:
    codes = _race_codes(df["race_id"])
    y = df["won"].to_numpy()
    return {name: round(pseudo_r2(p, y, codes), 5) for name, p in probs.items()}


# ------------------------------------------------------------ 学習して保存
def train_and_save(valid_from: str | None = None) -> dict:
    frame = prepare()
    if frame.empty:
        raise RuntimeError("学習に使えるデータがありません。先に backfill を進めてください。")
    end = frame["date"].max()
    valid_start = pd.Timestamp(valid_from) if valid_from else end - pd.DateOffset(months=6)
    fund = frame[frame["date"] < valid_start]
    valid = frame[frame["date"] >= valid_start]
    if fund["race_id"].nunique() < 500 or valid["race_id"].nunique() < 200:
        raise RuntimeError(f"データが少なすぎます（学習 {fund['race_id'].nunique()} レース、"
                           f"検証 {valid['race_id'].nunique()} レース）。収集が進んでから学習してください。")
    booster_a, rounds = train_lgbm(fund)
    stage2 = fit_stage2(valid, lgbm_logit(booster_a, valid))
    r2 = r2_table(valid, stage2_probs(stage2, valid, lgbm_logit(booster_a, valid)))
    # 本番用: 全期間で同じ回数だけ学び直す（第二段階の係数は検証期間で決めたものを使う）
    booster_b, _ = train_lgbm(frame, num_rounds=rounds)
    version = config.now_jst().strftime("%Y%m%d-%H%M")
    bundle = {
        "version": version,
        "trained_at": datetime.now(config.JST).isoformat(timespec="seconds"),
        "data_until": end.date().isoformat(),
        "valid_from": valid_start.date().isoformat(),
        "num_rounds": rounds,
        "features": FEATURES,
        "stage2": stage2.__dict__,
        "valid_pseudo_r2": r2,
        "betting": dict(DEFAULT_BETTING),
    }
    config.ensure_dirs()
    (config.MODEL_DIR / "lgbm.txt").write_text(booster_b.model_to_string(), encoding="utf-8")
    (config.MODEL_DIR / "bundle.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("モデルを保存しました: %s", version)
    return {k: bundle[k] for k in ("version", "data_until", "valid_from", "num_rounds", "stage2", "valid_pseudo_r2")}


def load_bundle():
    import lightgbm as lgb

    path = config.MODEL_DIR / "bundle.json"
    if not path.exists():
        raise FileNotFoundError("モデルがまだありません（train を先に実行）")
    bundle = json.loads(path.read_text(encoding="utf-8"))
    override = config.DATA_DIR / "paper_config.json"
    if override.exists():
        bundle["betting"].update(json.loads(override.read_text(encoding="utf-8")))
    booster = lgb.Booster(model_str=(config.MODEL_DIR / "lgbm.txt").read_text(encoding="utf-8"))
    return bundle, booster
