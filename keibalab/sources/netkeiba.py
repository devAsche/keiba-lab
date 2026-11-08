"""netkeiba のオッズAPI（race.netkeiba.com、JSON）を解析する。

レースIDはスポーツナビのIDの頭に年の上2桁を足したもの（2405050811 -> 202405050811）。
応答の data.odds は {"1": 単勝, "2": 複勝}、各馬は [オッズ, 複勝上限, 人気]。
"""

from __future__ import annotations

import json

ODDS_API = "https://race.netkeiba.com/api/api_get_jra_odds.html?race_id={race_id}&type=1&action=init"
BET_TYPES = {"1": "win", "2": "place"}
FINAL_STATUS = "result"


def to_netkeiba_id(yahoo_race_id: str) -> str:
    return "20" + yahoo_race_id


def odds_url(yahoo_race_id: str) -> str:
    return ODDS_API.format(race_id=to_netkeiba_id(yahoo_race_id))


def _num(value) -> float | None:
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def parse_odds(text: str) -> dict:
    """戻り値: {"status", "official_at", "rows": [{bet_type, horse_no, odds, odds_max, popularity}]}"""
    payload = json.loads(text)
    status = payload.get("status", "")
    data = payload.get("data")
    rows: list[dict] = []
    official_at = None
    if isinstance(data, dict):
        official_at = data.get("official_datetime")
        for key, bet_type in BET_TYPES.items():
            table = (data.get("odds") or {}).get(key) or {}
            for horse_no, values in table.items():
                if not str(horse_no).isdigit() or not isinstance(values, list) or not values:
                    continue
                odds = _num(values[0])
                if odds is None:
                    continue  # 取消・発売前など
                rows.append({
                    "bet_type": bet_type,
                    "horse_no": int(horse_no),
                    "odds": odds,
                    "odds_max": _num(values[1]) if len(values) > 1 else None,
                    "popularity": int(values[2]) if len(values) > 2 and str(values[2]).isdigit() else None,
                })
    return {"status": status, "official_at": official_at, "rows": rows}
