# P-0071 — credential がどこで何を握っているか、CI で機械的に裏切れないようにする

## 目的

apps/ 配下の SecretKeyRef / Doppler 参照は 29 ファイルに散在するが、宣言済みの一覧は
autopilot 自身向けの `allowed_autopilot_doppler_keys` しか無く、他アプリの credential 参照が
増えても機械的に検知できない。全 ExternalSecret / SecretKeyRef の参照先 (Doppler キーと
k8s Secret 名) を列挙して宣言済み一覧と突き合わせ、「鍵の地図」を CI が裏切らない仕組みにする
(seeds.md『鍵の地図を作る』= 旧 P-0041 の採択形)。

## 受入チェックリスト

initializer が実測した結果、**2 項目とも現時点で failing**
(2026-08-22、`project/p-0071` の checkout で、リポジトリルートから実行)。

- [ ] `test -f ops/check_credential_map.py`
  — 列挙・突き合わせスクリプトが存在すること。実測 rc=1 (ファイル自体が無い)。
- [ ] `grep -q 'check_credential_map' .github/workflows/ci.yml`
  — CI への配線。実測 rc=1 (ci.yml に 1 度も出てこない)。

**verify は DoD の下限であって DoD そのものではない。** 2 本とも「文字列があるか」しか見ず、
**「未宣言の credential 参照を実際に落とすか」という効能は verify が一切見張っていない**。
合成した違反 fixture を確実に落とすこと (--selftest 等) を証明し、コマンドと実出力を
`PROGRESS.md` に貼ることが唯一の証拠になる。

## 設計方針

### 前提 (initializer が 2026-08-22 に実測・実読した。調べ直さなくてよい)

- 実測スケール: apps/**/*.yaml (`/charts/` 除く) のうち ExternalSecret の `remoteRef.key`
  (Doppler キー) は **20 種**、ExternalSecret の `target.name` (k8s Secret 名) は **18 種**、
  Deployment/CronJob の `valueFrom.secretKeyRef` は **(secret, key) の組 8 種**。spec の
  「29 ファイル」はこの走査のヒット件数と一致する。
- 今日の宣言済み一覧は `ops/rules.json` の `allowed_autopilot_doppler_keys` (**4 キーのみ**、
  autopilot namespace 向け Job 注入の allowlist。validate.py `check_autopilot_secret_allowlist`
  が検査) だけ。残り 16 キーの参照は**宣言が存在しない** — 初期地図は現状の実測から生成し、
  「実態 = 宣言」の状態から始める。ここで足りないまま落とす設計にすると初回から赤になる。
- 形は既存 check スクリプトの流儀 (`ops/check_health_reporter_target.py` 等と同型):
  stdlib + PyYAML のみ (CI ランナーにある。kubectl/kustomize は不要な静的解析)、fail-closed
  (抽出に失敗したら成功扱いにしない)。ci.yml の "consistency checks" ステップ (apps 変更と
  無関係に常に回る純 Python 群) に 1 行足すだけで配線でき、verify の grep も同時に満たる。
- 宣言の場所: **rules.json は触らず**、新規の専用データファイルまたはスクリプト内定数に持つ。
  `allowed_autopilot_doppler_keys` は「Job に注入してよい鍵」という別の意味論を持つので混ぜない。
- 既知の死角: helm chart がレンダリングする Secret 参照 (immich values.yaml 由来等) は
  静的スキャンに映らない。P-0047 の test_backup_coverage と同じ流儀で docstring に明記する。

### 決めてあること

突き合わせの対象は最低でも 3 種: (1) ExternalSecret `remoteRef.key` ↔ Doppler キーの宣言、
(2) ExternalSecret `target.name` ↔ k8s Secret 名の宣言、(3) Deployment/CronJob
`secretKeyRef.name` ↔ 「その Secret を誰が作っているか」。未宣言エントリの追加で exit 非ゼロ。
判定ロジックは純関数に分け、合成入力で「違反を落とす」ことを固定テストする
(`ops/tests/` の discover 対象に寄せると ci.yml 追記が最小で済む)。

## やらないこと

- **apps/ 配下 manifest の変更**。参照の足し引き・リネームはしない。地図を作るだけで地形は変えない
- **rules.json / validate.py の変更**。autopilot 用 allowlist の意味論には触れない (1 PR 1 論点)
- **Doppler 側・クラスタ側の実測**。Secret の中身や同期状態は読まない。静的解析に留める
- **helm レンダリング分の網羅**。死角を明記するだけで埋めない (CI に helm を入れる判断は別論点)
- **ci.yml の構造変更**。"consistency checks" への追記のみ。新しい job は作らない
  (ruleset の必須チェック追加は人間専有)
- **`ops/backlog.json` / `ops/state.json` / `ops/journal/` の更新**。heart が直接 push する領域で
  コンフリクトする (CLAUDE.md)
