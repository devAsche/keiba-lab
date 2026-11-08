"""行儀のよい取得クライアント（標準ライブラリのみ）。

- ホストごとに最小間隔を空ける
- 一時的なエラー（通信断・5xx）は待って再試行
- 403/429 は「拒否された」とみなして即停止（回避策は取らない）
"""

from __future__ import annotations

import gzip
import logging
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from . import config

log = logging.getLogger(__name__)


class BlockedError(RuntimeError):
    """相手から拒否された（403/429）。収集を止めて人間が確認する。"""


class NotFoundError(RuntimeError):
    pass


_last_request: dict[str, float] = {}


def _throttle(host: str) -> None:
    interval = config.MIN_INTERVAL_SEC.get(host, config.DEFAULT_INTERVAL_SEC)
    last = _last_request.get(host)
    if last is not None:
        wait = interval - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
    _last_request[host] = time.monotonic()


def _decode(raw: bytes, content_type: str) -> str:
    match = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    candidates = []
    if match:
        candidates.append(match.group(1))
    head = raw[:2048].decode("ascii", errors="ignore")
    meta = re.search(r'charset=["\']?([\w-]+)', head, re.I)
    if meta:
        candidates.append(meta.group(1))
    candidates += ["utf-8", "cp932", "euc-jp"]
    for enc in candidates:
        enc = {"shift_jis": "cp932", "sjis": "cp932", "x-sjis": "cp932"}.get(enc.lower(), enc)
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def fetch(url: str, *, attempts: int = 4, timeout: float = 30.0) -> str:
    host = urlparse(url).netloc
    for attempt in range(1, attempts + 1):
        _throttle(host)
        request = urllib.request.Request(
            url,
            headers={"User-Agent": config.USER_AGENT, "Accept-Encoding": "gzip"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return _decode(raw, response.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as error:
            if error.code in (403, 429):
                raise BlockedError(f"{error.code} {url}") from error
            if error.code == 404:
                raise NotFoundError(url) from error
            # 400 は混雑時に一時的に返ることがある（間隔を空ければ通る）ので再試行の対象にする
            if (error.code >= 500 or error.code == 400) and attempt < attempts:
                log.warning("HTTP %s %s（%d回目、再試行）", error.code, url, attempt)
                time.sleep(5 * attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            if attempt < attempts:
                log.warning("通信エラー %s %s（%d回目、再試行）", type(error).__name__, url, attempt)
                time.sleep(5 * attempt)
                continue
            raise
    raise RuntimeError("unreachable")
