"""解析部分のテスト。実ページの複製は置かず、同じ構造の最小HTMLを手で書いて確かめる。"""

from datetime import date

from keibalab.odds_recorder import interval_minutes
from keibalab.sources import jra, netkeiba, yahoo

RACE_INFO = """
<div class="hr-predictRaceInfo">
  <div class="hr-predictRaceInfo__raceNumber">11R</div>
  <div class="hr-predictRaceInfo__data">
    <div class="hr-predictRaceInfo__date">
      <div class="hr-predictRaceInfo__text">2025年5月4日（日）</div>
      <div class="hr-predictRaceInfo__text">3回京都4日</div>
      <div class="hr-predictRaceInfo__text">15:40発走</div>
    </div>
    <h2 class="hr-predictRaceInfo__title">
      テスト記念
      <span class="hr-label hr-label--g2">GII</span>
    </h2>
    <div class="hr-predictRaceInfo__status">
      <span class="hr-predictRaceInfo__text">ダート・右 1800m</span>
      <span class="hr-predictRaceInfo__text">天気：<div class="hr-icon__tenki">曇</div></span>
      <span class="hr-predictRaceInfo__text">馬場：<div class="hr-labelBaba">稍重</div></span>
      <span class="hr-predictRaceInfo__text">4歳以上</span>
      <span class="hr-predictRaceInfo__text">オープン （国際） ハンデ</span>
      <span class="hr-predictRaceInfo__text">本賞金：5,700、2300万円</span>
    </div>
  </div>
</div>
<nav class="hr-menuWhite"></nav>
"""

RESULT_ROWS = """
<table class="hr-table"><thead><tr><th class="hr-table__head" scope="col">着順</th></tr></thead>
<tbody>
<tr class="hr-table__row">
<td class="hr-table__data hr-table__data--number">1</td>
<td class="hr-table__data hr-table__data--number"><span>3</span></td>
<td class="hr-table__data hr-table__data--number">5</td>
<td class="hr-table__data hr-table__data--name"><a href="/keiba/directory/horse/2021100001/">アルファ</a><p>牝4/468(-2)/B</p></td>
<td class="hr-table__data hr-table__data--time">1:51.2<p>-</p></td>
<td class="hr-table__data hr-table__data--order">03-03-02-01<p>37.1</p></td>
<td class="hr-table__data hr-table__data--name"><a href="/keiba/directory/jockey/00001/">騎手 一</a><p>55.0</p></td>
<td class="hr-table__data hr-table__data--odds">2<p>(4.3)</p></td>
<td class="hr-table__data hr-table__data--name"><a href="/keiba/directory/trainer/00009/">調教 師</a></td>
</tr>
<tr class="hr-table__row">
<td class="hr-table__data hr-table__data--number">取消</td>
<td class="hr-table__data hr-table__data--number"><span>1</span></td>
<td class="hr-table__data hr-table__data--number">1</td>
<td class="hr-table__data hr-table__data--name"><a href="/keiba/directory/horse/2021100002/">ベータ</a><p>セ5/</p></td>
<td class="hr-table__data hr-table__data--time"><p></p></td>
<td class="hr-table__data hr-table__data--order"><p></p></td>
<td class="hr-table__data hr-table__data--name"><a href="/keiba/directory/jockey/00002/">騎手 二</a><p>57.0</p></td>
<td class="hr-table__data hr-table__data--odds"><p></p></td>
<td class="hr-table__data hr-table__data--name"><a href="/keiba/directory/trainer/00008/">調教 二</a></td>
</tr>
</tbody></table>
<table cellspacing="0" class="hr-tableLeftTop"><tbody>
<tr><th class="hr-tableLeftTop__head" scope="row" rowspan="1">単勝</th>
<td class="x">5</td><td class="x">430円</td><td class="x">2</td></tr>
<tr><th class="hr-tableLeftTop__head" scope="row" rowspan="2">複勝</th>
<td class="x">5</td><td class="x">160円</td><td class="x">2</td></tr>
<tr><td class="x">7</td><td class="x">1,250円</td><td class="x">9</td></tr>
</tbody></table>
"""


def test_parse_time_and_body():
    assert yahoo.parse_time("1:59.5") == 119.5
    assert yahoo.parse_time("59.8") == 59.8
    assert yahoo.parse_time("") is None
    body = yahoo.parse_body("牡4/540(+16)")
    assert body == {"sex": "牡", "age": 4, "weight": 540, "weight_diff": 16, "blinker": 0}
    assert yahoo.parse_body("牝3/430(0)/B")["blinker"] == 1


def test_parse_race_info():
    info = yahoo.parse_race_info(RACE_INFO, "2508030411")
    assert info["date"] == "2025-05-04"
    assert info["post_time"] == "15:40"
    assert info["grade"] == "G2"
    assert info["name"] == "テスト記念"
    assert info["surface"] == "dirt" and info["distance"] == 1800
    assert info["weather"] == "曇" and info["going"] == "稍重"
    assert info["class_rank"] == 7
    assert info["handicap"] == 1
    assert info["prize1"] == 5700


def test_parse_result_and_payouts():
    info, runners, payouts = yahoo.parse_result(RACE_INFO + RESULT_ROWS, "2508030411")
    assert len(runners) == 2
    winner = runners[0]
    assert winner["finish"] == 1 and winner["horse_no"] == 5 and winner["frame"] == 3
    assert winner["horse_id"] == "2021100001" and winner["weight"] == 468 and winner["weight_diff"] == -2
    assert winner["blinker"] == 1 and winner["time_sec"] == 111.2 and winner["last3f"] == 37.1
    assert winner["popularity"] == 2 and winner["win_odds"] == 4.3 and winner["carried"] == 55.0
    scratched = runners[1]
    assert scratched["finish"] is None and scratched["finish_text"] == "取消"
    assert info["n_runners"] == 1
    assert {"bet_type": "単勝", "combo": "5", "payout": 430} == {k: payouts[0][k] for k in ("bet_type", "combo", "payout")}
    assert payouts[2]["bet_type"] == "複勝" and payouts[2]["payout"] == 1250


def test_parse_race_list_and_schedule():
    listing = """
      <td class="hr-tableSchedule__data hr-tableSchedule__data--date" rowspan="2">1R<p>9:50</p></td>
      <td><a href="/keiba/race/index/2605050301">A</a></td>
      <td class="hr-tableSchedule__data hr-tableSchedule__data--date" rowspan="2">2R<p>10:20</p></td>
      <td><a href="/keiba/race/index/2605050302">B</a></td>
    """
    races = yahoo.parse_race_list(listing)
    assert races == [
        {"race_id": "2605050301", "race_no": 1, "post_time": "09:50"},
        {"race_id": "2605050302", "race_no": 2, "post_time": "10:20"},
    ]
    schedule = """
      <td class="hr-tableSchedule__data hr-tableSchedule__data--date" rowspan="2">
        14日（土）<p><a href="/keiba/race/list/26050503" data-x="1">5回東京3日</a></p>
      </td>
    """
    meetings = yahoo.parse_monthly_schedule(schedule, 2026, 11)
    assert meetings == [{"meeting_id": "26050503", "date": "2026-11-14", "kai": 5, "track": "東京", "nichi": 3}]


def test_jra_calendar():
    page = "<p>5回東京3日</p><p>5回京都3日</p><p>5回東京3日</p>"
    meetings = jra.parse_meetings(page, date(2026, 11, 14))
    assert [m["meeting_id"] for m in meetings] == ["26050503", "26080503"]


def test_netkeiba_odds():
    text = ('{"status":"middle","data":{"official_datetime":"2026-11-14 09:30:00","odds":'
            '{"1":{"01":["3.4","0.0","1"],"02":["---.-","0.0","0"]},"2":{"01":["1.2","1.5","1"]}}}}')
    parsed = netkeiba.parse_odds(text)
    assert parsed["status"] == "middle"
    assert parsed["rows"] == [
        {"bet_type": "win", "horse_no": 1, "odds": 3.4, "odds_max": None, "popularity": 1},
        {"bet_type": "place", "horse_no": 1, "odds": 1.2, "odds_max": 1.5, "popularity": 1},
    ]
    empty = netkeiba.parse_odds('{"status":"middle","data":"","reason":"result odds empty"}')
    assert empty["rows"] == []
    assert netkeiba.to_netkeiba_id("2605050311") == "202605050311"


def test_intervals():
    assert interval_minutes(200) == 30
    assert interval_minutes(60) == 10
    assert interval_minutes(20) == 5
    assert interval_minutes(5) == 1
    assert interval_minutes(-3) == 5
