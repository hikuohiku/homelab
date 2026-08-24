# 段階 3 readiness 台帳 (P-0185)

VISION の段階 3 (生活ドメイン開放, Gmail / Calendar) を「エージェント自身が予告し、人間は拒否権のみ」
で開けるには、開放判断の材料を**事前に**列挙しておく必要がある。揃った日に最も急いでいる自分が
基準を即興で書くのが最悪の順番だから、この台帳は先に鋳造する。採点表本体は
[`readiness.json`](readiness.json)。判定ロジックの不変条件は `ops/tests/test_stage3_readiness.py`。

## verdict の判定規則

**全 criteria の `pass` が true のときだけ `ready_for_announce_draft`。1 つでも false
(または採点不能) なら `blocked`。**

- 「たぶん大丈夫」という総合感で ready 側に倒すことはない。1 項目でも欠けていれば blocked である
- **verdict がどちらに倒れても、この台帳は開放を実行しない。予告の送信もしない。**
  `ready_for_announce_draft` は「予告文の draft を作ってよい」を意味するだけで、送信は次の
  curriculum が別プロジェクトとして扱い、最終判断は issue #56 での人間の veto に委ねる。
  台帳を採点した日と開放の日を切り離すこと自体が、この台帳の存在理由である

verdict は `blocked` / `ready_for_announce_draft` の 2 値しかない。「条件付き ready」のような
曖昧な中間値を作らない — 中間値は「あと少し」という言い訳を生み、閾値の改竄を誘う。

## 各基準と、その閾値の理由

### 1. `trifecta-separation-drill` — lethal trifecta 分離の実証 (現在: pass=false)

- **閾値**: 分離 Job プロファイル (`ops/profiles/private-data/`, P-0161) のテンプレート +
  脅威モデル README + 合成データでの drill 実績 (`demo.json`: egress 拒否 / 成果物到達 /
  後始末完了の 3 点が true)
- **なぜ**: VISION が段階 3 を「lethal trifecta 分離プロファイルが前提」と明言している以上、
  前提は推測ではなく実測でなければ審査材料にならない。「分離する設計がある」ではなく
  「実際に通った記録がある」。trifecta (私的データ × 信頼できない内容 × 外部送信経路) は
  同時に揃うと事故る — 3 要素の同時存在を断つ構成を、本番データでなく偽メールで 1 回でも
  通した証拠が要る
- **現在値**: P-0161 採択済み・成果未着。`ops/profiles/private-data/` 未存在 (2026-08-23 実測)。
  証拠が無いので pass=false。evidence_path には不在の根拠となる採択記録
  (`ops/projects/archive.jsonl`) を指してある — **existence 検査を通すためのダミーファイルは
  作らない** (それは「自己申告を信用しない」の真逆)

### 2. `veto-channel-live` — 最新チャネルでの veto 到達性 (現在: pass=true)

- **閾値**: telegram-adapter Deployment が (a) digest pin (b) allowlist の private DM 限定
  (c) fail-closed (d) 判断せず転送のみ、の 4 条件を満たすこと
- **なぜ**: 人間の拒否権 (#56) は、人間が気づけなければ存在しないのと同じ。Discord は器の
  push 型だが、生活者への接点は Telegram が最新チャネルなので、そこから veto が届く口が必要。
  digest pin は「動くものがいつの間にか差し替わる」を防ぐ最低限の固定 (vaultwarden 1.36.0
  放置事件 #49 の教訓: pin されたものは誰も上げなければ据え置かれる)。private DM 限定 +
  fail-closed は「許可ユーザーが bot をグループに招いた発言を拾う」「設定ミスで全員からの
  DM を拾う」という事故の入口を潰すため。アダプタが判断を持たないのは、判定を triage 一箇所に
  集約しないと veto の機械的実装 (下記 6) と二重管理になるため
- **現在値**: 整備済み。digest pin 済み (`sha256:c634…96a329`)・private 限定・fail-closed・
  決定論アダプタ。稼働実績は initializer 実測 (2026-08-23)

### 3. `secrets-audit-wired` — 秘密分離の監査済み (現在: pass=true)

- **閾値**: credential 地図検査が CI 配線されており、SOPS 依存地図に未解決問題が無いこと
- **なぜ**: 生活ドメインを開くと credential 種類が増える。増えるたびに人間が地図を更新する
  運用は必ず腐る (棚卸しは「やった時点」でしか効かない — syncthing 5 日間丸腰事件) ので、
  「参照追加 → 宣言更新」を CI で強制する仕組みが先に要る。problems 空は、監査が
  「走らせた」だけでなく「見つけた問題が残っている状態でない」ことを保証する
- **現在値**: 監査済み。`ops/check_credential_map.py` (P-0077) が ci.yml:77 に配線、
  `ops/sops-dependency-map.json` (P-0105) は problems 空・encrypted_files 1 件が
  creation_rules と一致

### 4. `restore-proven` — バックアップ復元の実証 (現在: pass=true)

- **閾値**: restic 復元試験が最低 1 対象で完了し、手順・所要時間・整合性確認が docs に記録
  されていること
- **なぜ**: 生活データ (Gmail / Calendar) を触り始めると、失敗の被害が homelab 内で完結しない。
  「試したことのないバックアップは、バックアップではありません」(issue #56, 2026-08-05) —
  この原則は段階 3 でもそのまま効く。復元の手順と所要時間が記録されていれば、障害時に
  「やったことがない操作」を本番で初めて試すという最悪の状況を避けられる
- **現在値**: 実証済み。immich (2026-08-05, T-0071: restore_rc=0・16 秒 / 332 MiB・82 files) と
  syncthing (P-0047, 2026-08-10 復元試験まで完了) の 2 対象。手順は `docs/backup.md`

### 5. `loop-continuity-guarded` — ループ連続性 (現在: pass=true)

- **閾値**: heart の livenessProbe (内側) と、ループの外に居る別プロセス (常駐コア) の
  二重構え + 閾値の単一情報源 (rules.json)
- **なぜ**: VISION の賭けは「時間さえかければ届く」であり、止まったループはゼロを生む。
  死んだと言う口ごと死ぬ (旧・細切れループは止まったまま死んで誰も気づかなかった) を避ける
  ため、ループ自身以外の目が要る。見るのは**ビートの鮮度**で、プロセスの生死ではない —
  P-0027 の事故は「プロセスは生きているのにループが回っていない」だった
- **現在値**: 二重構え済み。livenessProbe (period 30s / failure 3)、コアの driver が
  60 秒毎に heart の Lease の `renewTime` と健全性レポートの `generated_at` を
  fail-closed で判定 (`apps/autopilot-core/app/silence.go`)、閾値は `rules.json` の
  `heartbeat.stale_seconds=7200` / `health.stale_seconds=21600`
- **GitHub Actions の watchdog を畳んだ理由** (state-out-of-git Phase 7): 別障害ドメインに
  居る利点はあったが、機械が git を定期的に叩く経路を 1 本も残さないという原則を優先した。
  node01 ごと死ねばコアも死ぬ — そのときは Telegram が応答しなくなり、所有者が日常的に
  使っている経路の上で沈黙が可視になる

### 6. `veto-machine-enforced` — veto の機械的実装 (現在: pass=true)

- **閾値**: rules.json に veto 窓 (window_hours) と stop_keywords が宣言され、triage が
  機械分類していること
- **なぜ**: 到達性 (上記 2) だけでは不十分で、届いた veto が確実に効かなければならない。
  「解釈できない停止命令は全停止に倒れる」のような倒れ方は運用の善意ではなく機械条件であるべき
  (fail-closed)。irreversible プロジェクトが常に窓を待つ宣言も、開放後の不可逆操作への
  最後の歯止めになる
- **現在値**: 宣言・実装済み。`window_hours=24` + stop_keywords 7 種、triage.classify による
  機械分類。test で固定済み

## 台帳の直し方

- 足りない観点が見つかったら criteria を追加する (schema 検査は追加を許す。ただし
  必須観点 5 id の欠落は落とされる)
- **pass を true にしたいなら、証拠を先に作ってから台帳を直す。** evidence_path の存在は
  unittest が毎回確認する。ダミーファイルで existence 検査を通すのは証拠の捏造である
- 閾値自体を変えたいときは README の理由も一緒に書き直す。理由の書き換えなしの
  閾値変更は、その日の気分と区別がつかない
