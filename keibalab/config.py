"""設定値（パス・アクセス間隔・時刻）。環境変数 KEIBALAB_DATA でデータの置き場所を変えられる。"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9), "JST")  # 日本は夏時間がないので固定オフセットで十分

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("KEIBALAB_DATA", str(ROOT / "data")))
DB_PATH = DATA_DIR / "keiba.sqlite"
LOG_DIR = DATA_DIR / "logs"
MODEL_DIR = DATA_DIR / "models"
REPORT_DIR = DATA_DIR / "reports"

# 正直に名乗る。ブラウザのふりはしない。
USER_AGENT = "keiba-lab/0.1 (personal research; polite crawler)"

# ホストごとの最小アクセス間隔（秒）。相手に負荷をかけないための値。
MIN_INTERVAL_SEC = {
    "sports.yahoo.co.jp": 2.5,
    "race.netkeiba.com": 2.0,
    "www.jra.go.jp": 2.5,
}
DEFAULT_INTERVAL_SEC = 3.0

TRACK_CODES = {
    "札幌": "01", "函館": "02", "福島": "03", "新潟": "04", "東京": "05",
    "中山": "06", "中京": "07", "京都": "08", "阪神": "09", "小倉": "10",
}
TRACK_NAMES = {v: k for k, v in TRACK_CODES.items()}


def now_jst() -> datetime:
    return datetime.now(JST)


def ensure_dirs() -> None:
    for path in (DATA_DIR, LOG_DIR, MODEL_DIR, REPORT_DIR):
        path.mkdir(parents=True, exist_ok=True)
