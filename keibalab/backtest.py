"""時系列で区切ったバックテスト（ウォークフォワード）。

各区切りで:
  第一段階の学習 … テスト開始の (検証月数) より前のデータ
  第二段階の学習 … テスト開始直前の (検証月数) か月
  テスト         … その後の (テスト月数) か月（学習には一切使わない）

買う時点のオッズ:
  オッズの時系列記録があるレース … 発走 lead 分前より前の最後の記録
  記録がないレース             … 確定オッズ（＝実戦より楽観的になる。レポートに明記する）
払戻はどちらの場合も確定オッズで計算する（パリミュチュエル方式なので）。
"""

from __future__ import annotations

import json
import logging

import numpy as np
import pandas as pd

from . import config
from .model import DEFAULT_BETTING, fit_stage2, lgbm_logit, prepare, r2_table, stage2_probs, train_lgbm

log = logging.getLogger(__name__)

EV_GRID = [1.0, 1.05, 1.1, 1.15, 1.2, 1.3, 1.5]
ODDS_BINS = [1, 2, 3, 5, 10, 20, 50, 100, 10000]


def make_folds(frame: pd.DataFrame, first_test: str | None, test_months: int, valid_months: int):
    start, end = frame["date"].min(), frame["date"].max()
    t0 = pd.Timestamp(first_test) if first_test else (start + pd.DateOffset(months=6 + valid_months)).normalize()
    t0 = t0.replace(day=1)
    folds = []
    while t0 <= end:
        t1 = t0 + pd.DateOffset(months=test_months)
        v0 = t0 - pd.DateOffset(months=valid_months)
        if frame[frame["date"] < v0]["race_id"].nunique() >= 300:
            folds.append((v0, t0, t1))
        t0 = t1
    return folds


def simulate(bets: pd.DataFrame) -> dict:
    if bets.empty:
        return {"bets": 0}
    ret = np.where(bets["won"] == 1, bets["win_odds"], 0.0)
    n = len(ret)
    roi = ret.mean()
    se = ret.std(ddof=1) / np.sqrt(n) if n > 1 else float("nan")
    return {
        "bets": int(n), "hits": int((bets["won"] == 1).sum()),
        "roi": round(float(roi), 4), "se": round(float(se), 4),
        "z_vs_100pct": round(float((roi - 1) / se), 2) if se and se > 0 else None,
        "avg_odds": round(float(bets["odds_dec"].mean()), 2),
    }


def select_bets(df: pd.DataFrame, p: np.ndarray, ev_min: float, rules: dict) -> pd.DataFrame:
    ev = p * df["odds_dec"].to_numpy()
    mask = (ev >= ev_min) & (df["odds_dec"].to_numpy() <= rules["max_odds"]) & (p >= rules["min_p"])
    out = df[mask].copy()
    out["p"] = p[mask]
    out["ev"] = ev[mask]
    return out


def kelly_path(bets: pd.DataFrame, rules: dict) -> dict:
    if bets.empty:
        return {}
    bankroll, peak, max_dd = 1.0, 1.0, 0.0
    for _, race in bets.sort_values(["date", "post_time", "race_id"]).groupby("race_id", sort=False):
        start = bankroll
        for row in race.itertuples():
            o = row.odds_dec
            f = rules["kelly_fraction"] * (row.p * o - 1) / (o - 1)
            stake = min(max(f, 0), rules["max_stake_frac"]) * start
            bankroll += -stake + (stake * row.win_odds if row.won == 1 else 0)
        peak = max(peak, bankroll)
        max_dd = max(max_dd, 1 - bankroll / peak)
    return {"final_multiple": round(bankroll, 4), "max_drawdown": round(max_dd, 4)}


def run_backtest(first_test: str | None = None, test_months: int = 6, valid_months: int = 6,
                 first_test_year: int | None = None) -> dict:
    if first_test_year and not first_test:
        first_test = f"{first_test_year}-01-01"
    rules = dict(DEFAULT_BETTING)
    frame = prepare()
    folds = make_folds(frame, first_test, test_months, valid_months)
    if not folds:
        raise RuntimeError("バックテストに必要な期間のデータがまだありません。")

    test_parts, fold_rows, chosen = [], [], []
    for v0, t0, t1 in folds:
        fund = frame[frame["date"] < v0]
        valid = frame[(frame["date"] >= v0) & (frame["date"] < t0)]
        test = frame[(frame["date"] >= t0) & (frame["date"] < t1)].copy()
        if valid.empty or test.empty:
            continue
        booster, rounds = train_lgbm(fund)
        valid_logit, test_logit = lgbm_logit(booster, valid), lgbm_logit(booster, test)
        stage2 = fit_stage2(valid, valid_logit)
        probs_valid = stage2_probs(stage2, valid, valid_logit)
        probs_test = stage2_probs(stage2, test, test_logit)
        # 閾値は検証期間で選ぶ（テスト期間で選び直さない）
        best_ev, best_roi = rules["ev_min"], -1.0
        for ev_min in EV_GRID:
            sim = simulate(select_bets(valid, probs_valid["combined"], ev_min, rules))
            if sim["bets"] >= 100 and sim["roi"] > best_roi:
                best_ev, best_roi = ev_min, sim["roi"]
        test["p_combined"] = probs_test["combined"]
        test["p_model"] = probs_test["model"]
        test["p_market"] = probs_test["market"]
        test["chosen_ev"] = best_ev
        test_parts.append(test)
        chosen.append(best_ev)
        fold_rows.append({
            "test": f"{t0.date()}〜{(t1 - pd.Timedelta(days=1)).date()}",
            "races": int(test["race_id"].nunique()),
            "rounds": rounds,
            "stage2_coef_[model,market]": [round(c, 3) for c in stage2.coef_combined],
            "pseudo_r2_test": r2_table(test, probs_test),
            "chosen_ev_min": best_ev,
            "selected_test": simulate(select_bets(test, probs_test["combined"], best_ev, rules)),
        })
    test_all = pd.concat(test_parts)
    p = test_all["p_combined"].to_numpy()

    grid = {str(ev): simulate(select_bets(test_all, p, ev, rules)) for ev in EV_GRID}
    selected = pd.concat([
        select_bets(part, part["p_combined"].to_numpy(), part["chosen_ev"].iloc[0], rules) for part in test_parts
    ])
    market_only_bets = select_bets(test_all, test_all["p_market"].to_numpy(), 1.0, rules)
    bins = pd.cut(test_all["odds_dec"], ODDS_BINS, right=False)
    flb = {}
    for interval, part in test_all.groupby(bins, observed=True):
        sim = simulate(part)
        flb[f"{interval.left:g}〜{interval.right:g}倍"] = {"bets": sim["bets"], "roi": sim["roi"]}

    summary = {
        "data_range": [str(frame["date"].min().date()), str(frame["date"].max().date())],
        "test_races": int(test_all["race_id"].nunique()),
        "share_decided_with_snapshot_odds": round(float(test_all["from_snapshot"].mean()), 4),
        "folds": fold_rows,
        "selected_strategy_test": {**simulate(selected), **kelly_path(selected, rules)},
        "ev_grid_test_reference_only": grid,
        "market_only_ev1.0_test": simulate(market_only_bets),
        "all_horses_by_odds_test": flb,
        "pseudo_r2_test_overall": r2_table(test_all, {
            "market_raw": test_all["q_market"].to_numpy(), "market": test_all["p_market"].to_numpy(),
            "model": test_all["p_model"].to_numpy(), "combined": p}),
    }
    write_report(summary, selected)
    return summary


def write_report(summary: dict, selected: pd.DataFrame) -> None:
    config.ensure_dirs()
    stamp = config.now_jst().strftime("%Y%m%d-%H%M")
    (config.REPORT_DIR / f"backtest_{stamp}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    cols = ["date", "race_id", "horse_no", "odds_dec", "win_odds", "p", "ev", "won", "from_snapshot"]
    selected[[c for c in cols if c in selected.columns]].to_csv(
        config.REPORT_DIR / f"backtest_bets_{stamp}.csv", index=False, encoding="utf-8-sig")
    sel = summary["selected_strategy_test"]
    r2 = summary["pseudo_r2_test_overall"]
    lines = [
        f"# バックテスト {stamp}",
        "",
        f"- データ期間: {summary['data_range'][0]} 〜 {summary['data_range'][1]}",
        f"- テストに使ったレース: {summary['test_races']}",
        f"- 買う時点のオッズに記録を使えた割合: {summary['share_decided_with_snapshot_odds']:.1%}"
        "（残りは確定オッズで判定しているので、実戦より楽観的）",
        "",
        "## いちばん大事な数字",
        "",
        f"- 市場だけの説明力（擬似R²）: {r2['market']} / モデルと市場の合成: {r2['combined']}"
        f"（差 {r2['combined'] - r2['market']:+.5f}）。差がプラスでなければ、市場に勝てる情報は無い。",
        f"- 検証期間で選んだ閾値でのテスト成績: {sel.get('bets', 0)}点、回収率 {sel.get('roi')}"
        f"（標準誤差 {sel.get('se')}、100%との差のz値 {sel.get('z_vs_100pct')}）",
        f"- 1/4ケリーで賭けた場合の資金倍率: {sel.get('final_multiple')}、最大の落ち込み {sel.get('max_drawdown')}",
        "",
        "## 区切りごと",
        "",
    ]
    for fold in summary["folds"]:
        lines.append(f"- {fold['test']}: {fold['races']}レース、係数[モデル,市場]={fold['stage2_coef_[model,market]']}、"
                     f"R²={fold['pseudo_r2_test']}、閾値{fold['chosen_ev_min']} → {fold['selected_test']}")
    lines += ["", "## 参考: テスト期間での閾値別（ここで閾値を選ぶと過学習になる）", ""]
    for ev, sim in summary["ev_grid_test_reference_only"].items():
        lines.append(f"- 期待値{ev}以上: {sim}")
    lines += ["", "## 参考: 全馬を買った場合のオッズ帯別回収率（本命-大穴バイアスの確認）", ""]
    for band, sim in summary["all_horses_by_odds_test"].items():
        lines.append(f"- {band}: {sim}")
    (config.REPORT_DIR / f"backtest_{stamp}.md").write_text("\n".join(lines), encoding="utf-8")
