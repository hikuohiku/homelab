# P-0185 — PROGRESS

worker セッションごとに追記する。書式は自由だが、証跡 (コマンドと実測値) を残すこと。

## 初期状態 (initializer, 2026-08-23)

- PROJECT.md を作成して commit。実装は未着手 (`ops/stage3/` 未存在)
- verify 3 項目とも failing を実測済み (詳細は PROJECT.md 受入チェックリスト)

## セッション 1 (worker, 2026-08-23)

### やったこと

- `ops/stage3/readiness.json` 新設 — 6 基準の採点表。**verdict = `blocked`**
  (trifecta 分離の drill 証跡がまだ無いため。残り 5 基準は pass=true)
- `ops/stage3/readiness.py` 新設 — schema 検査 (`validate`) / 必須観点網羅
  (`missing_perspectives`) / verdict 規則 (`compute_verdict`) / evidence 存在
  (`missing_evidence`) の純関数。fail-closed: pass 不明は blocked、criteria 空も blocked
- `ops/stage3/README.md` 新設 — 各基準の閾値の理由と verdict 判定規則、
  「開放の実行・予告送信はしない」の誓い、台帳の直し方 (証拠を先に作ってから台帳を直す)
- `ops/tests/test_stage3_readiness.py` 新設 — 合成入力で両方向固定 + 実台帳検査。23 テスト

### 採点の実測根拠 (repo 内で確認したもの)

| 観点 | 証拠 | 実測値 |
|---|---|---|
| trifecta 分離 | `ops/projects/archive.jsonl` (不在の根拠) | P-0161 採択済み・成果未着、`ops/profiles/private-data/` 未存在 → **pass=false** |
| veto 到達性 | `apps/telegram-adapter/deployment.yaml` | digest pin `sha256:c634…96a329`、private DM 限定、fail-closed |
| 秘密分離監査 | `ops/sops-dependency-map.json` | problems 空、ci.yml:77 に check_credential_map 配線済みを実確認 |
| バックアップ復元 | `docs/backup.md` | immich 2026-08-05 (16秒/332MiB/82files)、syncthing P-0047 復元試験まで完了 |
| ループ連続性 | `.github/workflows/watchdog.yml` | cron */30、stale_seconds=7200、livenessProbe period30/failure3 を heart-deployment.yaml で実確認 |
| (追加) veto 機械実装 | `ops/rules.json` | window_hours=24、stop_keywords 7種 |

### verify の自己実測 (全 green)

1. `python3 -c "import json,os; d=json.load(open('ops/stage3/readiness.json')); ..."` — OK
2. `test -s ops/stage3/README.md && grep -q 'verdict' ops/stage3/README.md` — OK
3. `python3 -m unittest ops.tests.test_stage3_readiness` — Ran 23 tests, OK
   (red→green も実測: evidence_path を一時的に存在しないパスへ変えると
   TestRealLedger が落ちることを確認してから戻した)

### 分かったこと / 発見

- Python では `pass=` はキーワード引数にできない (`pass` が文)。合成基準を作るときは
  `criterion(**{"pass": True})` の形が必要 — 次にこの台帳のテストを触る人への注意
- verdict 規則は「全 pass で ready 側」の単純規則にした (PROJECT.md 設計方針どおり)。
  「条件付き ready」のような中間値は作らない (README に理由を書いた)

### 次のセッションへの一言

- **受入 3 項目とも green 済み。完成宣言は wrapper の仕事なので、レビュー差し戻しが
  無ければやることは無いはず**
- P-0161 が成果物 (`ops/profiles/private-data/` + demo.json) を届けたら、この台帳の
  1 基準目を採点し直す必要がある。evidence_path を archive.jsonl から実成果物へ張り替え、
  pass=true にするのは**その時点で正当**になる (今は false が正直な採点)
- 台帳を直すときの鉄則は README「台帳の直し方」に書いた: ダミーファイルで existence
  検査を通さない (捏造)、閾値変更は README の理由も一緒に書き換える
