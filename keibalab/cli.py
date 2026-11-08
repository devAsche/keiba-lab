"""コマンド一覧（python -m keibalab <コマンド>）

  backfill     過去のレース結果を集める（途中から再開できる）
  odds-day     開催日のオッズを1日通して記録する
  odds-once    指定日の全レースのオッズを1回だけ記録する（前日夜用）
  raceday      odds-day に加えて、モデルがあれば仮想購入も行う（開催日の朝に起動）
  nightly      毎晩の処理（結果の追加収集＋仮想購入の精算）
  validate     集めたデータの整合性チェック
  train        予測モデルを学習する
  backtest     時系列で区切ったバックテストを実行してレポートを出す
  paper-report 仮想購入の成績を集計する
  status       収集状況を表示する
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, timedelta

from . import config

DEFAULT_START = "2023-01"


def setup_logging(name: str) -> None:
    config.ensure_dirs()
    stamp = config.now_jst().strftime("%Y%m%d")
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOG_DIR / f"{name}_{stamp}.log", encoding="utf-8"),
    ]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)


def _date(value: str | None) -> date:
    if not value or value == "today":
        return config.now_jst().date()
    if value == "tomorrow":
        return config.now_jst().date() + timedelta(days=1)
    return date.fromisoformat(value)


def main(argv: list[str] | None = None) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows のコンソールでも文字化けしないように
    except Exception:
        pass
    parser = argparse.ArgumentParser(prog="keibalab", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("backfill")
    p.add_argument("--start", default=DEFAULT_START)
    p.add_argument("--end", default=None)
    p.add_argument("--max-minutes", type=float, default=None)
    p.add_argument("--oldest-first", action="store_true")

    p = sub.add_parser("odds-day")
    p.add_argument("--date", default="today")
    p.add_argument("--max-minutes", type=float, default=None)

    p = sub.add_parser("odds-once")
    p.add_argument("--date", default="tomorrow")

    p = sub.add_parser("raceday")
    p.add_argument("--date", default="today")
    p.add_argument("--max-minutes", type=float, default=None)
    p.add_argument("--no-paper", action="store_true")

    p = sub.add_parser("nightly")
    p.add_argument("--start", default=DEFAULT_START)
    p.add_argument("--max-minutes", type=float, default=360)

    sub.add_parser("validate")

    p = sub.add_parser("train")
    p.add_argument("--valid-from", default=None, help="この日付以降を第二段階（オッズとの合成）の学習に使う")

    p = sub.add_parser("backtest")
    p.add_argument("--first-test", default=None, help="最初のテスト期間の開始日（省略時は自動）")
    p.add_argument("--test-months", type=int, default=6)
    p.add_argument("--valid-months", type=int, default=6)

    sub.add_parser("paper-report")
    sub.add_parser("status")

    args = parser.parse_args(argv)
    setup_logging(args.command)
    log = logging.getLogger("keibalab")

    if args.command == "backfill":
        from .collect_history import backfill
        out = backfill(args.start, args.end, max_minutes=args.max_minutes, newest_first=not args.oldest_first)
    elif args.command == "odds-day":
        from .odds_recorder import run_day
        out = run_day(_date(args.date), max_minutes=args.max_minutes)
    elif args.command == "odds-once":
        from .odds_recorder import snapshot_once
        out = snapshot_once(_date(args.date))
    elif args.command == "raceday":
        from .odds_recorder import run_day
        hook = None
        if not args.no_paper:
            try:
                from .paper import make_hook
                hook = make_hook()
            except Exception as error:  # モデルが無い・ライブラリが無いときは記録だけ行う
                log.info("仮想購入は行いません（%s）。オッズの記録だけ行います。", error)
        out = run_day(_date(args.date), max_minutes=args.max_minutes, on_tick=hook)
    elif args.command == "nightly":
        from .collect_history import backfill
        out = {"backfill": backfill(args.start, None, max_minutes=args.max_minutes)}
        try:
            from .paper import settle
            out["paper_settle"] = settle()
        except Exception as error:
            out["paper_settle"] = f"skip: {error}"
    elif args.command == "validate":
        from .validate import run_checks
        out = run_checks()
    elif args.command == "train":
        from .model import train_and_save
        out = train_and_save(valid_from=args.valid_from)
    elif args.command == "backtest":
        from .backtest import run_backtest
        out = run_backtest(first_test=args.first_test, test_months=args.test_months, valid_months=args.valid_months)
    elif args.command == "paper-report":
        from .paper import report
        out = report()
    elif args.command == "status":
        from .collect_history import status
        out = status()
    else:
        parser.error("unknown command")
        return
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
