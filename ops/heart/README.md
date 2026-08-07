# ops/heart/ — heart-and-projects の心臓

homelab 常駐エージェントの再設計 (2026-08-07、設計の経緯は PR 参照)。
旧 `apps/autopilot/loop.sh` の「2 分ごとに 1 セッションで何でもやる」を、
**決定論の心臓 + プロジェクト単位の長命 Job** に分離した。

```
heart (Deployment, ここ)            runner Job (ops/runner/runner.py)
  120s ごとの reconcile ループ        短フレッシュ claude セッションの連鎖
  ├── 事実収集 (facts.py)            ├── PROJECT.md / progress / git log が文脈
  ├── 状態機械 (reconcile.py, 純関数) ├── wrapper が予算・無活動・verify を強制
  ├── Job spawn/kill (spawn.py)      └── 生 stream-json を PVC へ tee
  ├── 予告・通知 (notify.py→Discord)
  ├── merge 実行 (verdict=pass 条件)  reviewer Job (クリーン文脈で検品)
  └── ops-state ブランチへ状態 push   curriculum Job (立案 2 段: 生成→判定)
```

## 原則 (実装の理由)

- **判断は reconcile.py の純関数だけ**。heart.py は観測と実行。テストは遷移表
  (`tests/test_reconcile.py`) が仕様
- **LLM は心臓に居ない**。フィードバック分類 (triage.py) すらキーワードルール。
  「止めて」「veto P-NNNN」はモデルの解釈を経由しない
- **merge は heart のコードが実行する**。条件は reviewer の verdict=pass + CI green。
  LLM の自己申告は納品判断に入らない
- **運用パラメータは ops/rules.json、モデルは ops/models.json** が単一情報源。
  どちらも人間レビュー必須パス (ruleset) に含める
- **状態は ops-state ブランチ** (単一書き手 = heart)。main の CI 外だが、push 前に
  statefiles.validate_projects() が守る

## モード

- `HEART_MODE=shadow` (Phase 1): spawn / merge / Discord 送信をせず「would ...」を
  ログに出すだけ。事実収集・信念照合・指標・ops-state push は本番同様に動く。
  旧 loop と並走させて判断の正しさを実データで検証する
- `HEART_MODE=active` (Phase 2〜): 全機能有効。旧 loop は退役

## 手動での疎通試験

```sh
# Discord webhook (プラン検証 #3)
DISCORD_WEBHOOK_URL=... python3 -m ops.heart.notify "テスト"
# 単体テスト
python3 -m unittest discover -s ops/heart/tests -t .
```
