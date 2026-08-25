# P-9062 — 進捗記録

## 2026-08-25（履歴エントリの壊れ (used_bytes 欠落/非数値) が予報をクラッシュさせ fill_days 契約を壊す経路を塞いだ。`_usable_samples` を新設し forecast を硬化。実装は完、受入検証は verify[1] green / verify[0] は構造的不可能のまま）

### やったこと

- **前 11 セッションの「ローカルでやることは残っていない」を信用せず全量を自分の手で再検証した。**
  実装は完 (後述の verify 実測)、受入検証の現地は従来どおり verify[1] green / verify[0] 構造的不可能。
  その上で、**新たに実リスクを 1 つ見つけて塞いだ**:
- **`root_disk_usage.py` の履歴の壊れ耐性を強化**。ConfigMap の `root_disk_history.json` を読む
  `_read_root_disk_history` は「list である」までしか検証しないため、個別エントリが
  **ts はあるが `used_bytes` が欠落・非数値**の状態だと `daily_increase_bytes` が `KeyError`
  (`s["used_bytes"]`) を漏らし、`collect()` が root_disk 節全体を `{"error": "KeyError: ..."}` に
  していた — **fill_days キーが消え、受入検証 (kubectl 側) の assert が落ちる**。
  前セッションが summary の JSON パース失敗 (ValueError) を fallback に含めたのと同じ論理:
  「root_disk 節は必ず正規の section + fill_days キーを持つ」。今回の経路は `_num` の docstring
  「1 項目の壊れで計測全体を止めない」という設計思想に反していた（実測で再現確認済み:
  used_bytes 欠落エントリ入り履歴 → `daily_increase_bytes CRASH: KeyError 'used_bytes'`）。
- **修正**: `_usable_samples(samples)` を新設 (ts が解釈不能、または `used_bytes` が数値でない
  サンプルを捨てる) し、`daily_increase_bytes` と `forecast` の note 分岐をそれでフィルタするよう
  変更。`ys` は `_num` で正規化 (数値文字列 "123" の混入で `y - my` が TypeError にならないよう)。
  canonical (ops/tools) と apps 側コピーの両方を同じ PR で修正 (drift check が一致を確認済み)。
- **`ops/tests/test_root_disk_usage.py` に回帰テスト 2 本追加** (20 tests に):
  `test_corrupt_used_bytes_sample_is_dropped` (壊れエントリを捨てて健全な 2 点から 100.0/day
  を計算) / `test_forecast_with_corrupt_history_keeps_fill_days_contract` (壊れた履歴でも
  build_report は source=statvfs の正規 section + fill_days キーを返す)。

### verify 実測（全てこのセッションで実行）

- `python3 ops/tools/root_disk_usage.py --check` → rc=0（**受入検証の 1 項目は green**）
- wrapper の verify[0] をこの環境でリテラル実行 → 従来どおり JSONDecodeError（空入力）。
  この pod にクラスタ資格情報が無い（SA token / kubeconfig 無し、`/var/run/secrets/
  kubernetes.io/serviceaccount` 不在を再実測）ため構造的に実行不能 — **verify[0] は変わらず
  2 重に構造的不可能**（jsonpath ドットキー + wrapper 資格情報無し。両方 CI 固定済み）
- `python3 -m unittest ops.tests.test_root_disk_usage -v` → **20 tests OK** (前回 18 + 新規 2)
- `python3 -m unittest ops.tests.test_report_root_disk` → **8 tests OK** (受入検証コマンドを
  kubectl 偽物 + 実 kubectl で固定する本を含む)
- `python3 -m unittest discover -s ops/tests -t .` → **613 OK** (前回 611 + 新規 2)、
  ops/heart/tests → 448 OK、ops/runner/tests → 53 OK
- `python3 ops/check_root_disk_usage_script_sync.py` → 一致 OK、`diff` canonical/コピー → 一致
- `kubectl kustomize apps/ops-health-reporter` → build OK（nodes/proxy + nodes/stats が
  resourceNames ["node01"] のまま）
- `python3 ops/validate.py` → 0 error（warning 11 は全て既存・対象外）
- 実環境の計測経路: `python3 ops/tools/root_disk_usage.py --node node01 --json` → rc=0、
  source=statvfs、fill_days=None + note「履歴が 2 点に満たない」(設計どおり)

### 分かったこと（実測・調査）

- **履歴の壊れ耐性は「list 検証」で止まっていた**。`_read_root_disk_history` は例外を握って
  空履歴に巻き戻すが、個別エントリの形状 (used_bytes の存在/数値性) は検証していなかった。
  今回は「ts 正常 + used_bytes 欠落」という自然に起きうる形状で予報がクラッシュすることを実測。
  修正後は壊れたエントリを捨てて健全なサンプルから予報する（samples カウントは生の件数を
  表示したまま — 予報は健全分のみ）。
- **この修正は spec 本文 (dod) の「実測と予報を latest.json の root_disk 節に載せる」を
  守るための追加**で、前セッションの summary パース fallback と同じスコープ (fallback/壊れ耐性)
  に閉じている。受入検証は変わらず「root_disk 節が常に fill_days キーを持つ」を満たす。

### 発見（スコープ外、curriculum へ）

- なし（dashboard_smoke の no-lie-coexistence 論点は据え置き）。

### 次のセッションへ（レビューで差し戻されたら）

- **ローカルで追加実装できることは残っていない（前回までの結論は今回も維持。今セッションは
  その上で見つけた履歴壊れ経路を塞いだ）。** 受入検証の残り 1 項目 (kubectl) は verify[0] が
  2 重に構造的不可能（①wrapper=runner Job にクラスタ資格情報が無い → 常に空出力、②spec の
  jsonpath `{.data.latest.json}` は実 kubectl でドットキーを解けない → `\.` エスケープが要る）。
  worker は spec を変えられない。**このままでは wrapper は verify ゲートで max_sessions まで
  回し続け、その後 heart が session_limit + question を人間へ出す**（runner.py:1015
  `done = all(v["ok"] for v in verify) if verify else session_done`）。人間の判断材料はこの
  PROGRESS.md。
- **修正の方向は dispatch / 所有者が決める**（2 案は 7c1e7caa の記録参照）:
  (a) 所有者判断 (2026-08-24) どおり dispatch 由来の verify を外す → wrapper が PR を出し、
  独立レビュー (reviewer Job はクラスタ read 権限を持つ) と CI に完成の判断を移す
  (b) verify[0] を worker 環境で実行可能な形に置き換える (例: `python3 -m unittest
  ops.tests.test_report_root_disk`。P-9037 流)
- 差し戻されたら従来どおり以下を疑う: nodes/proxy + nodes/stats の resourceNames が node01 の
  ままか（回帰テストあり）/ configMapGenerator sync の自愈待ち / 受入検証コマンド形状の drift
  （リテラル実行テストあり）/ 履歴エントリの壊れ（今回の `_usable_samples` が縛る）。
- **merge 後に確認すること**（従来と変わらず）: reporter が 1 回走る → `root_disk.source` が
  kubelet_summary になるか（RBAC nodes/proxy+stats の通し。取れていれば breakdown の images/PVC
  が載り、取れなくても statvfs 総量 + None で正常動作）、1 日分の履歴が溜まったら fill_days が
  数値になるか（MIN_WINDOW_DAYS=1.0）。実測したら substrate.md を更新する。

---

## 2026-08-25（wrapper 環境にクラスタ資格情報が無いことを実地で確定。verify[0] は 2 重に構造的不可能。前 9 セッションの「merge 後に green」見込みを訂正）

### やったこと

- **決定的な新発見: この sandbox は runner Job pod そのもので、クラスタ資格情報を持たない。**
  `HOSTNAME=runner-p-9062-a1-7pldr` / `RUNNER_MODE=worker`。`/var/run/secrets/kubernetes.io/serviceaccount`
  は存在しない（SA token 無し）、`~/.kube/config` も無い（cache のみ）。`kubectl get cm ...` は
  localhost:8080 に接続拒否（既定 kubeconfig）。**wrapper の run_verify() はこの同じ pod で
  `bash -c <spec verify>` を実行する**（runner.py run_verify → subprocess、cwd=repo_dir）。つまり
  `kubectl get cm -n autopilot ops-health-report` は**構造的に認証できず、常に空出力**になる。
- **裏付け**: ops/heart/spawn.py:38 `automount = use_writer or kind == "reviewer"`。P-9062 の
  capabilities は空（`"capabilities": []`）なので runner Job は `automountServiceAccountToken: false`
  （決定 #5: worker はクラスタ API に触れない）。ops/heart/tests/test_gate.py:126
  `test_job_never_gets_the_write_service_account` が `assertFalse(pod["automountServiceAccountToken"])`
  で機械的に縛っている。
- **前 9 セッションの結論を訂正**: 「merge 後に wrapper 環境で reporter が 1 回走れば verify[0] は
  green になる」は**不可能**。reporter が ConfigMap を正しく書いても、wrapper はそれを読む資格情報を
  持たない。**検証の実測環境（wrapper = クラスタ非接続）と検証対象（クラスタ内 ConfigMap）が分離されて
  いるのが構造的原因**。これは仕様レベルの問題で、worker のブランチからは何も直せない。
- **verify[0] は 2 重に構造的不可能**:
  1. wrapper 環境にクラスタ資格情報が無い（今回実地で確定。spawn.py / test_gate.py が CI 固定）。
     エスケープ済み jsonpath でも無理。
  2. spec の jsonpath `{.data.latest.json}` が実 kubectl で解けない（ドット=入れ子区切り。`\.` が要る）。
     実 kubectl + mock apiserver のテストで CI 固定済み。
- **verify[1] は green** のまま（`python3 ops/tools/root_disk_usage.py --check` → rc=0）。
- 実装（report.py の root_disk 節・履歴の同一 PUT・RBAC node01 限定・statvfs fallback）は変更なし。
  再度全量検証して全て green を実測した。

### verify 実測（全てこのセッションで実行）

- `python3 ops/tools/root_disk_usage.py --check` → rc=0（**受入検証 1 項目は green**）
- wrapper の verify[0] を**この環境（wrapper と同じ pod）でリテラル実行** → rc=1、wrapper の実測出力と
  **完全に同一**の JSONDecodeError（空入力）を再現
- `python3 -m unittest ops.tests.test_report_root_disk -v` → **8 tests OK**（実 kubectl 2 本を含む）
- `python3 -m unittest discover -s ops/tests -t .` → **611 OK**、ops/heart/tests → 448 OK、
  ops/runner/tests → 53 OK
- `python3 ops/check_root_disk_usage_script_sync.py` → 一致 OK、`diff` canonical/コピー → 一致
- `kubectl kustomize apps/ops-health-reporter` → build OK（nodes/proxy + nodes/stats が
  resourceNames ["node01"] のまま）
- `python3 ops/validate.py` → 0 error（warning 11 は全て既存・対象外）

### 分かったこと（実測・調査）

- **P-9037（先例）との差が確定した**: P-9037 の verify は `--check` + unittest のネットワーク非依存
  で、wrapper 環境でも green にできた。P-9062 の verify[0] はクラスタ read が要る verify で、wrapper
  には資格情報が無い。**「verify が wrapper 環境で実行できる」ことが受入検証の前提であり、これを満たさ
  ない verify は spec 採択時に排除されるべき**（所有者の 2026-08-24 判断「dispatch 由来の仕様は verify
  を持たない。verify を書くのも LLM なので迂回できる検査だったため外した」は、まさにこの問題への応答）。
- **このプロジェクトは wrapper の verify ゲートで永久停止する**。runner は verify 全 green まで worker
  セッションを回し続け、max_sessions_per_project に達すると heart が session_limit で stalled +
  question（「同じところを回り続けている可能性があります。続ける価値があるか判断してください」）を
  人間へ出す（ops/heart/reconcile.py:636）。つまり**この記録が人間の判断材料**になる。
- 実装側は今も完成・堅牢: DoD の残要素は無い。内訳（images/PVC は summary 経由、k3s/containerd/ログ は
  None=計測不能）・fill_days（履歴 1 日分まで None + note）・取得源（statvfs は実環境実測済み、summary
  は RBAC 追加）・履歴の壊れ耐性は全て CI 固定済み。

### 発見（スコープ外、curriculum へ）

- **curriculum への教訓（強）**: verify を書くときは「wrapper（runner Job, automount=false）で実行
  できるコマンド」にすること。クラスタ read（`kubectl get cm ...` 等）が要る verify は wrapper では
  永遠に green にならず、プロジェクトが verify ゲートで永久停止する（P-9062 が実例）。P-9037 の
  `--check` / unittest パターンが正。クラスタ内の状態を検証したいなら、それを wrapper の verify に
  するのではなく DoD として実装し、CI（mock 固定）または merge 後の実測で確認する。

### 次のセッションへ（レビューで差し戻されたら）

- **最初にやること**: spec の verify が修正されたか wrapper の verify 実測で確認する。修正されない限り
  このプロジェクトは verify ゲートで永久に停止する。**ローカルで追加実装できることは何も無い（10 回目の
  確認）。** 実装は DoD を満たし、全 CI 相当ゲートが green。
- **修正の方向は dispatch / 所有者が決める**。現実的な選択肢は 2 つ:
  (a) 所有者判断どおり dispatch 由来の verify を外す → wrapper が PR を出し、独立レビュー（reviewer Job
  はクラスタ read 権限を持つ）と CI に完成の判断を移す
  (b) verify[0] を worker 環境で実行可能な形に置き換える（例: `python3 -m unittest
  ops.tests.test_report_root_disk` のような、reporter の出力契約を mock で固定するテスト。P-9037 流）
- 差し戻されたら従来どおり以下を疑う: nodes/proxy + nodes/stats の resourceNames が node01 のままか
  （回帰テストあり）/ configMapGenerator sync の自愈待ち / 受入検証コマンド形状の drift（リテラル実行
  テストあり）。
- **merge 後に確認すること**（従来と変わらず）: reporter が 1 回走る → `root_disk.source` が
  kubelet_summary になるか（RBAC nodes/proxy+stats の通し。取れていれば breakdown の images/PVC が載り、
  取れなくても statvfs 総量 + None で正常動作）、1 日分の履歴が溜まったら fill_days が数値になるか
  （MIN_WINDOW_DAYS=1.0）。実測したら substrate.md を更新する。

---

## 2026-08-25（spec の verify[0] ブロッカーを「実 kubectl + mock apiserver」で CI に閉じた。再現手順の独立確認つき）

### やったこと

- **`ops/tests/test_report_root_disk.py` に実 kubectl を使うテストを 2 本追加**。
  従来の `KUBECTL_SHIM`（Python で kubectl の jsonpath 解釈を模す偽物）は前セッションで
  実解釈に忠実化済みだが、**偽物が実 kubectl からずれていたら CI が緑のままブロッカーが
  隠れる**余地が残っていた。今回、**mock apiserver（discovery + ConfigMap GET の最小
  実装）に report.py の main() が実際に書く ConfigMap を配信させ、実 kubectl v1.35.0 で
  spec の verify[0] コマンドを一字も崩さず実行**するテストを追加した（kubectl が無ければ
  skip。CI の ubuntu-latest には含まれる）:
  - `test_real_kubectl_spec_verify_verbatim_unsatisfiable` — spec の
    `{.data.latest.json}` は実 kubectl では入れ子解釈になり空出力 → JSONDecodeError
    (rc=1)。**正しく populate された ConfigMap（root_disk + fill_days 入り）でも通らない**。
  - `test_real_kubectl_escaped_verify_passes` — エスケープ形 `{.data.latest\.json}`
    なら同じ ConfigMap で rc=0（spec 修正後の green 形）。
- **再現手順を独立に再実行した**（mock apiserver + kubeconfig を mktemp に用意 → 実
  kubectl で 2 形を比較）。wrapper の verify[0] 実測出力（`JSONDecodeError: Expecting
  value: line 1 column 1 (char 0)`）と**完全に同一**の失敗を実バイナリで再現した。
  つまり「クラスタ到達・ConfigMap 内容・reporter 実装が全て正しくても spec の verify[0]
  は通らない」ことが、実機で確定した。

### verify 実測（全てこのセッションで実行）

- `python3 -m unittest ops.tests.test_report_root_disk -v` → **8 tests OK**（前回 6 + 新規 2）
- `python3 -m unittest discover -s ops/tests -t .` → **611 OK**（前回 609 + 新規 2）
- ops/heart/tests 448 OK、ops/runner/tests 53 OK
- `python3 ops/tools/root_disk_usage.py --check` → rc=0（**受入検証の 1 項目は green**）
- `python3 ops/check_root_disk_usage_script_sync.py` → 一致 OK、canonical と apps/ コピー diff 一致
- `python3 ops/validate.py` → 0 error（warning 11 は全て既存・対象外）

### 分かったこと（実測・調査）

- **ブロッカーは 1 点に確定し、その証拠が実バイナリに昇格した。** spec の verify[0] の
  jsonpath を `{.data.latest.json}` → `{.data.latest\.json}` にエスケープ修正するだけ。
  worker は採択済み spec を変えられない（dispatch の領分）。修正されれば wrapper 環境で
  reporter（CronJob 30 分毎）が 1 回走って ConfigMap が書かれた時点で verify[0] は green
  になる（`test_real_kubectl_escaped_verify_passes` がその形を実バイナリで固定済み）。
- 実装側（report.py の root_disk 節 + fill_days、履歴の同一 PUT、RBAC node01 限定、
  statvfs/summary の fallback）は今回も問題なし。受入検証のもう 1 項目
  （`root_disk_usage.py --check`）は green。

### 発見（スコープ外、curriculum へ）

- なし（dashboard_smoke の no-lie-coexistence 論点は据え置き）。

### 次のセッションへ（レビューで差し戻されたら）

- **最初にやること**: spec の verify[0] が修正されたか確認する（wrapper の verify 実測で
  判断できる。修正前は必ず JSONDecodeError）。修正されていない限り、このプロジェクトは
  verify が永久に red で wrapper がレビューへ進めない — **コード変更では解けない**。
- 修正されていれば: `test_real_kubectl_escaped_verify_passes` が green のまま、あとは
  wrapper 環境で reporter が 1 回走り ConfigMap が書かれるのを待つだけ。
- ローカルでやることは残っていない。実測の再現手順: mktemp に mock apiserver
  （discovery: /api・/apis・/api/v1（configmaps + shortName cm）+ ConfigMap GET）と
  kubeconfig（server http://127.0.0.1:port）を用意し、実 kubectl で
  `get cm -o jsonpath='{.data.latest.json}'` と `{.data.latest\.json}` を比較
  （前者は空出力・後者は内容が出る）。テストファイルに MockAPIServer として収録済み。
- 差し戻されたら従来どおり以下を疑う: nodes/proxy resourceNames=node01（回帰テストあり）/
  configMapGenerator sync 自愈待ち / コマンド形状の drift。
- **merge 後に確認すること**: `root_disk.source` が kubelet_summary になるか（RBAC
  nodes/proxy+stats の通し）、1 日分の履歴が溜まったら fill_days が数値になるか
  （MIN_WINDOW_DAYS=1.0）。実測したら substrate.md を更新。

---

## 2026-08-25（受入検証 verify[0] の jsonpath が実 kubectl では解けないことを mock apiserver で実測。spec レベルのブロッカーを発見し CI テストを真実に直した）

### やったこと

- **「verify[0] はクラスタ到達が解決すれば green になる」という前 5 セッションの結論が誤りであることを実測で確定した。**
  spec の verify[0] `kubectl get cm -n autopilot ops-health-report -o jsonpath='{.data.latest.json}'` は、
  **実 kubectl の jsonpath では決して green にならない**。実 kubectl は `.` を**入れ子フィールド区切り**と
  解釈するため `{.data.latest.json}` は `data["latest"]["json"]` を探す。ConfigMap の data キーは `latest.json`
  （リテラル、ドットはキー名の一部）なので **空出力**になり、後段の `json.load` が JSONDecodeError で落ちる。
- **証明（mock apiserver + 実 kubectl v1.35.0）**: Python のローカル HTTP サーバーで kube-apiserver を模し
  （`autopilot/ops-health-report` の data に `latest.json` = root_disk + fill_days 入り JSON を配信）、
  実 kubectl で `get cm -o jsonpath` をそのまま通した:
  - spec どおり `{.data.latest.json}` → **空出力（0 bytes）** → `JSONDecodeError: Expecting value: line 1
    column 1 (char 0)`。**wrapper の verify 実測出力と完全に同一**の失敗。
  - `{.data.latest\.json}`（エスケープ）→ root_disk + fill_days が読めて assert 通過（rc=0）。
  - つまり **クラスタ到達・ConfigMap 内容・reporter 実装が全て正しくても verify[0] は通らない**。
    仕様の verify[0] 自体の修正（jsonpath の `\.` エスケープ）が無い限り、このプロジェクトは受入検証を
    永久に満たせない。
- **既存の CI テストがこの罠を隠していた**: `test_report_root_disk.py` の kubectl 偽物は「jsonpath は
  リテラルキーを返す」と誤って模しており、「verify[0] はコマンド形状ごと CI 固定済み、残るはクラスタ到達のみ」
  という誤った結論を支えていた。**偽物を実 kubectl の jsonpath 解釈に忠実に書き直した**（ドット=入れ子・
  `\.`=リテラル・欠落フィールド=空出力）上で、テストを 2 本に分けた:
  1. `test_acceptance_kubectl_command_verbatim_unsatisfiable` — spec の verify[0] をリテラル実行し、
     **実 kubectl では空出力 → rc=1（JSONDecodeError）になる**ことを固定（ブロッカーを CI に閉じる）。
  2. `test_escaped_jsonpath_verify_command_passes` — CHARTER.md §5.5 の実測済み読み方 `{.data.latest\.json}`
     なら rc=0 で通ることを固定（verify[0] が修正されたときに green になる形）。
- **CHARTER.md §5.5 は既に正しい読み方を実測済みだった**: `kubectl -n autopilot get configmap
  ops-health-report -o jsonpath='{.data.latest\.json}'`。repo 側はドットキーの罠を知っていたが、
  spec の verify[0] には反映されていなかった。
- PROJECT.md の受入チェックリスト該当項目に追記（verify[0] が spec レベルの修正待ちである旨）。

### verify 実測（全てこのセッションで実行）

- `python3 -m unittest ops.tests.test_report_root_disk -v` → **6 tests OK**（前回 5。うち 1 本は
  verify[0] が実 kubectl で通らない事実の固定）
- `python3 -m unittest discover -s ops/tests -t .` → **609 OK**（前回 608 + 1）
- ops/heart/tests 448 OK、ops/runner/tests 53 OK
- `python3 ops/tools/root_disk_usage.py --check` → rc=0（**受入検証の 1 項目は green**）
- `python3 ops/validate.py` → 0 error（warning 11 は全て既存・対象外）
- `-W error::SyntaxWarning` で docstring のエスケープ警告なしを確認

### 分かったこと (実測・調査)

- **kubectl の jsonpath はドットキーを素直に読めない**。`{.data.latest.json}` は入れ子解釈、リテラルの
  ドットキーは `\.` でエスケープする。これは kubectl の標準動作で、repo 内の CHARTER.md §5.5 が実測済み。
- **受入検証の「残り 1 項目はクラスタ到達のみ」という結論は誤りだった。** 本当の残りは「spec の verify[0] の
  jsonpath 修正（エスケープ）」で、**worker の立場では直せない**（spec は dispatch が採択したもの）。
  reviewer / 所有者が spec の verify[0] を `{.data.latest\.json}` に直す必要がある。直せば merge 後に
  reporter が 1 回走って ConfigMap が書かれた時点で verify[0] は green になる
  （`test_escaped_jsonpath_verify_command_passes` がその形を固定済み）。
- 実装側（report.py の root_disk 節 + fill_days、履歴の同一 PUT、RBAC node01 限定）は今回も問題なし。

### 発見（スコープ外、curriculum へ）

- **dispatch が verify を書くときの既知の罠として「ドットキー（latest.json 等）を jsonpath で引く場合は
  `\.` エスケープが必要」を curriculum へ。** 今回のように CI の偽物が実 kubectl と異なる解釈をすると、
  偽物テストが green のまま spec の verify が永遠に落ちる。

### 次のセッションへ（レビューで差し戻されたら）

- **最初にやること**: spec の verify[0] の jsonpath を `{.data.latest.json}` → `{.data.latest\.json}` に
  修正できるか確認する（worker は spec を変えられない。reviewer / 所有者の判断）。修正されない限り verify[0] は
  merge 後も空出力のまま JSONDecodeError で落ち続ける。
- 修正されたら: `test_escaped_jsonpath_verify_command_passes` がその形を固定済み。あとは wrapper 環境で
  reporter が 1 回走り ConfigMap が書かれるのを待つだけ。
- 差し戻されたら従来どおり以下を疑う: nodes/proxy resourceNames=node01（回帰テストあり）/ configMapGenerator
  sync 自愈待ち / コマンド形状の drift。
- **merge 後に確認すること**: `root_disk.source` が kubelet_summary になるか（RBAC nodes/proxy+stats の通し）、
  1 日分の履歴が溜まったら fill_days が数値になるか（MIN_WINDOW_DAYS=1.0）。実測したら substrate.md を更新。
- 実測の手順は再現可能: ローカルに mock apiserver（mktemp）→ kubeconfig で kubectl を向ける →
  `get cm -o jsonpath` の 2 形（`{.data.latest.json}` / `{.data.latest\.json}`）を比較。

---

## 2026-08-25（5 回目の全量再検証セッション。コード変更なし — 前回までと同じ結論を実測で裏取り）

### やったこと

- 前回 (40169c9b) までの実装を**自分の手でゼロから再検証**した。前セッションの
  記録を信用せず、受入検証 2 項目の現在地と CI 相当ゲートを全て実測した。
  結論は前回までと同じ: **ローカルで追加実装できることは残っていない**。
- 受入検証の現在地 (wrapper 実測と一致):
  - `python3 ops/tools/root_disk_usage.py --check` → rc=0 (**green**)
  - `kubectl get cm ... | python3 -c 'root_disk + fill_days を assert'` → rc=1
    (JSONDecodeError: 空入力)。原因を再確認 — SA token がマウントされていない
    (`/var/run/secrets/kubernetes.io/serviceaccount` 無し)、kubeconfig の
    current-context 無し、apiserver (10.43.0.1:443) は 401 で到達できるが認証情報が
    無い。**クラスタ到達が無いだけで、実装起因の失敗ではない**。
- report.py の配線・RBAC・kustomize を**改めて全ファイル読んで**確認した:
  `collect_root_disk()` が latest.json の `root_disk` 節を書き、履歴は同一 ConfigMap の
  `root_disk_history.json` キーへ**同じ 1 回の PUT** で書き戻す (別 PUT だと 409)、
  CronJob は `serviceAccountName: ops-health-reporter` で SA token 自動マウント、
  ClusterRole の `nodes/proxy` / `nodes/stats` は両方 `resourceNames: ["node01"]` +
  verbs `["get"]` (kustomize build 出力を実測)。受入検証コマンドの形は
  `test_acceptance_kubectl_command_verbatim` が spec のままリテラル実行で縛っている。

### verify 実測 (全てこのセッションで実行)

- `python3 ops/tools/root_disk_usage.py --check` → rc=0 (**受入検証の 1 項目 green**)
- `python3 -m unittest ops.tests.test_report_root_disk -v` → 5 tests OK
  (受入検証コマンドのリテラル実行テスト含む)
- `python3 -m unittest discover -s ops/tests -t .` → **608 OK**、
  `ops/heart/tests` → 448 OK、`ops/runner/tests` → 53 OK
- consistency checks 10 本 → 全 OK (root_disk_usage sync 含む)
- `diff ops/tools/root_disk_usage.py apps/ops-health-reporter/root_disk_usage.py` → 一致
- `kubectl kustomize apps/ops-health-reporter` → build OK (ClusterRole の
  nodes/proxy + nodes/stats resourceNames ["node01"] を実測)
- `python3 ops/validate.py` → 0 error (warning 11 は全て既存・対象外)
- 実環境の計測経路: `python3 ops/tools/root_disk_usage.py --node node01 --json` →
  rc=0、source=statvfs (summary は SA token 無しで None → 意図どおり statvfs に倒れる)、
  capacity=270202880000 / used=78937239552 / free=180212092928、fill_days=None +
  note「履歴が 2 点に満たない」

### 分かったこと (実測・調査)

- 受入検証の kubectl 項目が落ちる理由は「root_disk が無い」実装起因ではなく**クラスタ
  到達不能のみ**。report.py 側の契約 (root_disk 節 + fill_days キー) は
  test_report_root_disk.py がクラスタ無しで固定済みで、残るのは「merge 後、wrapper 環境
  で reporter (SA token マウント済み) が 1 回走る」だけ。
- 取得源の設計どおりの fallback を実環境で確認: summary が取れなくても statvfs 総量 +
  fill_days=None + note で root_disk 節は必ず正規の section になる (受入検証はキー存在
  のみなので green)。

### 発見（スコープ外、curriculum へ）

- なし (dashboard_smoke の no-lie-coexistence 論点は据え置き)。

### 次のセッションへ（レビューで差し戻されたら）

- **ローカルでやることは残っていない (5 回目の確認)。** 受入検証の残り 1 項目
  (kubectl) は wrapper 環境で reporter が 1 回走った後、認証付きの文脈で green に
  なる。sandbox では SA token が無く実行不能 (今回も実測)。
- 差し戻されたら以下を疑う (優先順):
  1. `nodes/proxy` / `nodes/stats` の resourceNames が node01 のままか (回帰テスト
     TestRbac.test_kubelet_summary_proxy_resource_names_match_node が縛っている)
  2. ArgoCD が configMapGenerator を sync するまで reporter が旧 ConfigMap で走る
     自愈待ち (P-9037 と同じ。数回で治る)
  3. 受入検証コマンドの形 (jsonpath・namespace/name・パイプ) が spec からずれて
     いないか (test_acceptance_kubectl_command_verbatim が縛っている)
  4. summary 経路の例外が fallback をすり抜けていないか
     (FetchKubeletSummaryTest が縛っている)
- **merge 後 (wrapper 環境) に確認すること**:
  1. reporter が 1 回走る → `kubectl get cm -n autopilot ops-health-report -o
     jsonpath='{.data.latest.json}'` に `root_disk.source` と `fill_days` キー
     (初回 None) が載る → 受入検証 green
  2. `root_disk.source` が `kubelet_summary` になるか (RBAC nodes/proxy+stats の
     通し)。取れていれば breakdown の images/PVC が載り、取れなくても statvfs
     総量 + None で正常動作 (実測済みの fallback)。実測したら substrate.md を
     更新する。
  3. 1 日分の履歴が溜まったら fill_days が数値になる (観測窓 MIN_WINDOW_DAYS=1.0)。
     「予報が出ていない」と指摘されたら「1 日分の履歴が必要」を説明する。

---

## 2026-08-25（最終状態の全量再検証セッション。コード変更なし — 実装は完成済み）

### やったこと

- 前セッション (5a9f070e) までの実装を**改めて全量再検証**した。ローカルで回せる
  CI 相当ゲートは全て green を実測し、**sandbox で追加実装できることは何も残って
  いない**ことを再確認した (4 セッション連続で同じ結論。今セッションはコード
  変更なしで、その事実を自分の実測で裏取りした)。
- 受入検証 2 項目の現在地を自分の手で再測定した:
  - `python3 ops/tools/root_disk_usage.py --check` → rc=0 (**green**)。
  - `kubectl get cm ... | python3 -c 'assert root_disk + fill_days'` → rc=1
    (JSONDecodeError: 空入力)。原因は wrapper の実測どおり**クラスタ到達不能** —
    この sandbox は node01 上の pod だが SA token がマウントされていない
    (`/var/run/secrets/kubernetes.io/serviceaccount` が存在しない、実測)。kubectl
    自体は v1.35.0 が存在するが認証情報が無く、これ以上ローカルで詰められない。

### verify 実測 (全てこのセッションで実行)

- `python3 ops/tools/root_disk_usage.py --check` → rc=0 (**受入検証の 1 項目 green**)
- `python3 -m unittest ops.tests.test_report_root_disk -v` → **5 tests OK**
  (受入検証コマンド全体を kubectl 偽物でリテラル実行する test_acceptance_kubectl_
  command_verbatim 含む)
- `python3 -m unittest discover -s ops/tests -t .` → **608 OK**
- `python3 -m unittest discover -s ops/heart/tests -t .` → 448 OK、
  `python3 -m unittest discover -s ops/runner/tests -t .` → 53 OK
- consistency checks 10 本 (check_version_sync / check_pvc_usage_script_sync /
  check_download_ledger_script_sync / check_dashboard_smoke_script_sync /
  check_node_saturation_script_sync / **check_root_disk_usage_script_sync** /
  check_health_reporter_target / check_doc_commands / check_feedback /
  check_credential_map) → 全 OK
- `python3 ops/validate.py` → 0 error (warning 11 は全て既存・対象外)
- `diff ops/tools/root_disk_usage.py apps/ops-health-reporter/root_disk_usage.py` →
  一致 (canonical / コピーの drift なし)
- `kubectl kustomize apps/ops-health-reporter` → build OK。**ClusterRole の
  nodes/proxy / nodes/stats が両方 resourceNames ["node01"]** (nodes/proxy の
  resourceNames は node 名と照合される罠の修正後)、ConfigMap に root_disk_usage.py
  と ROOT_DISK_HISTORY_KEY が載ることを実測。

### 分かったこと (実測・調査)

- **受入検証の kubectl 項目の失敗は「root_disk が無い」実装起因ではなくクラスタ
  到達不能だけ**。report.py 側の契約 (root_disk 節 + fill_days キー) は
  test_report_root_disk.py が「受入検証コマンド全体を kubectl 偽物でリテラル実行」
  する形で CI 固定済み (main() 本体を実行するため配線の変化を検出できる)。merge 後
  wrapper 環境で reporter (SA token マウント済み) が 1 回走れば green になる想定。
- DoD の残要素は全て埋まっていることを再確認: 内訳 (images/PVC は summary 経由、
  k3s/containerd/ログ は None=計測不能を正直に) / fill_days 予報 (履歴が 1 日分
  溜まるまで None + note) / 取得源 (statvfs は実環境で実測済みの検証済み経路、
  kubelet summary は RBAC nodes/proxy+stats 追加済み)。
- 履歴の壊れ耐性も実装で担保済み: `_read_root_disk_history` は例外を握って空履歴に
  巻き戻す (report 全体が落ちない)、summary の JSON パース失敗は statvfs へ倒れる
  (root_disk 節が必ず正規の section + fill_days キーを持つ)。

### 発見（スコープ外、curriculum へ）

- なし (dashboard_smoke の no-lie-coexistence 論点は据え置き)。

### 次のセッションへ（レビューで差し戻されたら）

- **ローカルでやることは残っていない (4 回目の確認)。** 受入検証の残り 1 項目
  (kubectl) は wrapper 環境で reporter が 1 回走った後、認証付きの文脈で green に
  なる。この sandbox では SA token が無く実行不能。
- 差し戻されたら以下を疑う (優先順):
  1. `nodes/proxy` / `nodes/stats` の resourceNames が node01 のままか (回帰テスト
     TestRbac.test_kubelet_summary_proxy_resource_names_match_node が縛っている)
  2. ArgoCD が configMapGenerator を sync するまで reporter が旧 ConfigMap で走る
     自愈待ち (P-9037 と同じ。数回で治る)
  3. 受入検証コマンドの形 (jsonpath・namespace/name・パイプ) が spec からずれて
     いないか (test_acceptance_kubectl_command_verbatim が縛っている)
  4. summary 経路の例外が fallback をすり抜けていないか
     (FetchKubeletSummaryTest が縛っている)
- **merge 後 (wrapper 環境) に確認すること**:
  1. reporter が 1 回走る → `kubectl get cm -n autopilot ops-health-report -o
     jsonpath='{.data.latest.json}'` に `root_disk.source` と `fill_days` キー
     (初回 None) が載る → 受入検証 green
  2. `root_disk.source` が `kubelet_summary` になるか (RBAC nodes/proxy+stats の
     通し)。取れていれば breakdown の images/PVC が載り、取れなくても statvfs
     総量 + None で正常動作 (実測済みの fallback)。実測したら substrate.md を
     更新する。
  3. 1 日分の履歴が溜まったら fill_days が数値になる (観測窓 MIN_WINDOW_DAYS=1.0)。
     「予報が出ていない」と指摘されたら「1 日分の履歴が必要」を説明する。

---

## 2026-08-25（追加セッション: summary 経路の JSON パース失敗も statvfs fallback に含めた）

### やったこと

- **`root_disk_usage.fetch_kubelet_summary` の except に `ValueError` を追加**。
  従来は `(OSError, HTTPError, URLError)` だけを掴み、`json.load` の
  `JSONDecodeError` / `UnicodeDecodeError` (いずれも ValueError) は**漏れて**
  `measure()` の外へ伝播し、`collect()` が root_disk 節全体を
  `{"error": "JSONDecodeError: ..."}` にしていた。そうなると **fill_days キーが
  無くなり、受入検証 (kubectl 側) が「root_disk が無い/assert 失敗」で落ちる**。
  200 だが応答が JSON でない (apiserver 前段のプロキシが HTML を返す等) と
  発生しうる。設計上の契約は「summary が取れないなら None → statvfs へ倒す」
  (docstring に明記) なので、パース失敗も「取れない」として None に落とし、
  root_disk 節が必ず fill_days キーを持つことを保った。canonical と apps/ 側コピー
  の両方を修正 (drift check が一致を確認済み)。
- **`ops/tests/test_root_disk_usage.py` に `FetchKubeletSummaryTest` を追加** (3 テスト):
  - ネットワーク系失敗 (OSError / HTTPError 403) → None
  - 非 JSON 応答 (ValueError) → None (今回の修正)
  - 実測経路の結合: summary 取得が JSON パース失敗でも `build_report` が
    source=statvfs の section を返し fill_days キーを持つ (受入検証の契約)

### verify 実測

- `python3 ops/tools/root_disk_usage.py --check` → rc=0 (**受入検証の 1 項目は green**)
- `python3 -m unittest ops.tests.test_root_disk_usage -v` → **18 tests OK** (前回 15 + 新規 3)
- `python3 -m unittest discover -s ops/tests -t .` → **608 OK** (前回 605 + 新規 3)
- `python3 ops/check_root_disk_usage_script_sync.py` → 一致 OK
- `diff ops/tools/root_disk_usage.py apps/ops-health-reporter/root_disk_usage.py` → 一致
- `kubectl kustomize apps/ops-health-reporter` → build OK

### 分かったこと (実測・調査)

- **受入検証の kubectl 側が落ちるもう一つの経路を塞いだ**: 「root_disk が無い」
  は (a) reporter がまだ新コードで走っていない (merge 待ち) 以外にも、
  (b) summary 経路が例外を漏らして root_disk 節が `{"error": ...}` になる、でも
  起きる。今回の修正で (b) は構造的に無くなった — root_disk 節は summary 成功か
  statvfs のどちらでも必ず正規の section になり fill_days キーを持つ。
- 前セッションまでの「ローカルでやることは残っていない」はそのまま正しい。
  今回の修正はその上で見つけた**実リスクの塞ぎ**で、スコープは P-9062 の
  fallback 設計の範囲内 (summary → statvfs) に閉じている。

### 発見（スコープ外、curriculum へ）

- なし (dashboard_smoke の no-lie-coexistence 論点は据え置き)。

### 次のセッションへ（レビューで差し戻されたら）

- **ローカルでやることは残っていない。** 受入検証の残り 1 項目 (kubectl) は
  wrapper 環境で reporter が 1 回走った後、認証付きの文脈 (クラスタ到達) で green に
  なる。sandbox では apiserver は 401 で到達できるが認証情報が無く、実行不能。
- 差し戻されたら以下を疑う:
  1. `nodes/proxy` / `nodes/stats` の resourceNames が node01 のままか (回帰テスト
     TestRbac.test_kubelet_summary_proxy_resource_names_match_node が縛っている)
  2. ArgoCD が configMapGenerator を sync するまで reporter が旧 ConfigMap で走る
     自愈待ち (P-9037 と同じ。数回で治る)
  3. 受入検証コマンドの形 (jsonpath・namespace/name・パイプ) が spec からずれていないか
     (test_acceptance_kubectl_command_verbatim が縛っている)
  4. (新規) summary 経路の例外が fallback をすり抜けていないか
     (FetchKubeletSummaryTest が縛っている)
- **merge 後 (wrapper 環境) に確認すること**:
  1. reporter が 1 回走る → `kubectl get cm -n autopilot ops-health-report -o
     jsonpath='{.data.latest.json}'` に `root_disk.source` と `fill_days` キー (初回
     None) が載る → 受入検証 green
  2. `root_disk.source` が `kubelet_summary` になるか (RBAC nodes/proxy+stats の通し)。
     取れていれば breakdown の images/PVC が載り、取れなくても statvfs 総量 + None で
     正常動作 (実測済みの fallback)。実測したら substrate.md を更新する。
  3. 1 日分の履歴が溜まったら fill_days が数値になる (観測窓 MIN_WINDOW_DAYS=1.0)。
     「予報が出ていない」と指摘されたら「1 日分の履歴が必要」を説明する。

---

## 2026-08-25（追加セッション: nodes/proxy の resourceNames 罠を修正した）

### やったこと

- **`apps/ops-health-reporter/rbac.yaml` の罠を修正**: `nodes/proxy` の
  `resourceNames` を `["stats/summary"]` → `["node01"]` に変更。
  **nodes/proxy の resourceNames は node 名と照合される**（proxy サブパスとは照合
  されない）。`["stats/summary"]` にすると「stats/summary という名前の node」を指す
  ことになり、`GET /api/v1/nodes/node01/proxy/stats/summary` は 403 で拒否される
  （受入検証が「root_disk が無い」で落ち続ける場合、この罠が最初の疑い）。
  → summary 経路が通らず breakdown の images/PVC が永遠に None になる前の修正。
  `nodes/stats` も同じ理由で `resourceNames: ["node01"]` を追加（kubelet の
  SubjectAccessReview は resourceName に node 名を入れてくるため有効。旧コメントの
  「resourceNames で絞れない」は誤りで、逆に node01 へ絞れる）。
- **`ops/tests/test_health_report_path.py` に回帰テストを追加**:
  `TestRbac.test_kubelet_summary_proxy_resource_names_match_node` —
  nodes/proxy / nodes/stats の resourceNames が `["node01"]`・verbs が `["get"]`
  のみであることを機械で縛る（再び `["stats/summary"]` を入れる事故を防ぐ）。
- **`ops/memory/substrate.md`** の summary 経路の記述を実測値で更新（resourceNames
  の罠と修正後を記載）。
- report.py の notes の「RBAC を summary に限定して追加」→「node01 に限定して
  追加」に文言修正（summary 限定は nodes/proxy では不可能と判明したため）。

### verify 実測

- `python3 -m unittest ops.tests.test_health_report_path -v` → 7 tests OK
- `python3 -m unittest discover -s ops/tests -t .` → 604 OK（前回 603 + 回帰 1）
- `python3 ops/tools/root_disk_usage.py --check` → rc=0
- `kubectl kustomize apps/ops-health-reporter` → build OK（ClusterRole に
  nodes/proxy + nodes/stats の resourceNames `["node01"]` が載る）
- consistency checks（root_disk_usage sync ほか）OK

### 分かったこと（実測・調査）

- **`nodes/proxy` の `resourceNames` は node 名と照合される。** RBAC の authorization
  attributes では proxy サブリソースの Name 属性が node 名になる
  （403 の message が `cannot get resource "nodes/proxy"` + details.name が node 名
  で実証）。proxy サブパス（`stats/summary` 等）では絞れない — これは既知の非対称で、
  KEP-2862 (KubeletFineGrainedAuthz) が `/stats/*` を nodes/stats サブリソースに
  マップして解決する（kubelet 側の Webhook 認可は node 名で絞れる）。
- **apiserver 経由の summary 取得には両方が要る**: apiserver 側ゲート (nodes/proxy
  get + resourceNames node01) と kubelet 側 Webhook 認可 (nodes/stats get +
  resourceNames node01)。片方だけだと 403。

### 発見（スコープ外、curriculum へ）

- なし（前回から据え置きの dashboard_smoke no-lie-coexistence 論点のみ）。

### 次のセッションへ（レビューで差し戻されたら）

- **受入検証の残り 1 項目はやはりクラスタ到達が必要。** merge 後に reporter が
  1 回走れば green になる想定 (CronJob は 30 分毎)。「root_disk が無い」で落ち続ける
  場合は、最初に **nodes/proxy の resourceNames が node01 になっているか**
  （stats/summary のまま 403 → None で summary が落ちる罠）を疑う。
- **未実測の罠**: in-cluster で `root_disk.source` が本当に kubelet_summary になるか
  （RBAC nodes/proxy + nodes/stats の通し）は merge 後に reporter の実測で確認し、
  substrate.md を更新する。取れていれば breakdown の images/PVC が載り、取れなくても
  statvfs 総量 + None で正常動作。
- fill_days は履歴が 1 日分溜まるまで None (fill_days_note に理由)。「予報が出てない」と
  指摘されたら「1 日分の履歴が必要」を説明する。

---

## 2026-08-25（追加セッション: 受入検証の残り 1 項目の契約を CI で固定した）

### やったこと

- **`ops/tests/test_report_root_disk.py`** を新設 (4 テスト)。受入検証の残り 1 項目
  (`kubectl get cm ... ops-health-report ... root_disk + fill_days`) はクラスタ到達が
  要り sandbox では実行できないため、**report.py の main() を AST 抽出し k8s 層を偽物に
  差し替えて 1 周実行**し、書けた ConfigMap の `data[latest.json]` に受入検証の python 断片
  (kubectl 以外の部分) を**そのまま流して rc=0** を CI で固定した。「たぶん通る」を
  実測に変えたのが目的で、main() 本体を実行するので配線 (root_disk キー / fill_days /
  履歴書き戻し) の変化を検出できる。
- 追加で固定した契約: root_disk.source が kubelet_summary (RBAC 追加した nodes/proxy
  経路を root_disk_usage.k8s_get の差し替えでオフライン実行。内訳 images/PVC まで載る)、
  履歴は latest.json と**同じ 1 回の PUT** で root_disk_history.json に書かれる、初回 run
  は fill_days=None + fill_days_note あり (受入検証はキー存在のみなので green になる)。

### verify 実測

- `python3 -m unittest ops.tests.test_report_root_disk -v` → 4 tests OK
- `python3 -m unittest discover -s ops/tests -t .` → 603 OK (前回 599 + 新規 4)、
  ops/heart/tests 448 OK、ops/runner/tests 53 OK
- `python3 ops/tools/root_disk_usage.py --check` → rc=0
- consistency checks (root_disk_usage / node_saturation sync、health_reporter_target) OK
- ruff は sandbox に無いため未実行 (CI で F821 のみ。新ファイルは未定義名なしを手検証)

### 次のセッションへ（レビューで差し戻されたら）

- **受入検証の残り 1 項目はやはりクラスタ到達が必要。** 実装・契約は CI テストで固定した。
  wrapper 環境で reporter が 1 回走れば green になる想定 (CronJob は 30 分毎)。
  merge 後の最初の reporter run で `root_disk: {"error": ...}` でも ArgoCD の
  configMapGenerator sync まで数回で自愈する (P-9037 と同じ)。
- **未実測の罠は据え置き**: in-cluster で `root_disk.source` が本当に kubelet_summary
  になるか (RBAC nodes/proxy + nodes/stats) は merge 後に reporter の実測で確認し、
  substrate.md を更新する。テストは offline で summary 経路を固定したが、実 RBAC の
  通しは未検証。
- fill_days は履歴が 1 日分溜まるまで None (fill_days_note に理由)。「予報が出てない」と
  指摘されたら「1 日分の履歴が必要」を説明する。

---

## 2026-08-25（実装完了。verify 2 項目のうちローカル実行可能な方 (--check) は green）

### やったこと

- **`ops/tools/root_disk_usage.py`** (canonical) を作成。標準ライブラリのみ。
  純関数 (`sample_from_summary` / `sample_from_statvfs` / `append_sample` /
  `daily_increase_bytes` / `forecast` / `build_section` / `build_report`) と、
  ServiceAccount トークンでクラスタ到達する `measure()`、オフライン注入用 CLI
  (`--summary` / `--history`)、`--check` (ネットワーク非依存の自己検査) を持つ。
  - **総使用量**: kubelet stats/summary `node.fs` → pod 内 statvfs (`shutil.disk_usage`)
    の順に試し、後者で確実に取れる (下記の実測)。
  - **内訳**: イメージ (`node.runtime.imageFs.usedBytes`) と local-path PVC 相当
    (`node.pods[].volume[].fs.usedBytes` 合計。kubelet summary は SC を返さないため
    近似) は summary が取れたときだけ載る。k3s/containerd/ログ は非特権 pod から
    hostPath 無しでは読めないため **None (計測不能)** を正直に載せる。
  - **fill_days**: 履歴サンプル列 (ts + used_bytes) に最小二乗で日次増加量を当て、
    `free_bytes / 増加量` で残り日数を出す。観測窓が 1 日未満・増加が非正・履歴 2 点
    未満は None (予報不能) + `fill_days_note` に理由。
- **`apps/ops-health-reporter/root_disk_usage.py`**: canonical の同一内容コピー
  (configMapGenerator が /scripts に載せ、report.py から import)。drift は新設の
  **`ops/check_root_disk_usage_script_sync.py`** が CI で検出。ci.yml の consistency
  checks に追加。
- **`apps/ops-health-reporter/report.py`**: `collect_root_disk()` を追加 — latest.json の
  `root_disk` 節に内訳実測 + fill_days 予報を書く。履歴は同一 ConfigMap の別キー
  `root_disk_history.json` に保持し (PROJECT.md「履歴は root_disk の増加量計算に必要な
  最小限に閉じる」)、latest.json と**同じ 1 回の PUT** で書き戻す (別 PUT だと
  resourceVersion 競合の 409 になる)。main() の report dict・notes・kustomization.yaml
  の configMapGenerator にも追記。
- **`apps/ops-health-reporter/rbac.yaml`**: kubelet stats/summary 用に read-only RBAC を
  追加 — `nodes/proxy` get + resourceNames `["stats/summary"]` (apiserver 側ゲート)、
  `nodes/stats` get (kubelet の Webhook 認可用。kubelet は resourceName に node 名を
  入れて検査するため resourceNames で絞れない — stats サブリソース自体 read-only なので
  get のみで足りる)。
- **`ops/tests/test_root_disk_usage.py`**: 15 テスト。summary fixture のパース /
  計測不能の None / 履歴の追記と切詰め / 1 GiB/day fixture の予報 / 予報不能の理由
  note / build_report の section と履歴返却を固定。
- **`ops/memory/substrate.md`**: 「ルートディスクの計測経路 (P-9062)」節を追記。

### verify 実測

- `python3 ops/tools/root_disk_usage.py --check` → rc=0 (**受入検証の 1 項目は green**)
- `python3 -m unittest ops.tests.test_root_disk_usage -v` → 15 tests OK
- CI 相当: `python3 -m unittest discover -s ops/tests -t .` → 599 OK、ops/heart/tests
  448 OK、ops/runner/tests 53 OK、consistency checks 10 本全 ok、
  `kubectl kustomize apps/ops-health-reporter` build OK (ConfigMap に root_disk_usage.py、
  ClusterRole に nodes/proxy + nodes/stats が載ることを確認)。
- **受入検証の残り 1 項目 (`kubectl get cm ... ops-health-report ...`) はこの sandbox では
  実行不可** (kubectl がクラスタに到達できない、initializer と同じ)。実装は report.py の
  CronJob が 1 回走れば `root_disk` 節 + `fill_days` キー (初回は None だが存在) を
  ops-health-report に書く形で、クラスタ到達できる wrapper 環境で green になる想定。

### 分かったこと (実測)

- **pod 内の `df /` / `shutil.disk_usage("/")` は node01 のホストルートディスク全体を返す。**
  overlay の statfs は下層 (ホスト root ディスク) の値を透過する。この runner pod から
  overlay 251.65 GiB / used 74.07 GiB / free 167.28 GiB を実測 (node01 の 256GiB に整合)。
  → **statvfs が検証済みの総量取得源**。kubelet summary は RBAC 追加したが本セッションで
  は実測不可 (SA token 無し)。
- **非特権 pod から `/var/lib/rancher` は見えない** (`ls` で No such file)。k3s / containerd
  / ログの内訳は pod 内からは計測不能 → None を載せる設計が正しい。
- **summary 経路 (nodes/proxy + nodes/stats) は取れたら 1 回のフィクスチャ検証で良い**: 内訳
  は statvfs の総量とは独立で、summary が取れない間は images/PVC が None になるだけ
  (総量予報は止まらない)。merge 後に reporter の実測で source と breakdown を確認する。
- report.py の import (download_budget / node_saturation / root_disk_usage の 3 モジュール)
  は configMapGenerator が同じ /scripts に載せるので解決する。P-9037 と同型。

### 発見（スコープ外、curriculum へ）

- なし (dashboard_smoke の no-lie-coexistence 論点は P-9037 から据え置き)。

### 次のセッションへ（レビューで差し戻されたら）

- **受入検証の残り 1 項目はクラスタ到達が必要。** この sandbox では実行不可。wrapper 環境
  で reporter が 1 回走った後に green になる想定。もし「root_disk が無い」で落ち続けるなら
  merge 後の最初の reporter run 待ち (CronJob は 30 分毎) を確認する。
- **未実測の罠**: kubelet summary 経路 (nodes/proxy + nodes/stats の RBAC)。in-cluster で
  report の `root_disk.source` が `kubelet_summary` になるか確認し、substrate.md を更新する。
  取れていれば breakdown の images/PVC が載り、取れなくても statvfs 総量 + None で正常動作。
- **fill_days は履歴が 1 日分溜まるまで None** (fill_days_note に理由)。仕様の verify は
  キー存在のみなので初回 run で green になるが、「予報が出ていない」と指摘されたら
  「1 日分の履歴が必要」を説明する。
- merge 後、最初の reporter run で `root_disk: {"error": ...}` になっていても、ArgoCD が
  configMapGenerator を sync するまで数回で自愈する (P-9037 と同じ)。

---

## 2026-08-25（最終ローカル全量再検証。コード変更なし）

### やったこと

- 最終コミット (a1fde443) 以降の状態を**改めて全量再検証**した。ローカルで回せる
  CI 相当ゲートは全て green を実測し、受入検証の残り 1 項目 (kubectl) はクラスタ
  到達のみが残ることを確認。**sandbox で追加実装できることは何も残っていない。**
- 実測したゲート (全て rc=0):
  - `python3 ops/tools/root_disk_usage.py --check` → **受入検証の 1 項目は green**
  - `python3 -m unittest discover -s ops/tests -t .` → 604 OK
  - `python3 -m unittest discover -s ops/heart/tests -t .` → 448 OK
  - `python3 -m unittest discover -s ops/runner/tests -t .` → 53 OK
  - `diff ops/tools/root_disk_usage.py apps/ops-health-reporter/root_disk_usage.py` → 一致
  - consistency checks 10 本 (check_version_sync / pvc_usage / download_ledger /
    dashboard_smoke / node_saturation / **root_disk_usage** / health_reporter_target /
    doc_commands / feedback / credential_map) → 全 ok
  - `python3 ops/validate.py` → 0 error (warning 11 は全て既存・対象外)
  - `kubectl kustomize apps/ops-health-reporter` → build OK。**ClusterRole に
    nodes/proxy get + nodes/stats get、両方 resourceNames ["node01"]**、ConfigMap に
    root_disk_usage.py + ROOT_DISK_HISTORY_KEY が載ることを実測。
  - `python3 -m py_compile` 全対象 → OK
- ruff F821 は sandbox に無いため未実行 (CI が gate)。AST 手検査で未定義名なしを確認
  (loop 変数・引数は false positive)。

### 分かったこと (実測)

- **kubectl は sandbox からクラスタに到達できない** (localhost:8080 拒否) — 受入検証の
  残り 1 項目はここでは実行不能という wrapper の実測どおり。実装側の契約は
  test_report_root_disk.py が「受入検証の python 断片を main() の実出力にそのまま流す」
  形で CI 固定済みなので、wrapper 環境で reporter が 1 回走れば green になる。
- 仕様本文 (dod) の残要素の埋まり: 内訳実測 (images/PVC は summary 経由、k3s/containerd/
  ログ は None=計測不能) ✓ / fill_days 予報 ✓ / 取得源は statvfs 検証済み + summary は
  RBAC 追加 ✓。**「やったつもり」で終わっていないことはこの再検証で確認できた。**

### 発見（スコープ外、curriculum へ）

- なし (dashboard_smoke の no-lie-coexistence 論点は据え置き)。

### 次のセッションへ（レビューで差し戻されたら）

- **ローカルでやることは残っていない。** 差し戻されたら以下を疑う:
  1. `nodes/proxy` / `nodes/stats` の resourceNames が node01 のままか (回帰テスト
     TestRbac.test_kubelet_summary_proxy_resource_names_match_node が縛っている)
  2. ArgoCD が configMapGenerator を sync するまで reporter が旧 ConfigMap で走る
     自愈待ち (P-9037 と同じ。数回で治る)
- **merge 後 (wrapper 環境) に確認すること**:
  1. reporter が 1 回走る → `kubectl get cm -n autopilot ops-health-report -o
     jsonpath='{.data.latest.json}'` に `root_disk.source` と `fill_days` キー (初回
     None) が載る → 受入検証 green
  2. `root_disk.source` が `kubelet_summary` になるか (RBAC nodes/proxy+stats の通し)。
     取れていれば breakdown の images/PVC が載り、取れなくても statvfs 総量 + None で
     正常動作。実測したら substrate.md を更新する。
  3. 1 日分の履歴が溜まったら fill_days が数値になる (観測窓 MIN_WINDOW_DAYS=1.0)。
     「予報が出ていない」と指摘されたら「1 日分の履歴が必要」を説明する。

---

## 2026-08-25（受入検証コマンド全体を kubectl 偽物で CI 固定。ツールの実測経路を実環境で確認）

### やったこと

- **`ops/tests/test_report_root_disk.py` に受入検証コマンドのリテラル実行テストを追加**
  (`test_acceptance_kubectl_command_verbatim`)。従来のテストは受入検証の python 断片
  だけを検証していたが、残りの kubectl 側 (namespace/name・`-o jsonpath=
  '{.data.latest.json}'` の形・`2>/dev/null`・パイプ) が spec のコマンドから 1 文字でも
  ずれたら CI が拾えないまま wrapper の verify が「root_disk が無い」で永久に落ちる
  リスクがあった。kubectl を PATH に差し込んだ偽物に差し替え、**受入検証コマンドを
  spec のまま一字も崩さず実行**して rc=0 を固定した (残るのはクラスタ到達と reporter
  の実実行のみ)。偽物は引数 7 個 (get cm -n autopilot ops-health-report -o
  jsonpath={.data.latest.json}) を厳密に検査し、report.py が put_configmap に渡した
  ConfigMap 本文から data.latest.json を jsonpath 相当で取り出す。
- これで受入検証の **コマンド形状全体が CI 固定済み**になった。前セッションまでの
  「受入検証の残りはクラスタ到達のみ」という契約が、python 断片単体ではなく
  コマンド全体にまで閉じた。

### verify 実測

- `python3 -m unittest ops.tests.test_report_root_disk -v` → **5 tests OK** (前回 4 + 新規 1)
- `python3 -m unittest discover -s ops/tests -t .` → **605 OK** (前回 604 + 新規 1)
- ops/heart/tests 448 OK、ops/runner/tests 53 OK
- `python3 ops/tools/root_disk_usage.py --check` → rc=0 (**受入検証の 1 項目は green**)

### 分かったこと (実測・調査)

- **この sandbox は node01 上の pod そのもの** (mountinfo で overlay lowerdir が
  `/var/lib/rancher/k3s/agent/containerd/...`、`KUBERNETES_SERVICE_HOST=10.43.0.1`、
  statvfs が node01 の 251.6GiB/73.5GiB/167.9GiB に一致)。**apiserver 自体は
  到達できる** (`curl -sk https://10.43.0.1:443/version` → 401 Unauthorized) —
  kubectl が落ちるのはネットワーク不通ではなく**認証情報が無い (SA token も
  kubeconfig も無い)** だけ。前セッションの「localhost:8080 拒否」は kubectl の
  既定 kubeconfig 側の話で、両方 true。
- **ツールの実測経路を実環境 (node01 上の非特権 pod) で通した**:
  `python3 ops/tools/root_disk_usage.py --node node01 --json` → rc=0、
  `source=statvfs` (summary は SA token 無しで None → 意図どおり statvfs に倒れる)、
  `capacity_bytes=270202880000` / `used_bytes=78914818048` /
  `free_bytes=180234514432`、`fill_days=None` + note「履歴が 2 点に満たない」。
  設計どおりの fallback が実環境で動くことを確認した。
- 受入検証コマンドの `2>/dev/null` は偽物の stderr を握りつぶすため、テストは
  rc だけで判定する (shim の引数不一致は rc=2 で落ちる)。

### 発見（スコープ外、curriculum へ）

- なし (dashboard_smoke の no-lie-coexistence 論点は据え置き)。

### 次のセッションへ（レビューで差し戻されたら）

- **ローカルでやることは残っていない。** 受入検証の残り 1 項目 (kubectl) は
  wrapper 環境で reporter が 1 回走った後、認証付きの文脈 (クラスタ到達) で green に
  なる。sandbox では apiserver は 401 で到達できるが認証情報が無く、実行不能
  (今回の実測)。
- 差し戻されたら以下を疑う:
  1. `nodes/proxy` / `nodes/stats` の resourceNames が node01 のままか (回帰テスト
     TestRbac.test_kubelet_summary_proxy_resource_names_match_node が縛っている)
  2. ArgoCD が configMapGenerator を sync するまで reporter が旧 ConfigMap で走る
     自愈待ち (P-9037 と同じ。数回で治る)
  3. 受入検証コマンドの形 (jsonpath・namespace/name・パイプ) が spec からずれていないか
     (新設の test_acceptance_kubectl_command_verbatim が縛っている)
- **merge 後 (wrapper 環境) に確認すること**:
  1. reporter が 1 回走る → `kubectl get cm -n autopilot ops-health-report -o
     jsonpath='{.data.latest.json}'` に `root_disk.source` と `fill_days` キー (初回
     None) が載る → 受入検証 green
  2. `root_disk.source` が `kubelet_summary` になるか (RBAC nodes/proxy+stats の通し)。
     取れていれば breakdown の images/PVC が載り、取れなくても statvfs 総量 + None で
     正常動作 (実測済みの fallback)。実測したら substrate.md を更新する。
  3. 1 日分の履歴が溜まったら fill_days が数値になる (観測窓 MIN_WINDOW_DAYS=1.0)。
     「予報が出ていない」と指摘されたら「1 日分の履歴が必要」を説明する。