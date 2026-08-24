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

## admission gate (gate.py) — コアが着手を同期で要求する口

設計 rev3 Phase D。常駐コアの `dispatch_task` が
`POST http://autopilot-heart.autopilot.svc:8099/dispatch` を叩き、**数秒で
受理か拒否 + 理由**を得る。ビート周期を人間の待ち時間から外すための経路で、
コアに k8s の write 権限は渡さない (D29)。

```
core (MCP) --HTTP--> gate スレッド --判定--> reconcile.admit() (純関数)
                          |
                          +--非同期--> 採択ゲート実測 (最大 300s) --> Job 作成
                                              |
                                              v
                                   /data/dispatch/inbox/<id>.json
                                              |
                                    次のビートが projects.json へ折り込む
```

- **判定は既存の不変条件だけ**: `stop_engaged` (他の何より先) / `max_concurrent` /
  capability の宣言連鎖 (即時 dispatch は capability を名乗れない) / レート制限。
  遷移表は `tests/test_reconcile.py` の `AdmissionGateDecision`
- **冪等**: dispatch_id は内容のハッシュ。Job 名も決定論的で 409 は正常扱い
- **単一書き手は変わらない**: gate スレッドは git を触らない。ops-state への
  書き込み (projects.json / audit.jsonl) は必ずビート側が行う
- **到達範囲**: ClusterIP のみ + NetworkPolicy で送信元は autopilot-core の Pod。
  認証トークンは持たない (持てば `ops/rules.json` の Doppler 鍵 allowlist を
  触ることになり、人間レビュー必須になる)
- **止め方**: `HEART_GATE_LISTEN` を空にすると gate を起こさない。コアの
  `dispatch_task` は isError になり、`request_task` (バス経由の起票) に戻る

## 原則 (実装の理由)

- **判断は reconcile.py の純関数だけ**。heart.py は観測と実行。テストは遷移表
  (`tests/test_reconcile.py`) が仕様
- **LLM は心臓に居ない**。フィードバック分類 (triage.py) すらキーワードルール。
  「止めて」「veto P-NNNN」「approve P-NNNN」はモデルの解釈を経由しない
- **merge は heart のコードが実行する**。条件は reviewer の verdict=pass + CI green。
  LLM の自己申告は納品判断に入らない
- **運用パラメータは ops/rules.json、モデルは ops/models.json** が単一情報源。
  どちらも人間レビュー必須パス (ruleset) に含める
- **状態は ops-state ブランチ** (単一書き手 = heart)。main の CI 外だが、push 前に
  statefiles.validate_projects() が守る
- **書き置きは 2 経路から読む**。issue #56 / ops-feedback ブランチ (GitHub) に加えて、
  同居する Go サイドカー (`apps/autopilot/bus-sidecar`) が NATS から
  `/data/feedback-bus/inbox/<id>.json` に落としたぶんも読む。所有者の「止めて」を
  外部 SaaS の可用性から切り離すため (設計 D16/D27)。両経路の既読は同じ鍵
  (`ops/feedback/inbox/<id>.json`) で cursors に載るので、同じ書き置きは 1 回しか
  処理されない

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
