# keiba-lab

無料で手に入るデータだけで、中央競馬（JRA）の「市場（オッズ）に勝てる確率表」が作れるかを検証するための道具一式です。
**実際の馬券は買いません。** 記録・バックテスト・仮想購入までを行います。

## 考え方

- 目標は的中率ではなく、控除率（単勝20%）を超える期待値。
- 予想はLLMにさせず、コードで確率を出す。モデルの確率と、オッズから逆算した大衆の確率を組み合わせる（Benter, 1994 の「第二段階」）。
- 検証は時系列で区切る。「買う時点で手に入ったオッズ」で検証しないと実戦より良く見えるので、オッズの時系列を自前で記録する。
- 勝てる根拠が数字で確認できるまで、実際のお金は入れない。

## データの取り方

| データ | 取得元 | 頻度 |
|---|---|---|
| 開催日程・レース一覧・過去結果・出馬表 | スポーツナビ（Yahoo!競馬） | 毎晩、続きから |
| 単勝・複勝オッズの時系列 | netkeiba のオッズAPI（失敗時はスポーツナビ） | 開催日に発走時刻に合わせて |
| 開催カレンダー | JRA公式 | 開催日の朝 |

アクセスは正直なUser-Agentで名乗り、ホストごとに間隔（2〜2.5秒）を空けます。403/429が返ったら回避せず止まります。
収集したデータ・モデル・台帳は `data/` に置き、リポジトリには入れません。

## 使い方（Windows）

初回だけ:

```
cd /d "C:\AI model\keiba-lab"
scripts\windows\setup.bat
powershell -ExecutionPolicy Bypass -File scripts\windows\register_tasks.ps1
```

定時の起動は Muse が行います（`MUSE_KEIBA.md`）。手で動かすときは:

```
.venv\Scripts\python.exe -m keibalab status          収集状況
.venv\Scripts\python.exe -m keibalab backfill        過去結果の収集（2023年〜、続きから）
.venv\Scripts\python.exe -m keibalab odds-day        今日のオッズを1日記録
.venv\Scripts\python.exe -m keibalab validate        データの整合性チェック
.venv\Scripts\python.exe -m keibalab train           モデルの学習
.venv\Scripts\python.exe -m keibalab backtest        バックテストのレポート
.venv\Scripts\python.exe -m keibalab paper-report    仮想購入の成績
```

## 構成

```
keibalab/
  sources/yahoo.py     スポーツナビの解析（日程・一覧・結果・出馬表・オッズ）
  sources/netkeiba.py  オッズAPIの解析
  sources/jra.py       JRA開催カレンダーの解析
  http.py              行儀のよい取得（間隔・再試行・拒否されたら停止）
  db.py                SQLite（WALモード）
  collect_history.py   過去結果の収集
  odds_recorder.py     オッズ時系列の記録
  validate.py          整合性チェック
  features.py          特徴量（その時点で手に入る情報だけを使う）
  model.py             LightGBM ＋ 条件付きロジットでオッズと合成
  backtest.py          時系列分割のバックテスト
  paper.py             仮想購入の台帳と精算
scripts/windows/       セットアップ・定時実行用のバッチとタスク登録
```

## 参考にしたもの

- W. Benter, "Computer Based Horse Race Handicapping and Wagering Systems: A Report" (1994)
- GitHub上の既存実装（netkeiba/Yahoo の解析、オッズ時系列の取得間隔の考え方）。コードは流用せず、取得間隔の設計などの発想だけを参考にした。ブラウザに成りすます実装（User-Agentの偽装やTLS指紋の偽装）は採用していない。
