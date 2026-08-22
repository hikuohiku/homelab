# P-0107 — OpenClaw の決定論パススルーを完成させる — Telegram の全受信を ops-feedback へ生保存 (P-0090 の続き)

## 目的

P-0090 は gateway/Telegram 接続までで soak 失敗により停止し、grill 済み設計の絶対条件 (2) が
未実装のまま: **受信メッセージの生テキストを必ず ops-feedback ブランチへ保存し、停止/veto/
タスク依頼の判定は heart の決定論 triage に任せる**。判定に LLM を経由させないことを
コードレベルで強制する。これが無いと人間の「止めて」が bot の LLM 判断に依存したままになる。

## 受入チェックリスト

initializer が実測した結果、**2 項目とも現時点で failing**
(2026-08-22、`project/p-0107` の checkout、リポジトリルートから実行)。

- [ ] `grep -rq 'ops-feedback' apps/openclaw/`
  — apps/openclaw/ 配下のどこかに ops-feedback ブランチへの書き込み経路が存在すること。
    実測 rc=1 (apps/openclaw/ 配下に 'ops-feedback' の文字列なし。P-0105 の
    apps/ops-dashboard 側にはあるが OpenClaw 側は未配線)。
- [ ] `python3 -m unittest ops.tests.test_openclaw_bridge`
  — パススルー機構 (受信→inbox 形式への変換) の固定テストが存在し green であること。
    実測 rc=1, FAILED (errors=1、`ops/tests/test_openclaw_bridge.py` 自体が未作成による
    import error)。

**verify は DoD の下限であって DoD そのものではない。** spec の本文どおり、検証は
**実メッセージ 1 通を送って ops-feedback ブランチにファイルが現れることの実測まで**
が本体であり、grep / unittest の green だけでは完了しない。その証跡は PROGRESS.md に残す。

## 設計方針

### 前提 (initializer が 2026-08-22 に実読・実測した。調べ直さなくてよい)

- **保存形式の原本**: ops-feedback ブランチの `ops/feedback/inbox/<id>.json`、1 件 1 ファイル、
  `{id, source, received, body}`。生成ロジックは
  `apps/ops-dashboard/app/src/app/api/feedback/route.ts` (`newNoteId()` :61-65 = 
  `YYYYMMDD-HHMMSS-<3byte hex>`、`JSON.stringify(note, null, 1) + "\n"` :98、
  Contents API PUT + ensureBranch() :46-59)。Telegram 版は同形式で `source: "telegram"`。
- **heart 側の取り込み口は既存のまま流用できる**: `ops/heart/facts.py` の `collect_feedback()`
  が inbox 新着の `body` を読み、`triage.classify()` (決定論キーワードのみ) が veto /
  stop_all 等を分類する。**生 body が inbox 形式で保存されれば停止経路は既存パススルーに乗る**。
- **kind: task-request は付けない** (spec 明記)。付与は「依頼らしさ」判定になり決定論の反対。
  生 body の保存だけで P-0091 側の triage が拾う。P-0091 とのインターフェイスは note 形式のみ。
- **実装候補は 2 つ、どちらでもよい** (spec):
  (a) OpenClaw 内部 hook — P-0090 worker #1 が既に特定済み:
      `message:received` internal hook (`HOOK.md` + handler.ts) を ConfigMap 化して
      `hooks.internal.load.extraDirs` から読ませる。ただし hooks doc に
      「command 風メッセージでは context.content が command body を優先する (= 生 text で
      なくなりうる)」という注意書きがあり、**生保存の要件と衝突する可能性を実測前に残している**
  (b) sidecar コンテナが `/home/node/.openclaw/telegram/ingress-spool-*` と
      `/tmp/openclaw/openclaw-*.log` を tail して 1 件 1 ファイルで書く。
      OpenClaw 内部に触らず決定論。spool/log は非公式な内部実装詳細なのでバージョン差分の
      実測が前提 (digest pin 済みの現 image 2026.7.1 = sha256:6a31d44b… での実在確認が先)
- **credential**: 書き込みトークンは AUTOPILOT_GITHUB_TOKEN 流用 (spec 指定)。
  rules.json の allowlist 登録済み・check_credential_map.py の DECLARED_DOPPLER_KEYS 登録済み
  のため、`apps/openclaw/external-secret.yaml` へ data エントリ 1 行足すだけで地図更新は不要
  (P-0090 worker #1 の結論と一致)。deployment.yaml への secretKeyRef 追加も同型。
- **今日の対話セッションが直した箇所を壊さない** (spec 注意): deployment.yaml は
  digest pin (sha256:6a31d44b…)・Recreate strategy・probe (/healthz, /startupz)・
  initContainer bootstrap・config-revision annotation "7" (2026-08-23 実読時点) が揃った状態。
  config を変えたら annotation を上げて pod を作り直すこと。
- **OpenClaw config は strict validation** — 未知キーがあると gateway が起動しない
  (P-0090 worker #1 実知見)。hook 方式を採る場合も新規キーは docs 例からのみ選ぶこと。

### 方針

1. 機構選定は最初の worker セッションで**実物で確定させる**: digest pin 済み image 内で
   spool/log ファイルの実在と形式を実測し、(b) sidecar tail が成立するならそちらを優先する
   (OpenClaw 内部に触れない = hook の content 加工問題も strict validation リスクも回避でき、
   「LLM を経由しない」強制がファイル読み取りというコードレベルの事実になる)。
   spool/log が実在しない等 (b) が不成立なら (a) hook で進めるが、その場合は
   context.content が生 text を保持するかを実際メッセージで検証してから inbox 書き込みに繋ぐ。
2. 書き込みは dashboard route.ts の原本ロジックを移植する: ensureBranch → Contents API PUT、
   422 衝突時に id 振り直し。1 件 1 ファイルなので read-modify-write も競合も無い。
3. テスト `ops/tests/test_openclaw_bridge.py` は、受信レコード→note JSON への変換
   (形式・source: telegram・id 一意性) を固定する。unittest (pytest は Job イメージに無い)。
   機構が (b) なら tail パースの変換も含める。
4. 検証は実メッセージ 1 通: TELEGRAM_BOT_TOKEN 等が Doppler 登録済みで Pod が起動している
   状態で送信 → ops-feedback ブランチに `<id>.json` が現れることを実測し、PROGRESS.md に
   証跡 (id・コミットハッシュ等) を残す。鍵未登録なら想定どおり待機状態なので、
   待ちの間にコードとテストを完成させておく。

## やらないこと

- **kind: task-request の付与・「依頼らしさ」判定** — spec 明記の禁じ手。生 body 保存のみ。
- **器側の task-request 消費配線** — P-0091。こちらは note 形式を守るだけ (1 PR 1 論点)。
- **停止/veto 判定ロジックの変更** — heart の triage.classify() は既存のまま使う。
- **OpenClaw の fork・改造・独自ビルド** — 公式イメージを digest pin のまま使う。
  (a) hook も公式拡張点の利用であり内部改造ではない。
- **apps/openclaw/deployment.yaml の既存部分の作り直し** — digest / replicas (Recreate) /
  probe / initContainer は今日の対話セッションが直した実績のある形を維持。触るのは
  env 追加と config-revision を上げる箇所に留める。
- **モデル設定・gateway 接続まわりの変更** — P-0090 soak の領域。本プロジェクトは
  受信の生保存のみ。
- **memory limits の新設** — substrate の規則 (実測の裏付けなしに付けない) を継続。
