# P-0085 — 写真を預ける窓口 (sync フォルダ → 自動取り込み) を実データで開通させ、immich の受け入れ側を完成させる

## 目的

immich-library の実使用は約 373 MB (health pvc_usage、P-0056 時点 357 MB からほぼ無成長)。
人間はまだ写真を預けておらず、段階 2 の完了判定「人間が満足を表明」はこのままでは来ない。
器はここまで immich の「壊れない側」(DB バックアップ・CrashLoop 宿題) ばかり固めてきた。
「預けない理由」最大の要素は取り込みの手間なので、人間が sync フォルダに放り込むだけで
immich に現れる経路を作り、テスト画像 1 枚の end-to-end 通過で証明する (P-0056 の調査的
視点を具体機構に替えた大胆版。P-0056 自体は未採択 — archive.jsonl 参照)。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing**（2026-08-22、`project/p-0085`
の checkout で、リポジトリルートから実行）。

- [ ] `test -f apps/syncthing/photo-intake-cronjob.yaml`
  — intake CronJob の manifest が apps/syncthing 配下に存在すること。実測 rc=1
    （ファイルが無い）。kustomization.yaml への配線はこの verify の射程外だが、
    置いただけでは ArgoCD は同期しないので配線必須（P-0047 の教訓）。
- [ ] `python3 -m unittest ops.tests.test_photo_intake`
  — intake 機構の機械検査テストが存在し green であること。実測 rc=1
    (`ModuleNotFoundError: No module named 'ops.tests.test_photo_intake'`)。
    CI は `python3 -m unittest discover -s ops/tests -t .` で自動拾いするので
    (.github/workflows/ci.yml:58)、ファイル名は `ops/tests/test_photo_intake.py`
    で固定（verify コマンドがモジュールパスを名指ししている）。
- [ ] `python3 -c "import json; p=json.load(open('ops/projects/logs/P-0085/e2e-proof.json')); assert p['assets_after']>p['assets_before'] and p.get('via')=='photo-intake'"`
  — **実クラスタでの end-to-end 通過の実績記録**があること。実測 rc=1
    (`FileNotFoundError`)。`assets_before` < `assets_after`（取り込み前後の assets 数比較）
    かつ `via == "photo-intake"`（intake 経路で入ったこと）を JSON で残す。

## 設計方針

### 前提（initializer が 2026-08-22 に実読・実測した。調べ直さなくてよい）

- **置き場所と PVC**: intake フォルダは syncthing-data PVC 上に置く
  （`apps/syncthing/pvc.yaml` — 20Gi / local-path / `Prune=false`、syncthing Pod は
  `/var/syncthing` にマウント済み）。CronJob も syncthing namespace・同 PVC マウントで
  読む。node01 単一ノードなので local-path (hostPath ベース) の RWO でも
  Deployment と CronJob pod の同時マウントは競合しない。
- **immich server への到達**: namespace 越しの cluster 内 DNS で到達できる
  （chart releaseName は `immich`。Service 名は chart 由来の `immich-server` を想定するが、
  worker が `kubectl get svc -n immich` で実名・ポートを確認してから書くこと）。
- **immich CLI には公式 Docker イメージがある**
  （docs.immich.app/features/command-line-interface, 2026-08-22 実閲覧）:
  `ghcr.io/immich-app/immich-cli`。env `IMMICH_INSTANCE_URL`（サーバ URL + `/api`）と
  `IMMICH_API_KEY` だけで認証でき、`immich upload --recursive <dir>` でアップロードする。
  **サーバ側に checksum ベースの重複排除が組み込みである**（同一ファイルの再 upload は
  スキップされる）。タグは repo 流儀どおり pin する（`:latest` は使わない）。
- **API キーは現状どこにも無い**（repo 全体 grep で `IMMICH_API_KEY` 等はゼロ件）。
  発行は immich Web UI（User Settings > API Keys）が公式手順で、エージェント側に
  発行手段は無い。Doppler (`homelab/prd`) に登録 → ExternalSecret → CronJob が env で参照、
  というのが既存の型（`apps/syncthing/restic-external-secret.yaml` 同型。
  ClusterSecretStore `doppler` はクラスタスコープなので新規登録なしで引ける）。
  CHARTER §3 の T-0049 型（manifest 先行・キー名決め打ちで依頼）で進める。
  **この credential 待ちが唯一の人間待ちポイントであり、verify #3 (end-to-end) に必須なので
  worker は着手早期に依頼を出すこと**（blocked をタスク全体ではなく「最後の一歩」で止める）。
- **テストの様式は `ops/tests/test_backup_coverage.py` 流儀**: manifest 静的走査 +
  純関数は合成入力で両方向（落ちること/通ること）を固定。「実 repo だけを見るテストは
  今たまたま通っていると正しいを区別できない」。見張るべき既知の失敗形は
  「kustomization.yaml resources への配線忘れ」。
- **substrate 制約**（ops/memory/substrate.md）: memory limits は実測なしに付けない /
  `spec.timeZone` 未指定の schedule は JST 評価 / 同名で再適用する検証用 Job には最初から
  `argocd.argoproj.io/sync-options: Force=true,Replace=true` / apps root Application は
  `prune: true`。

### 決めてあること（この方針で作る。変えるなら理由を PROGRESS.md に書く）

1. **CronJob は `apps/syncthing/photo-intake-cronjob.yaml`**、namespace `syncthing`。
   syncthing-data を `/var/syncthing` にマウントし、intake ディレクトリは
   `/var/syncthing/photo-intake`。CronJob 冒頭で `mkdir -p` すればフォルダ作成のための
   別 Job は要らない。`apps/syncthing/kustomization.yaml` の resources に追加する
   （verify #1 の射程外だが配線しないと存在しないのと同じ）。
2. **処理フローは「upload 成功分だけ done/ へ移動」**:
   `photo-intake/` 直下の画像・動画を `immich upload --recursive` し、成功したファイルだけを
   `photo-intake/done/` へ移動する。失敗分は元の場所に残留し次回リトライ。
   サーバ側 checksum 重複排除（CLI 前提で確認済み）+ ローカル移動の二重防止で
   DoD の「重複取り込みの防止」を満たす。done/ 移動は PVC 内 mv なので可逆。
3. **ExternalSecret を syncthing namespace に追加し、Doppler キー `IMMICH_API_KEY`
   を参照する**（キー名は PR 内でこれに固定。needs_human_reason には「このキー名で
   Web UI で発行した鍵の値を登録してほしい」と具体的に書く）。
4. **assets 数の before/after は immich API または postgres で数えてよい**
   （例: `/api/assets/statistics` は同一キーで読める想定。worker が実機で動く方を選ぶ）。
   e2e の検証 Job は同名再適用になるので Force=true,Replace=true を付ける。
   結果は `ops/projects/logs/P-0085/e2e-proof.json` に verify #3 のスキーマ通りの形で残す。
5. **CronJob の schedule は高頻度・排他**（例: 10 分毎 + `concurrencyPolicy: Forbid`）。
   「放り込んだらそのうち現れる」体験が目的なので日次では遅い。resources は requests のみ
   （memory limits 無し方針）、CPU limits は可。
6. **人間向け 1 ページは `docs/photo-intake.md`**。預け方（どのフォルダに何を置くか）/
   反映までの時間 / 重複時にどうなるか / 元ファイルを消してよい条件
   （done/ 移動後、かつ syncthing-data の restic backup が追いついた後）を上から順に書く。

### ロールバック

追加のみの変更（CronJob・ExternalSecret・docs・テスト）。revert すれば消えて元通りで、
DB スキーマや既存データには触れない。done/ へ移動済みのファイルも PVC 内 mv なので手で戻せる。

## やらないこと

- **スマホ等デバイスと syncthing フォルダの実際のペアリング設定**。GUI 作業であり人間側の
  手順。docs に書くまでがこちらの仕事で、ペアリング実行は含まない。
- **既存写真数百 GB の一括移行・大規模投入の実測**（P-0056 的スコープ）。今回はテスト画像
  1 枚の end-to-end 証明まで。速度・リソース影響の定量実測は別論点。
- **immich chart / server / CLI イメージのバージョン更新、machine-learning の resources
  調整**。1 PR 1 論点。
- **syncthing-data の restic backup 対象の見直し**。intake も同じ PVC 上なので既存 backup の
  スコープ内。変更が不要なら触らない。
- **`ops/backlog.json` / `ops/state.json` / `ops/journal/` の更新**。autopilot が直接 main に
  push するファイルでコンフリクトする（CLAUDE.md）。気づいたことは PROGRESS.md に書いて渡す。
