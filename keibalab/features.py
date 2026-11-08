"""特徴量づくり。各行には「そのレースより前に分かっていた情報」だけを使う。

- 馬の過去成績は、馬ごとに日付順に並べて1つずらして（shift）から集計する
- 騎手・調教師の成績は、その日より前の日の分だけを数える（当日のレースは含めない）
- 当日のオッズは特徴量に入れない（市場の確率は第二段階で別に組み合わせる）
"""

from __future__ import annotations

import numpy as np
import pandas as pd

GOING_CODE = {"良": 0, "稍重": 1, "稍": 1, "重": 2, "不良": 3, "不": 3}
SURFACE_CODE = {"turf": 0, "dirt": 1, "jump": 2}
SEX_CODE = {"牡": 0, "牝": 1, "セ": 2}

STATIC_FEATURES = [
    "frame", "horse_no", "n_runners", "distance", "surface_code", "track_code", "going_code",
    "class_rank", "handicap", "sex_code", "age", "carried", "carried_dm", "weight", "weight_diff", "month",
]
HISTORY_FEATURES = [
    "n_starts", "days_since", "prev_finish_pct", "fp_mean3", "fp_mean_all", "win_rate", "top3_rate",
    "prev_behind", "behind_mean3", "behind_min5", "l3f_mean3", "prev_log_odds", "log_odds_mean3",
    "prev_pop_pct", "dist_change", "class_change", "same_surface_starts", "same_surface_top3",
    "same_dist_band_top3", "prev_weight_diff_abs",
]
ENTITY_FEATURES = ["jockey_win", "jockey_top3", "jockey_starts", "trainer_win", "trainer_top3", "trainer_starts"]
RELATIVE_BASE = ["fp_mean3", "win_rate", "behind_mean3", "jockey_win", "log_odds_mean3", "l3f_mean3",
                 "top3_rate", "trainer_win"]
RELATIVE_FEATURES = [f"{c}_rk" for c in RELATIVE_BASE] + [f"{c}_dm" for c in RELATIVE_BASE]
FEATURES = STATIC_FEATURES + HISTORY_FEATURES + ENTITY_FEATURES + RELATIVE_FEATURES

HIST_SQL = """
SELECT x.race_id, x.horse_no, x.finish, x.finish_text, x.frame, x.horse_id, x.sex, x.age,
       x.weight, x.weight_diff, x.time_sec, x.last3f, x.jockey_id, x.carried, x.popularity,
       x.win_odds, x.trainer_id,
       r.date, r.track, r.race_no, r.post_time, r.surface, r.distance, r.going, r.class_rank,
       r.handicap, r.grade
FROM results x JOIN races r ON r.race_id = x.race_id
WHERE r.has_result = 1
"""

ENTRY_SQL = """
SELECT e.race_id, e.horse_no, NULL AS finish, '' AS finish_text, e.frame, e.horse_id, e.sex, e.age,
       e.weight, e.weight_diff, NULL AS time_sec, NULL AS last3f, e.jockey_id, e.carried, NULL AS popularity,
       NULL AS win_odds, e.trainer_id,
       r.date, r.track, r.race_no, r.post_time, r.surface, r.distance, r.going, r.class_rank,
       r.handicap, r.grade
FROM entries e JOIN races r ON r.race_id = e.race_id
WHERE e.race_id IN ({ids}) AND COALESCE(e.scratched, 0) = 0
"""


def load_runs(conn, entry_race_ids: list[str] | None = None) -> pd.DataFrame:
    hist = pd.read_sql_query(HIST_SQL, conn)
    ran = hist["finish"].notna() | hist["finish_text"].isin(["中止", "失格"])
    hist = hist[ran].copy()
    hist["is_target"] = 0
    frames = [hist]
    if entry_race_ids:
        ids = ",".join("?" for _ in entry_race_ids)
        live = pd.read_sql_query(ENTRY_SQL.format(ids=ids), conn, params=list(entry_race_ids))
        live = live[~live["race_id"].isin(hist["race_id"])]
        live["is_target"] = 1
        frames.append(live)
    runs = pd.concat(frames, ignore_index=True)
    runs["date"] = pd.to_datetime(runs["date"])
    # 出走予定の行は結果の列が空（NULL）なので、結合すると文字列型になってしまう。数値に揃える
    numeric = ["horse_no", "finish", "frame", "age", "weight", "weight_diff", "time_sec", "last3f",
               "carried", "popularity", "win_odds", "distance", "class_rank", "handicap", "race_no"]
    for col in numeric:
        runs[col] = pd.to_numeric(runs[col], errors="coerce")
    return runs


def _shifted_mean(df: pd.DataFrame, key: str, col: str) -> pd.Series:
    shifted = df.groupby(key, sort=False)[col].shift(1)
    total = shifted.fillna(0).groupby(df[key], sort=False).cumsum()
    count = shifted.notna().astype(int).groupby(df[key], sort=False).cumsum()
    return total / count.replace(0, np.nan)


def _shifted_rolling(df: pd.DataFrame, key: str, col: str, window: int, how: str = "mean") -> pd.Series:
    shifted = df.groupby(key, sort=False)[col].shift(1)
    roller = shifted.groupby(df[key], sort=False).rolling(window, min_periods=1)
    out = roller.mean() if how == "mean" else roller.min()
    return out.reset_index(level=0, drop=True).reindex(df.index)


def _entity_rates(df: pd.DataFrame, key: str, prefix: str, prior_win: float, prior_top3: float,
                  k: float = 30.0) -> pd.DataFrame:
    """その日より前の全期間の成績（縮小推定）。"""
    hist = df[df["is_target"] == 0]
    daily = (hist.groupby([key, "date"])
             .agg(starts=("won", "size"), wins=("won", "sum"), top3=("top3", "sum"))
             .reset_index().sort_values("date"))
    for col in ("starts", "wins", "top3"):
        daily[col] = daily.groupby(key)[col].cumsum()  # その日を含む累計
    left = df[[key, "date"]].reset_index().sort_values("date")
    merged = pd.merge_asof(left, daily, on="date", by=key, direction="backward", allow_exact_matches=False)
    merged = merged.set_index("index").reindex(df.index)
    starts = merged["starts"].fillna(0)
    out = pd.DataFrame(index=df.index)
    out[f"{prefix}_starts"] = starts
    out[f"{prefix}_win"] = (merged["wins"].fillna(0) + prior_win * k) / (starts + k)
    out[f"{prefix}_top3"] = (merged["top3"].fillna(0) + prior_top3 * k) / (starts + k)
    return out


def build_features(runs: pd.DataFrame) -> pd.DataFrame:
    df = runs.sort_values(["date", "post_time", "race_id", "horse_no"]).reset_index(drop=True)
    for key in ("horse_id", "jockey_id", "trainer_id"):
        df[key] = df[key].fillna("?").astype(str)
    df["surface_code"] = df["surface"].map(SURFACE_CODE)
    df["going_code"] = df["going"].map(GOING_CODE)
    df["sex_code"] = df["sex"].map(SEX_CODE)
    df["track_code"] = pd.to_numeric(df["race_id"].str[2:4], errors="coerce")
    df["month"] = df["date"].dt.month
    df["n_runners"] = df.groupby("race_id")["horse_no"].transform("size")
    df["carried_dm"] = df["carried"] - df.groupby("race_id")["carried"].transform("mean")

    # 各出走の「結果」系（過去の行としてだけ使う）
    finished = df["finish"].notna()
    n_fin = df.groupby("race_id")["finish"].transform("count")
    df["finish_pct"] = np.where(finished, (df["finish"] - 1) / (n_fin - 1).clip(lower=1), np.nan)
    df.loc[(~finished) & (df["is_target"] == 0), "finish_pct"] = 1.0  # 中止・失格は最下位扱い
    df["won"] = np.where(df["is_target"] == 0, (df["finish"] == 1).astype(float), np.nan)
    df["top3"] = np.where(df["is_target"] == 0, (df["finish"] <= 3).astype(float), np.nan)
    best = df.groupby("race_id")["time_sec"].transform("min")
    df["behind"] = (df["time_sec"] - best).clip(lower=0, upper=5)
    df["l3f_pct"] = df.groupby("race_id")["last3f"].rank(pct=True)
    df["log_odds"] = np.log(df["win_odds"].where(df["win_odds"] > 0))
    df["pop_pct"] = (df["popularity"] - 1) / (df["n_runners"] - 1).clip(lower=1)
    df["dist_band"] = pd.cut(df["distance"], [0, 1400, 1800, 2200, 9999], labels=False)

    # 馬の過去
    h = "horse_id"
    g = df.groupby(h, sort=False)
    df["n_starts"] = g.cumcount()
    df["days_since"] = (df["date"] - g["date"].shift(1)).dt.days
    df["prev_finish_pct"] = g["finish_pct"].shift(1)
    df["prev_behind"] = g["behind"].shift(1)
    df["prev_log_odds"] = g["log_odds"].shift(1)
    df["prev_pop_pct"] = g["pop_pct"].shift(1)
    df["dist_change"] = df["distance"] - g["distance"].shift(1)
    df["class_change"] = df["class_rank"] - g["class_rank"].shift(1)
    df["prev_weight_diff_abs"] = g["weight_diff"].shift(1).abs()
    df["fp_mean3"] = _shifted_rolling(df, h, "finish_pct", 3)
    df["fp_mean_all"] = _shifted_mean(df, h, "finish_pct")
    df["behind_mean3"] = _shifted_rolling(df, h, "behind", 3)
    df["behind_min5"] = _shifted_rolling(df, h, "behind", 5, how="min")
    df["l3f_mean3"] = _shifted_rolling(df, h, "l3f_pct", 3)
    df["log_odds_mean3"] = _shifted_rolling(df, h, "log_odds", 3)
    prior_win, prior_top3 = 0.08, 0.24
    wins_before = g["won"].shift(1).fillna(0).groupby(df[h], sort=False).cumsum()
    top3_before = g["top3"].shift(1).fillna(0).groupby(df[h], sort=False).cumsum()
    df["win_rate"] = (wins_before + prior_win * 3) / (df["n_starts"] + 3)
    df["top3_rate"] = (top3_before + prior_top3 * 3) / (df["n_starts"] + 3)
    gs = df.groupby([h, "surface"], sort=False)
    df["same_surface_starts"] = gs.cumcount()
    df["same_surface_top3"] = gs["top3"].shift(1).fillna(0).groupby([df[h], df["surface"]], sort=False).cumsum()
    df["same_surface_top3"] = (df["same_surface_top3"] + prior_top3 * 3) / (df["same_surface_starts"] + 3)
    gd = df.groupby([h, "dist_band"], sort=False)
    band_starts = gd.cumcount()
    band_top3 = gd["top3"].shift(1).fillna(0).groupby([df[h], df["dist_band"]], sort=False).cumsum()
    df["same_dist_band_top3"] = (band_top3 + prior_top3 * 3) / (band_starts + 3)

    # 騎手・調教師
    df = df.join(_entity_rates(df, "jockey_id", "jockey", prior_win, prior_top3))
    df = df.join(_entity_rates(df, "trainer_id", "trainer", prior_win, prior_top3))

    # レース内での相対値
    for col in RELATIVE_BASE:
        grp = df.groupby("race_id")[col]
        df[f"{col}_rk"] = grp.rank(pct=True)
        df[f"{col}_dm"] = df[col] - grp.transform("mean")
    return df


def modeling_frame(df: pd.DataFrame) -> pd.DataFrame:
    """学習・検証に使う行（障害戦と少頭数戦を除く）。"""
    keep = (df["surface"].isin(["turf", "dirt"])) & (df["n_runners"] >= 5)
    return df[keep].copy()


def market_probability(odds: pd.Series, race_ids: pd.Series) -> pd.Series:
    """単勝オッズから大衆の確率を出す（逆数をレース内で合計1に正規化）。"""
    inv = 1.0 / odds.where(odds > 0)
    return inv / inv.groupby(race_ids).transform("sum")
