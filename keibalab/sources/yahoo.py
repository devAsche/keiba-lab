"""スポーツナビ（Yahoo!競馬）のページを解析する。

ID の形式:
    開催ID  YYTTKKDD   （年下2桁・場コード・回・日）例 24050508
    レースID YYTTKKDDRR （上に R を足したもの）  例 2405050811
"""

from __future__ import annotations

import html as htmllib
import re
from datetime import date

BASE = "https://sports.yahoo.co.jp/keiba"


def monthly_schedule_url(year: int, month: int) -> str:
    return f"{BASE}/schedule/monthly?year={year}&month={month}"


def race_list_url(meeting_id: str) -> str:
    return f"{BASE}/race/list/{meeting_id}"


def result_url(race_id: str) -> str:
    return f"{BASE}/race/result/{race_id}"


def denma_url(race_id: str) -> str:
    return f"{BASE}/race/denma/{race_id}"


def clean(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = htmllib.unescape(text).replace("　", " ")
    return re.sub(r"\s+", " ", text).strip()


def _to_int(text: str | None) -> int | None:
    if text is None:
        return None
    match = re.search(r"-?\d+", text.replace(",", ""))
    return int(match.group(0)) if match else None


def _to_float(text: str | None) -> float | None:
    if text is None:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    return float(match.group(0)) if match else None


def parse_time(text: str) -> float | None:
    """'1:59.5' -> 119.5、'59.8' -> 59.8"""
    match = re.search(r"(?:(\d+):)?(\d+)\.(\d)", text or "")
    if not match:
        return None
    minutes = int(match.group(1) or 0)
    return minutes * 60 + int(match.group(2)) + int(match.group(3)) / 10


# ---------------------------------------------------------------- 開催日程
def parse_monthly_schedule(page: str, year: int, month: int) -> list[dict]:
    """月間日程から (日付, 開催ID, 回, 場, 日) を取り出す。"""
    meetings: list[dict] = []
    seen: set[str] = set()
    cells = re.findall(
        r'hr-tableSchedule__data--date"[^>]*>(.*?)</td>', page, re.S
    )
    for cell in cells:
        day_match = re.search(r"(\d{1,2})日", clean(cell))
        if not day_match:
            continue
        day_of_month = int(day_match.group(1))
        for meeting_id, kai, track, nichi in re.findall(
            r'/keiba/race/list/(\d{8})"[^>]*>\s*(\d+)回(\S+?)(\d+)日\s*<', cell
        ):
            if meeting_id in seen:
                continue
            seen.add(meeting_id)
            meetings.append({
                "meeting_id": meeting_id,
                "date": date(year, month, day_of_month).isoformat(),
                "kai": int(kai),
                "track": track,
                "nichi": int(nichi),
            })
    return meetings


def parse_race_list(page: str) -> list[dict]:
    """開催ごとのレース一覧から (レースID, R, 発走時刻) を取り出す。"""
    races: list[dict] = []
    seen: set[str] = set()
    for match in re.finditer(r"(\d{1,2})R\s*<p>\s*(\d{1,2}:\d{2})\s*</p>", page):
        rest = page[match.end(): match.end() + 3000]
        id_match = re.search(r"/keiba/race/(?:index|result|denma)/(\d{10})", rest)
        if not id_match:
            continue
        race_id = id_match.group(1)
        if race_id in seen:
            continue
        seen.add(race_id)
        hour, minute = match.group(2).split(":")
        races.append({
            "race_id": race_id,
            "race_no": int(match.group(1)),
            "post_time": f"{int(hour):02d}:{minute}",
        })
    return races


# ---------------------------------------------------------------- レース情報
GRADE_MAP = {"g1": "G1", "g2": "G2", "g3": "G3", "jg1": "JG1", "jg2": "JG2", "jg3": "JG3"}


def class_rank(class_text: str, grade: str | None, title: str = "") -> float | None:
    """クラスを数値化（大きいほど上）。"""
    if grade in ("G1", "JG1"):
        return 8
    if grade in ("G2", "JG2"):
        return 7
    if grade in ("G3", "JG3"):
        return 6
    text = class_text or ""
    if "新馬" in text:
        return 0
    if "未勝利" in text:
        return 1
    if "1勝" in text or "500万" in text:
        return 2
    if "2勝" in text or "1000万" in text:
        return 3
    if "3勝" in text or "1600万" in text:
        return 4
    if "(L)" in title or "リステッド" in text:
        return 5.5
    if "オープン" in text:
        return 5
    return None


def parse_race_info(page: str, race_id: str) -> dict:
    block_match = re.search(r'<div class="hr-predictRaceInfo">(.*?)<nav', page, re.S)
    block = block_match.group(1) if block_match else ""
    texts = [clean(t) for t in re.findall(
        r'class="hr-predictRaceInfo__text">(.*?)</(?:div|span)>\s*(?=<|$)', block, re.S
    )]
    joined = " | ".join(texts)

    info: dict = {"race_id": race_id}
    date_match = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", joined)
    info["date"] = (
        date(int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))).isoformat()
        if date_match else None
    )
    post_match = re.search(r"(\d{1,2}):(\d{2})発走", joined)
    info["post_time"] = f"{int(post_match.group(1)):02d}:{post_match.group(2)}" if post_match else None

    title_match = re.search(r'hr-predictRaceInfo__title">(.*?)</h2>', block, re.S)
    title_html = title_match.group(1) if title_match else ""
    grade_match = re.search(r'hr-label--(\w+)"', title_html)
    info["grade"] = GRADE_MAP.get(grade_match.group(1).lower()) if grade_match else None
    info["name"] = clean(re.sub(r'<span class="hr-label.*?</span>', "", title_html, flags=re.S))

    course_match = re.search(r"(芝|ダート|障害)[・･]?([^|]*?)\s*(\d{3,4})m", joined)
    if course_match:
        info["surface"] = {"芝": "turf", "ダート": "dirt", "障害": "jump"}[course_match.group(1)]
        info["course_detail"] = course_match.group(2).strip("・･ ")
        info["distance"] = int(course_match.group(3))
    else:
        info["surface"] = info["course_detail"] = info["distance"] = None
    weather_match = re.search(r"天気：\s*(\S+)", joined)
    info["weather"] = weather_match.group(1) if weather_match else None
    going_match = re.search(r"馬場：\s*(\S+)", joined)
    info["going"] = going_match.group(1) if going_match else None

    class_text = ""
    age_text = ""
    for text in texts:
        if re.search(r"(新馬|未勝利|勝クラス|万下|オープン|障害)", text) and "本賞金" not in text and "発走" not in text:
            class_text = text
        if re.fullmatch(r"\d歳(以上)?", text):
            age_text = text
    info["class_text"] = class_text
    info["age_cond"] = age_text
    info["class_rank"] = class_rank(class_text, info["grade"], info["name"])
    info["handicap"] = 1 if "ハンデ" in class_text else 0
    prize_match = re.search(r"本賞金：\s*([\d,]+)", joined)
    info["prize1"] = _to_int(prize_match.group(1)) if prize_match else None
    return info


# ---------------------------------------------------------------- 結果
def _cells(row: str) -> list[str]:
    return re.findall(r'<td class="hr-table__data[^"]*">(.*?)</td>', row, re.S)


def _first_p(cell: str) -> str:
    match = re.search(r"<p>(.*?)</p>", cell, re.S)
    return clean(match.group(1)) if match else ""


def _before_p(cell: str) -> str:
    return clean(cell.split("<p>")[0])


def _link_id(cell: str, kind: str) -> str | None:
    match = re.search(rf"/keiba/directory/{kind}/(\w+)/", cell)
    return match.group(1) if match else None


def parse_body(text: str) -> dict:
    """'牡4/540(+16)/B' のような文字列を分解する。"""
    out = {"sex": None, "age": None, "weight": None, "weight_diff": None, "blinker": 0}
    sexage = re.search(r"(牡|牝|セ|騙)(\d+)", text)
    if sexage:
        out["sex"] = {"騙": "セ"}.get(sexage.group(1), sexage.group(1))
        out["age"] = int(sexage.group(2))
    weight = re.search(r"(\d{3})\s*\(\s*([+-]?\d+)\s*\)", text)
    if weight:
        out["weight"] = int(weight.group(1))
        out["weight_diff"] = int(weight.group(2))
    else:
        bare = re.search(r"/(\d{3})(?:\D|$)", text)
        if bare:
            out["weight"] = int(bare.group(1))
    if re.search(r"(^|/)\s*B\s*($|/)", text):
        out["blinker"] = 1
    return out


def parse_result(page: str, race_id: str) -> tuple[dict, list[dict], list[dict]]:
    info = parse_race_info(page, race_id)
    table_match = re.search(r"<th[^>]*>着順</th>.*?<tbody>(.*?)</tbody>", page, re.S)
    runners: list[dict] = []
    if table_match:
        for row in re.findall(r'<tr class="hr-table__row">(.*?)</tr>', table_match.group(1), re.S):
            cells = _cells(row)
            if len(cells) < 9:
                continue
            finish_text = clean(cells[0])
            body = parse_body(_first_p(cells[3]))
            popularity_text = _before_p(cells[7])
            odds_text = _first_p(cells[7])
            runners.append({
                "race_id": race_id,
                "finish_text": finish_text,
                "finish": int(finish_text) if finish_text.isdigit() else None,
                "frame": _to_int(clean(cells[1])),
                "horse_no": _to_int(clean(cells[2])),
                "horse_id": _link_id(cells[3], "horse"),
                "horse_name": _before_p(cells[3]),
                **body,
                "time_sec": parse_time(_before_p(cells[4])),
                "margin": _first_p(cells[4]),
                "passing": _before_p(cells[5]),
                "last3f": _to_float(_first_p(cells[5])),
                "jockey_id": _link_id(cells[6], "jockey"),
                "jockey": _before_p(cells[6]),
                "carried": _to_float(_first_p(cells[6])),
                "popularity": _to_int(popularity_text),
                "win_odds": _to_float(odds_text),
                "trainer_id": _link_id(cells[8], "trainer"),
                "trainer": clean(cells[8]),
            })
    payouts = parse_payouts(page, race_id)
    info["n_runners"] = sum(1 for r in runners if r["finish"] is not None or r["finish_text"] in ("中止", "失格"))
    return info, runners, payouts


def parse_payouts(page: str, race_id: str) -> list[dict]:
    payouts: list[dict] = []
    for table in re.findall(r'<table[^>]*class="hr-tableLeftTop"[^>]*>(.*?)</table>', page, re.S):
        current = None
        for row in re.findall(r"<tr>(.*?)</tr>", table, re.S):
            head = re.search(r'<th[^>]*scope="row"[^>]*>(.*?)</th>', row, re.S)
            if head:
                current = clean(head.group(1))
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            if current and len(cells) >= 2:
                payouts.append({
                    "race_id": race_id,
                    "bet_type": current,
                    "combo": clean(cells[0]),
                    "payout": _to_int(clean(cells[1])),
                    "popularity": _to_int(clean(cells[2])) if len(cells) > 2 else None,
                })
    return payouts


# ---------------------------------------------------------------- オッズ（予備の取得元）
def odds_url(race_id: str) -> str:
    return f"{BASE}/race/odds/tfw/{race_id}"


def parse_odds(page: str) -> dict:
    """単勝・複勝オッズ。netkeiba.parse_odds と同じ形で返す。"""
    status_match = re.search(r'hr-label--oddsStatus">\s*([^<]+?)\s*<', page)
    status_text = status_match.group(1).strip() if status_match else ""
    updated = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})\s*更新", page)
    official_at = (
        f"{updated.group(1)}-{int(updated.group(2)):02d}-{int(updated.group(3)):02d} "
        f"{int(updated.group(4)):02d}:{updated.group(5)}:00" if updated else None
    )
    rows: list[dict] = []
    body = re.search(r'<tbody[^>]*id="raceodds_table"[^>]*>(.*?)</tbody>', page, re.S)
    if body:
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", body.group(1), re.S):
            numbers = re.findall(r'hr-tableValue__data--number">\s*(?:<span[^>]*>)?\s*(\d+)', row)
            odds_cells = re.findall(r'hr-tableValue__data--odds">(.*?)</td>', row, re.S)
            if len(numbers) < 2 or len(odds_cells) < 2:
                continue
            horse_no = int(numbers[1])
            win = [_to_float(v) for v in re.findall(r"<span[^>]*>\s*([\d.]+)\s*</span>", odds_cells[0])]
            place = [_to_float(v) for v in re.findall(r"<span[^>]*>\s*([\d.]+)\s*</span>", odds_cells[1])]
            if len(win) == 1:
                rows.append({"bet_type": "win", "horse_no": horse_no, "odds": win[0], "odds_max": None,
                             "popularity": None})
            if len(place) == 2:
                rows.append({"bet_type": "place", "horse_no": horse_no, "odds": place[0], "odds_max": place[1],
                             "popularity": None})
    status = "result" if status_text == "確定オッズ" else (status_text or "")
    return {"status": status, "official_at": official_at, "rows": rows}


# ---------------------------------------------------------------- 出馬表
def parse_denma(page: str, race_id: str) -> tuple[dict, list[dict]]:
    info = parse_race_info(page, race_id)
    body_match = re.search(r"<tbody data-denma-sort-body[^>]*>(.*?)</tbody>", page, re.S)
    entries: list[dict] = []
    if body_match:
        for row_match in re.finditer(r'<tr class="hr-table__row"([^>]*)>(.*?)</tr>', body_match.group(1), re.S):
            row = row_match.group(2)
            cells = _cells(row)
            if len(cells) < 8:
                continue
            pedigree = cells[5]
            sire = re.search(r"父：\s*(?:<a[^>]*>)?([^<]+)", pedigree)
            damsire = re.search(r"母父：\s*(?:<a[^>]*>)?([^<)]+)", pedigree)
            body = parse_body(_first_p(cells[2]) + "/" + clean(cells[6]))
            odds_cell = clean(cells[7])
            odds_match = re.search(r"\(\s*([\d.]+)\s*\)", odds_cell)
            scratched = bool(re.search(r"(取消|除外)", clean(row)))
            entries.append({
                "race_id": race_id,
                "frame": _to_int(clean(cells[0])),
                "horse_no": _to_int(clean(cells[1])),
                "horse_id": _link_id(cells[2], "horse"),
                "horse_name": _before_p(cells[2]),
                "sex": body["sex"],
                "age": body["age"],
                "jockey_id": _link_id(cells[3], "jockey"),
                "jockey": _before_p(cells[3]),
                "carried": _to_float(_first_p(cells[3])),
                "trainer_id": _link_id(cells[4], "trainer"),
                "trainer": _before_p(cells[4]),
                "sire": clean(sire.group(1)) if sire else None,
                "damsire": clean(damsire.group(1)) if damsire else None,
                "weight": body["weight"],
                "weight_diff": body["weight_diff"],
                "odds": float(odds_match.group(1)) if odds_match else None,
                "scratched": 1 if scratched else 0,
            })
    return info, entries
