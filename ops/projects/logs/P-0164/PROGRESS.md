# P-0164 — PROGRESS

## 状態

- initializer 完了: PROJECT.md 初版を作成し commit 済み。
- **セッション 2 (2026-08-23) 完了**: 演習統括スクリプト + unittest を実装し、
  verify のうち unittest と dry-run の 2 項目を green 自力実測。
  残るは report.json (verify 第 1 項) = 実演習の実施のみ。
- **セッション 3 (2026-08-23) 完了**: 実演習の事前準備をすべて完了。
  ラベル 2 commit を積んだ `exercise/p-0164-labels` ブランチ push 済み
  (21011a7af / 5d24c8932)、ドラフト **PR #524** 作成済みで必須チェック `ci` も
  green。演習当日の Git 側作業は「PR を ready にして merge」の 2 API 呼び出しのみ。
  安全弁はセッション中も閉じたまま (blocking 5→6 件に増加) なので実施は見送り。

### 次のセッションへの一言

**実演習 (--run) をやって report.json を出すのが残りすべて。材料は全部揃っている**
(下記「実演習の手順」と PR #524)。弁が開いたら: `--run` を nohup 起動 →
`kubectl get` で 3 対象の ready=0 を自分の目で確認 → PR #524 を ready+merge
(curl + $AUTOPILOT_GITHUB_TOKEN、コマンドは手順節にある) → あとはスクリプトが
scale 1・計時・report.json 書き出しまで自動。**スクリプトは実行中ずっと黙る**
(出力は最後の 1 回)。進捗は kubectl で直接見ること。**dry-run を先に回して
弁の判定を見てから。** 閉じていたら何も始めないで終わってよい (準備済みのため
やり残しはない。P-0174 が新たに announced されており、開くのはまだ先の可能性が高い)。

## 実装メモ

### 作ったもの

- `ops/tools/deploy_continuity.py`: 演習統括。`--dry-run` (安全弁判定のみ・書き込み
  無し・rc=0 で完走) と `--run` (演習本体) 。集計 (`build_report`) と検算
  (`validate_report`) は純関数に切り出してありクラスタなしで試せる。
- `ops/tests/test_deploy_continuity.py`: 36 テスト。CI (`unittest discover -s ops/tests`)
 でも走るため git も kubectl も使わない (FakeRunner 注入)。

### 実測で訂正した initializer の前提 (重要)

- **argocd-application-controller は StatefulSet** (chart 9.1.6 実測。
  `kubectl get statefulset -n argocd` で確認)。初版 PROJECT.md の「3 つとも
  Deployment」は誤りで、PROJECT.md 側にも訂正を書き込んだ。scale は
  `kubectl scale statefulset/argocd-application-controller -n argocd --replicas=N`
- 安全弁の文字通りの読み (announced/active が「1 件も無い」) だと P-0164 自身が
  active である限り永遠に開かず完遂不可能。よって **自己 (P-0164) のみ除外**して
  判定する実装にした (凍結の害は「他プロジェクト」にある、という PROJECT.md の
  理由づけに基づく解釈)。判断を覆したい場合は `valve_verdict(projects, exclude=())`
  の呼び形に戻すだけでよい。dry-run の `--exclude-all` で文字通りの判定も見える

### スクリプトの設計 (次セッションはこれを読んで動く)

- **安全弁**: `origin/ops-state:projects.json` を refspec 明示 fetch で読む
  (shallow clone 単独 fetch の罠回避 — adoptgate.clone_fresh と同じ対策)。
  自己以外に announced/active が居たら `--run` は rc=2 で拒否し、**クラスタに
  一切触れない** (弁チェックは try/finally の外)。
- **--run の流れ**: baseline 確認 (全対象 replicas=1) → scale 0 一斉 → 全 pod 落ち
  確認 → main の前進監視 (`git ls-remote origin refs/heads/main` を poll。
  SHA が `--settle` 秒不動で確定。2 commit 未満なら中断) → `--dwell` 秒停止継続 →
  停止中の Application 一斉 snapshot 取得 (heart/health の見え方の証跡) → self-heal
  チェック (Git 設定は replicas=1 なので controller が自力で戻す可能性がある。
  戻っていたら手動 scale はしないで記録だけ) → scale 1 → 各アプリの
  status.sync.revision 到達 (refresh) と label 到達 (sync) を別々に計時 →
  report.json 書き出し。**どの経路で抜けても finally で scale 1 復帰を試みる**
- **計測の 2 段構え**: refresh_seconds = 「ArgoCD が Git の前進に気いた」まで /
  catchup_seconds = 「変更が live に適用され切った」まで (verify 必須キー)。
  label が乗らない (sync しない) 落ち方を検出できるように分けた
- **report.json の契約**: `catchup_seconds` キーは常に存在 (取りこぼし時は null +
  `missed_changes: true`)。validate_report が key 存在・非負・時刻順序・旗との整合を
  検査し、NG なら report を書かず中断する
- **Git 書き込みはスクリプトの外**: ruleset で main 直 push 不可のため PR 経由。
  gh CLI は無いので、次セッションは mcp github 系ツール or curl API で PR 作成/
  merge する。スクリプトは ls-remote で見るだけ
- kubectl write の許可はコードレベルでガード済み: `k_scale` は TARGETS (3 対象) 以外
  と replicas 0/1 以外を ValueError で拒む (TestScaleGuard が固定)

### 実演習の手順 (次セッション用チェックリスト。材料はセッション 3 で準備済み)

1. `python3 ops/tools/deploy_continuity.py --dry-run` → 弁 ok:true を確認
   (2026-08-23 時点の閉鎖中: P-0092 announced + P-0116/P-0157/P-0161/P-0163 active、
   さらに P-0174 announced が追加。他プロジェクトが delivered/stalled になるのを待つ)。
   閉じていたら終わってよい
2. **PR #524 の鮮度確認** (演習前日・直前に 1 回):
   `exercise/p-0164-labels` (21011a7af vaultwarden / 5d24c8932 coder の 2 commit) は
   ドラフト PR #524 で main を向いて待機中。main が進んで conflict していれば
   `PUT /repos/hikuohiku/homelab/pulls/524/update-branch` で解消し `ci` の再 green
   を待つ。ドラフトが何かに閉じられていれば同じ head/base で開き直す
3. バックグラウンドで `--run` を起動。**スクリプトは完了まで stdout に何も出さない**
   (最後に summary JSON の 1 回だけ)。ログはファイルへ:

   ```bash
   LOG=$(mktemp /tmp/p0164-run.XXXXXX)
   nohup python3 ops/tools/deploy_continuity.py --run --max-wait 900 --settle 30 --dwell 60 \
     > "$LOG" 2> "$LOG.err" & echo "pid=$! log=$LOG"
   ```

4. scale 0 の完了を**自分で kubectl 読み取りで確認してから** merge する
   (起動から 1〜3 分程度):

   ```bash
   kubectl get deployment/argocd-server deployment/argocd-repo-server \
     statefulset/argocd-application-controller -n argocd   # 3 行とも READY 0 を確認するまで数秒おきに再実行
   ```

   3 対象すべて READY=0 を確認したら、PR #524 を ready→merge (ruleset で main 直 push
   不可・レビュー 0 人・必須チェック `ci` は既に green 済みなので merge だけ通る):

   ```bash
   curl -s -X PATCH -H "Authorization: Bearer $AUTOPILOT_GITHUB_TOKEN" \
     -H "Content-Type: application/json" -d '{"draft": false}' \
     https://api.github.com/repos/hikuohiku/homelab/pulls/524 | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('state'),d.get('draft'))"
   curl -s -X PUT -H "Authorization: Bearer $AUTOPILOT_GITHUB_TOKEN" \
     -H "Content-Type: application/json" -d '{"merge_method":"merge"}' \
     https://api.github.com/repos/hikuohiku/homelab/pulls/524/merge
   ```

   - LABEL_KEY=`p0164.continuity` はスクリプトが live の Application CR 上で見に行く
     キー。値は何でもよい (key の存在だけを見る)
   - metadata.labels 変更はワークロード再起動を誘発しない (touches_apps=false 両立)
   - 早すぎる merge (ready=0 前) は「ArgoCD 生きている間の main 前進」になってしまうので
     必ず確認後に行う。逆に遅くても `--max-wait 900` 内なら問題ない
5. スクリプトが自動で dwell → scale 1 → 計時 → report.json 書き出しまで運ぶ。
   `$LOG` に summary JSON (catchup_seconds 等)、stderr 側に final restore 行
6. watcher/critic の観察は**停止窓口の間に**集める (DoD (3)。verify は見ない):
   `python3 ops/tools/version_watch.py` の出力や health reporter の見え方を時刻付きで
   PROGRESS.md の証跡に貼る。`--notes-file` は起動時に渡さないこと (**ファイルが存在
   しないと report 書き出し直前に例外で abort する** — rc=3, scale 復帰は finally で
   行われるが計測は失われる)。観察は実行後に
   `report.json` の `observations` へ python 一行で追記すればよい (validate 対象外の
   自由キー)

   ```bash
   python3 -c "import json;p='ops/projects/logs/P-0164/report.json';d=json.load(open(p));d['observations']['watcher']='(ここに観察)';json.dump(d,open(p,'w'),ensure_ascii=False,indent=2)"
   ```
7. 完了後: verify 3 項目を自分で回す (第 1 項が初めて green になる)。その後
   ラベル撤去 — `exercise/p-0164-labels` の差分を打ち消す branch + PR + merge を
   通常経路 (ArgoCD 生還後なので普通に同期される) で行い、live の Application から
   ラベルが消えたことを確認してから commit・セッション終了

## 証跡

(2026-08-23, project/p-0164 checkout, リポジトリルートで実行)

- verify 第 2 項 green 自力実測:

```
$ python3 -m unittest ops.tests.test_deploy_continuity
Ran 36 tests in 0.011s
OK
```

- verify 第 3 項 green 自力実測 (rc=0。弾かれたのは安全弁が現実に閉じているためで、
  dry-run の仕事は「判定を見せること」なのでこれは成功):

```
$ python3 ops/tools/deploy_continuity.py --dry-run; echo "rc=$?"
{
  "mode": "dry-run",
  "cluster_writes": false,
  ...
  "valve": { "ok": false,
             "blocking": ["P-0092","P-0116","P-0157","P-0161","P-0163"],
             "excluded_self": ["P-0164"], "checked": 60 },
  "targets_seen": [
    {"kind": "deployment", "name": "argocd-server", ...},
    {"kind": "deployment", "name": "argocd-repo-server", ...},
    {"kind": "statefulset", "name": "argocd-application-controller", ...}
  ],
  ...
}
rc=0
```

  targets_seen の 3 行目が StatefulSet を正しく認識していることの実測でもある。
- 全体テストスイート + validate も green:

```
$ python3 -m unittest discover -s ops/tests -t .   # Ran 278 tests ... OK
$ python3 ops/validate.py                          # 0 error (既存 warning 11 件のみ)
```

- branch 先頭の origin/main merge (archive.jsonl 先頭一致エラーの解消):
  `Merge origin/main (curriculum 採択) into project/p-0164 — 本プロジェクト領域への影響なし`

### セッション 3 (2026-08-23, 演習事前準備。project/p-0164 checkout, リポジトリルートで実行)

- **演習用ブランチ + ドラフト PR を準備済み** (実施は弁開放待ちのため見送り):
  - branch `exercise/p-0164-labels` = origin/main (c5d6df255) + 2 commit
    (21011a7af: vaultwarden に `p0164.continuity: "1"` / 5d24c8932: coder に
    `"2"`。どちらも metadata.labels への 2 行追加のみ、diff --stat 実測で
    2 files changed, 4 insertions)。push 済み (push_rc=0)
  - ドラフト PR **#524** (head: exercise/p-0164-labels / base: main) 作成済み。
    本文に「普段は merge しない・演習窓口で ready+merge される」旨を明記済み
- **main の ruleset 実測** (`GET /repos/hikuohiku/homelab/rules/branches/main`):
  deletion / non_fast_forward / pull_request (required_approving_review_count: 0) /
  required_status_checks (context: `ci`)。つまり **PR が通れば承認なしで merge 可能、
  ただし `ci` チェック必須** → ci.yml は `pull_request` トリガーなので、ドラフトを
  先に開いておけばチェックは演習前に green になる (実際 green を実測:

  ```
  $ curl .../commits/5d24c8932/check-runs → ci completed success
  ```

  演習窓口での Git 側作業は PATCH (ready 解除) + PUT (merge) の 2 API 呼び出しで済む)
- 認証: git push と API はどちらも `$AUTOPILOT_GITHUB_TOKEN`
  (git config の credential helper 実測。API GET repo = 200)。gh CLI は無いが不要
- 弁の再確認 (セッション後半):

```
$ python3 ops/tools/deploy_continuity.py --dry-run | python3 -c "...print valve..."
valve ok: False | blocking: ['P-0092','P-0116','P-0157','P-0161','P-0163','P-0174']
```

  セッション 2 時点より 1 件増えており (P-0174 が新たに announced)、閉じる方向に
  動いている。実施判断を誤らないため、この日は準備のみで閉じた
- unittest 36 件 + dry-run rc=0 はセッション 3 冒頭でも再実測ずみ (変更加えていない
  ため省略なしで同一結果)

### セッション 4 (2026-08-23, 当日確転バグの除去。project/p-0164 checkout, リポジトリルートで実行)

- 弁は閉じたまま (blocking 6 件: P-0092 / P-0116 / P-0157 / P-0161 / P-0163 / P-0174、
  セッション 3 から不変)。targets は 3 件とも 1/1、PR #524 は open / draft /
  mergeable_state=clean、head 5d24c8932 の `ci` は本日時点で success を再実測、main も
  不動 (c5d6df255)。実演習は今回も見送り
- **当日に確実に転ける欠陥を 2 件発見・修正** (本セッションの本体):

  1. `cmd_run` が main HEAD の移動を「**2 回**」観測することを要求していた
     (`len(new_shas) < 2` で raise)。しかし固定手順は PR #524 = **1 PR 2 commit の単発
     merge** であり、merge_method=merge での HEAD 移動は merge commit 分の **1 回**しか
     ない → 弁が開いた日に `--run` すると確実に rc=3 で abort し計測が失われた。
     PROJECT.md が許す「PR 2 本または 1 PR 2 commit」の片方しか実装していなかった。
     要求を「前進 ≥1 回」へ緩和し、観測した移動回数は report の `new_main_shas` の長さと
     してそのまま残る (PR 2 本形状も従来どおり通る。watch_main_advance 自体は無変更)
  2. scale 1 復帰後の ready 確認 `wait_until(all_ready, ...)` が引数無しで
     `all_ready(runner)` を呼んでいた → **復帰直後に TypeError で必ず abort**
     (report 書き出し直前。scale 復帰は finally で守られるが計測は消える)。
     `lambda: all_ready(runner)` へ修正

  共通原因は「--run happy path を通すテストが存在しなかった」こと。
  `ScriptedRunner` (同一コマンドに段階ごとの別応答を返せる runner) を新設し e2e 3 本を
  追加した: (a) 単発 merge 形状で完走・report 検算まで (b) PR 2 本形状 (移動 2 回) も
  受容 (c) main 不動なら rc=3・report 無し・finally が scale 1×3 を再発行することまで
  実測。これで (1)(2) は両方ともテストが落として発見できた型のバグだった

- 検証実測 (全て自力):

```
$ python3 -m unittest ops.tests.test_deploy_continuity   # Ran 39 tests (36→39) OK
$ python3 -m unittest discover -s ops/tests -t .         # Ran 281 tests OK
$ python3 ops/validate.py                                # 0 error (既存 warning 11 件のみ)
$ python3 ops/tools/deploy_continuity.py --dry-run       # rc=0
  valve blocking: 上記 6 件 / targets_seen 3 件とも replicas==ready==1
$ test -f ops/projects/logs/P-0164/report.json && ...    # rc=1 (弁開放待ち。唯一の failing)
```

- 当日の手順はセッション 3 固定のまま**変更なし** (--run を背景起動 → 全 zero 確認後に
  PATCH ready 解除 → PUT merge の 2 API 呼び出し。既定値のまま: settle 20s / dwell 60s /
  max-wait 900s / catchup-timeout 600s)。--notes-file は渡さない (既知の罠)
- **次のセッションへの一言**: コード側の残作業はもう無い。残りは「弁が開いている日の
  実演習 + report.json の observations 追記」「ラベル撤去 PR (exercise/p-0164-labels の
  打ち消し)」のみ。冒頭で dry-run の弁を見て、開いていれば即日実施してよい。開いて
  いなければ dry-run 確認だけして軽く閉じてよい

### セッション 5 (2026-08-23, 弁確認のみ。project/p-0164 checkout, リポジトリルートで実行)

- 冒頭で dry-run を実測 → **弁は閉じたまま** (blocking 6 件: P-0092 / P-0116 /
  P-0157 / P-0161 / P-0163 / P-0174、セッション 3・4 から不変。checked=61)。
  セッション 4 の指示「開いていなければ dry-run 確認だけして軽く閉じてよい」に従い
  実演習は見送り。コード変更は無し (セッション 4 の宣言どおり残作業なし)
- 前置条件の劣化がないことだけ再実測しておいた (次の「弁が開いた日」に調べ直さなくて済む):

```
$ python3 -m unittest ops.tests.test_deploy_continuity   # Ran 39 tests OK
$ python3 ops/tools/deploy_continuity.py --dry-run       # rc=0, targets_seen 3 件とも replicas==ready==1
$ git rev-parse origin/main                              # c5d6df255 (不動)
$ GET /pulls/524  → open / draft=true / mergeable_state=clean / head=5d24c8932
$ GET .../commits/5d24c8932/check-runs → ci success, GitGuardian success
```

- 当日の手順はセッション 3 固定のまま不変 (--run 背景起動 → 全 zero 確認 → PATCH ready
  解除 → PUT merge)。--notes-file は渡さない (既知の罠)
- **次のセッションへの一言**: 同じ。冒頭で dry-run の弁を見るのが最初で最後の分岐。
  開いていれば即日実施 (手順は PROGRESS セッション 3・4 の固定どおり)、開いていなければ
  上記の再実測だけして軽く閉じてよい。ラベル撤去 PR は演習完了後の作業であり先行準備は
  不要 (#524 が merge されるまで打ち消し先が main に存在しない)

### セッション 6 (2026-08-23, 弁確認のみ。project/p-0164 checkout, リポジトリルートで実行)

- 冒頭で dry-run を実測 → **弁は閉じたまま**だが blocking が **6→5 件に減少**
  (P-0092 / P-0116 / P-0157 / P-0161 / P-0174。**P-0163 が抜けた**。checked=61)。
  減少傾向は続いているが 0 ではないので、セッション 5 の指示どおり実演習は見送り。
  コード変更は無し
- 前置条件の再実測 (劣化なし):

```
$ python3 ops/tools/deploy_continuity.py --dry-run       # rc=0, valve ok=false (blocking 5), targets 3/3 ready
$ python3 -m unittest ops.tests.test_deploy_continuity   # Ran 39 tests OK
$ git rev-parse origin/main                              # c5d6df255 (不動)
$ GET /pulls/524  → open / draft=true / mergeable_state=clean / head=5d24c8932
$ GET .../commits/5d24c8932/check-runs → ci success, GitGuardian success
```

- 環境メモ: `gh` CLI はこの環境に無い (実測: command not found)。PR 確認は
  `curl -H "Authorization: Bearer $AUTOPILOT_GITHUB_TOKEN" https://api.github.com/repos/$GITHUB_REPO/...`
  でよい ($GITHUB_REPO も env に在り)
- 当日の手順はセッション 3 固定のまま不変 (--run 背景起動 → 全 zero 確認 → PATCH ready
  解除 → PUT merge)。--notes-file は渡さない (既知の罠)
- **次のセッションへの一言**: 同じ。冒頭で dry-run の弁を見るのが最初で最後の分岐。
  開いていれば即日実施 (手順は PROGRESS セッション 3・4 の固定どおり)、開いていなければ
  上記の再実測だけして軽く閉じてよい。blocking が減っている (6→5) ので、開く日は近いかも

### セッション 7 (2026-08-23, 弁確認のみ。project/p-0164 checkout, リポジトリルートで実行)

- 冒頭で dry-run を実測 → **弁は閉じたまま**だが blocking が **5→4 件に減少**
  (P-0092 / P-0116 / P-0157 / P-0161。**P-0174 が抜けた**。checked=61)。減少傾向継続
  (6→5→4) のも 0 ではないので、固定指示どおり実演習は見送り。コード変更は無し
- 前置条件の再実測 (劣化なし):

```
$ python3 ops/tools/deploy_continuity.py --dry-run       # rc=0, valve ok=false (blocking 4), targets 3/3 ready
$ python3 -m unittest ops.tests.test_deploy_continuity   # Ran 39 tests OK
$ python3 -m unittest discover -s ops/tests -t .         # Ran 281 tests OK
$ python3 ops/validate.py                                # 0 error, 11 warning (既存)
$ git rev-parse origin/main                              # c5d6df255 (不動)
$ GET /pulls/524  → open / draft=true / mergeable_state=clean / head=5d24c8932
$ GET .../commits/5d24c8932/check-runs → ci success, GitGuardian success
```

- **弁が開かない構造的理由を実測** (読み取り専用で `git show origin/ops-state:projects.json`):
  blocking 4 件の内訳は announced 1 (P-0092 immich-postgres 更新) + active 3
  (P-0116 restic 週次健康診断 / P-0157 restic 静停止監視 / P-0161 分離 Job プロファイル)。
  announced は採択フローが進めば抜けるが、**active 3 は各プロジェクトが完了しない限り
  弁は開かない**。つまり「開く日を待つ」戦略は active の消化ペースに律速される —
  数日単位で開くとは期待せず、毎セッション確認を続けるのが正しい待ち方
- 当日の手順はセッション 3 固定のまま不変 (--run 背景起動 → 全 zero 確認 → PATCH ready
  解除 → PUT merge)。--notes-file は渡さない (既知の罠)
- **次のセッションへの一言**: 同じ。冒頭で dry-run の弁を見るのが最初で最後の分岐。
  開いていれば即日実施 (手順は PROGRESS セッション 3・4 の固定どおり)、開いていなければ
  再実測だけして軽く閉じてよい。blocking 内訳は announced 1 + active 3 なので、
  弁が開くのは P-0092 が消えたあとも active 3 の完了待ちになる可能性が高い

### セッション 8 (2026-08-23, 弁確認のみ。project/p-0164 checkout, リポジトリルートで実行)

- 冒頭で dry-run を実測 → **弁は閉じたまま、blocking 4 件でセッション 7 から不変**
  (P-0092 / P-0116 / P-0157 / P-0161。checked=61)。減少傾向 (6→5→4) が止まった。
  固定指示どおり実演習は見送り。コード変更は無し
- 前置条件の再実測 (劣化なし):

```
$ python3 ops/tools/deploy_continuity.py --dry-run       # rc=0, valve ok=false (blocking 4), targets 3/3 ready
$ python3 -m unittest ops.tests.test_deploy_continuity   # Ran 39 tests OK
$ python3 -m unittest discover -s ops/tests -t .         # Ran 281 tests OK
$ python3 ops/validate.py                                # 0 error, 11 warning (既存)
$ git rev-parse origin/main                              # c5d6df255 (不動)
$ GET /pulls/524  → open / draft=true / mergeable_state=clean / head=5d24c8932
$ GET .../commits/5d24c8932/check-runs → ci success, GitGuardian success
```

- セッション 7 の発見の補足: projects.json を生読みしたら state 内訳が
  delivered 26 / vetoed 2 / stalled 26 / **in_review 2** / announced 1 / active 4。
  前セッションまで「帳簿上観測されるのは 5 状態」だったが、**in_review が初めて
  台帳に現れた** (active 4 = blocking 3 + 自己 P-0164。excluded_self と辻褄一致)。
  in_review は弁の判定対象外なので演習条件への影響は無し
- 当日の手順はセッション 3 固定のまま不変 (--run 背景起動 → 全 zero 確認 → PATCH ready
  解除 → PUT merge)。--notes-file は渡さない (既知の罠)
- **次のセッションへの一言**: 同じ。冒頭で dry-run の弁を見るのが最初で最後の分岐。
  開いていれば即日実施 (手順は PROGRESS セッション 3・4 の固定どおり)、開いていなければ
  再実測だけして軽く閉じてよい。blocking は announced 1 + active 3 のまま減速中 —
  active の完了待ちが律速という構造は変わっていない

### セッション 9 (2026-08-23, 弁確認のみ。project/p-0164 checkout, リポジトリルートで実行)

- 冒頭で dry-run を実測 → **弁は閉じたまま、blocking が 4→5 件に増加**
  (P-0092 / P-0116 / P-0157 / P-0161 / **P-0174 が戻った**。checked=61)。減少傾向
  (6→5→4) が反転した。固定指示どおり実演習は見送り。コード変更は無し
- 前置条件の再実測 (劣化なし):

```
$ python3 ops/tools/deploy_continuity.py --dry-run       # rc=0, valve ok=false (blocking 5), targets 3/3 ready
$ python3 -m unittest ops.tests.test_deploy_continuity   # Ran 39 tests OK
$ python3 ops/validate.py                                # 0 error, 11 warning (既存)
$ git fetch origin && git rev-parse origin/main          # c5d6df255 (不動)
$ GET /pulls/524  → open / draft=true / mergeable_state=clean / head=5d24c8932
$ GET .../commits/5d24c8932/check-runs → ci success, GitGuardian success
```

- 増加の原因を実測 (読み取り専用で `git show origin/ops-state:projects.json`):
  セッション 8 に初観測だった `in_review` が **2→1 件に減り、抜けた 1 件が
  P-0174 として active に遷移していた** (state 内訳: delivered 26 / vetoed 2 /
  stalled 26 / announced 1 / active 5 / in_review 1)。つまり in_review から
  active への遷移が実際に起こった — in_review は一時停止であり完了ではない。
  演習条件への影響は「active が 3→4 件に増えた」ことのみ (自己除外後の blocking 実質 4)
- 当日の手順はセッション 3 固定のまま不変 (--run 背景起動 → 全 zero 確認 → PATCH ready
  解除 → PUT merge)。--notes-file は渡さない (既知の罠)
- **次のセッションへの一言**: 同じ。冒頭で dry-run の弁を見るのが最初で最後の分岐。
  開いていれば即日実施 (手順は PROGRESS セッション 3・4 の固定どおり)、開いていなければ
  再実測だけして軽く閉じてよい。blocking は announced 1 (P-0092) + active 3 (P-0116 /
  P-0157 / P-0161) が本体で、in_review からの再 active (P-0174) がノイズとして乗る —
  「減っていれば開く日が近い」という読みは当てにならず、0 か否かだけを見ること

### セッション 10 (2026-08-23, 弁確認のみ。project/p-0164 checkout, リポジトリルートで実行)

- 冒頭で dry-run を実測 → **弁は閉じたまま、blocking が 5→6 件に増加**
  (P-0092 / P-0116 / P-0157 / P-0161 / **P-0163 が戻った** / P-0174。checked=61)。
  減少局面は一度も訪れていない (7→6→5→6→5→6)。固定指示どおり実演習は見送り。
  コード変更は無し
- 前置条件の再実測 (劣化なし):

```
$ python3 ops/tools/deploy_continuity.py --dry-run       # rc=0, valve ok=false (blocking 6), targets 3/3 ready
$ python3 -m unittest ops.tests.test_deploy_continuity   # Ran 39 tests OK
$ python3 -m unittest discover -s ops/tests -t .         # Ran 281 tests OK
$ python3 ops/validate.py                                # 0 error, 11 warning (既存)
$ git fetch origin && git rev-parse origin/main          # c5d6df255 (不動)
$ GET /pulls/524  → open / draft=true / mergeable_state=clean / head=5d24c8932
$ GET .../commits/5d24c8932/check-runs → ci success, GitGuardian success
```

- 増加の原因を実測 (読み取り専用 `git show origin/ops-state:projects.json`):
  セッション 9 に台帳へ残っていた `in_review` 最後の 1 件 (P-0163) が active へ
  遷移していた (state 内訳: delivered 26 / vetoed 2 / stalled 26 / announced 1 /
  **active 6** / in_review 0)。P-0163 は adopt_gate_attempts=1・verify 未通過
  (ops/tools/syncthing_acceptance.py 系) のまま active — in_review→active の復帰が
  **2 例連続**となり、in_review は「完了目前」ではなく「差し戻し/保留」の表現である
  可能性が高い。in_review 台帳値は本日時点で 0
- 当日の手順はセッション 3 固定のまま不変 (--run 背景起動 → 全 zero 確認 → PATCH ready
  解除 → PUT merge)。--notes-file は渡さない (既知の罠)
- **次のセッションへの一言**: 同じ。冒頭で dry-run の弁を見るのが最初で最後の分岐。
  開いていれば即日実施 (手順は PROGRESS セッション 3・4 の固定どおり)、開いていなければ
  再実測だけして軽く閉じてよい。in_review は 0 になったので、以降の blocking 増減は
  announced/active の出入りのみで決まる。本体は announced 1 (P-0092) + active 5 —
  「減っていれば近い」は当てにならないままで、0 か否かだけを見ること

### セッション 11 (2026-08-23, 弁確認のみ。project/p-0164 checkout, リポジトリルートで実行)

- 冒頭で dry-run を実測 → **弁は閉じたまま、blocking 6 件でセッション 10 から不変**
  (P-0092 / P-0116 / P-0157 / P-0161 / P-0163 / P-0174。checked=61)。固定指示どおり
  実演習は見送り。コード変更は無し
- **本セッションの実イベント: main が初めて動いた** (c5d6df255 → 93d1c431b。
  PR #525 openclaw 撤去→telegram-adapter 置換えの 3 commit)。演習材料の PR #524 が
  これを**無介入で乗り越えた**: 触るファイルが素
  (vaultwarden/coder の application.yaml vs openclaw/telegram-adapter 配下) のため
  conflict せず、head は 5d24c8932 のまま、update-branch 不要と実測
  (手順の「conflict していれば update-branch」は発動せず)
- 前置条件の再実測 (main 前進後も劣化なし):

```
$ python3 ops/tools/deploy_continuity.py --dry-run       # rc=0, valve ok=false (blocking 6), targets 3/3 ready
$ python3 -m unittest ops.tests.test_deploy_continuity   # Ran 39 tests OK
$ python3 -m unittest discover -s ops/tests -t .         # Ran 281 tests OK
$ python3 ops/validate.py                                # 0 error, 11 warning (既存)
$ git fetch origin && git rev-parse origin/main          # 93d1c431b (c5d6df255 から前進)
$ GET /pulls/524  → open / draft=true / mergeable_state=clean / head=5d24c8932
$ GET .../commits/5d24c8932/check-runs → ci success, GitGuardian success
```

- ops-state の内訳もセッション 10 から不変 (読み取り専用 `git show
  origin/ops-state:projects.json`: delivered 26 / vetoed 2 / stalled 26 /
  announced 1 / active 6 / in_review 0)。スクリプトは自前で refspec 明示 fetch をする
  (deploy_continuity.py:274) ので、冒頭 dry-run の弁判定は常に最新台帳に対する値
- 当日の手順はセッション 3 固定のまま不変 (--run 背景起動 → 全 zero 確認 → PATCH ready
  解除 → PUT merge)。--notes-file は渡さない (既知の罠)
- **次のセッションへの一言**: 同じ。冒頭で dry-run の弁を見るのが最初で最後の分岐。
  開いていれば即日実施 (手順は PROGRESS セッション 3・4 の固定どおり)、開いていなければ
  再実測だけして軽く閉じてよい。追加の注意: main はもう動く前提で置くこと。
  演習直前に PR #524 を見て mergeable_state が **unknown でも慌てない** — base 移動直後は
  GitHub が計算中で数秒〜十数秒 unknown になる (本日実測: 8 秒待ちで clean に落ち着いた)。
  待って再取得し、dirty が確定したときだけ update-branch → ci 再 green を待つ

### セッション 12 (2026-08-23, 弁確認のみ。project/p-0164 checkout, リポジトリルートで実行)

- 冒頭で dry-run を実測 → **弁は閉じたまま、blocking 5 件に減少**
  (P-0092 / P-0116 / P-0157 / P-0161 / P-0174。checked=61)。P-0163 が active を抜けた。
  固定指示どおり実演習は見送り。コード変更は無し
- **減少の理由を実測**: P-0163 が **active → in_review へ遷移していた** (読み取り専用
  `git show origin/ops-state:projects.json`。内訳: delivered 26 / vetoed 2 / stalled 26 /
  announced 1 / **active 5** / in_review 1)。in_review→active の復帰を 2 例見た後の
  **逆方向 (active→in_review) 遷移の初観測**で、in_review が双方向の中間状態
  (=差し戻し/保留表現) である裏付け。in_review は弁判定対象外なので、blocking 減は
  演習開始条件の前進ではない (0 か否かだけが分岐)
- 本セッションの実イベント: main がさらに前進 (93d1c431b → cc1c86626。
  PR #528 telegram-adapter イメージの digest pin)。演習材料の PR #524 は
  **2 度目の base 前進も無介入で乗り越え**、head=5d24c8932 のまま clean 維持
  (mergeable_state は unknown を経由せず初手から clean だった)
- 前置条件の再実測 (劣化なし):

```
$ python3 ops/tools/deploy_continuity.py --dry-run       # rc=0, valve ok=false (blocking 5), targets 3/3 ready
$ python3 -m unittest ops.tests.test_deploy_continuity   # Ran 39 tests OK
$ python3 -m unittest discover -s ops/tests -t .         # Ran 281 tests OK
$ python3 ops/validate.py                                # 0 error, 11 warning (既存)
$ git fetch origin && git rev-parse origin/main          # cc1c86626 (93d1c431b から前進)
$ GET /pulls/524  → open / draft=true / mergeable_state=clean / head=5d24c8932
$ GET .../commits/5d24c8932/check-runs → ci success, GitGuardian success
```

- 当日の手順はセッション 3 固定のまま不変 (--run 背景起動 → 全 zero 確認 → PATCH ready
  解除 → PUT merge)。--notes-file は渡さない (既知の罠)
- **次のセッションへの一言**: 同じ。冒頭で dry-run の弁を見るのが最初で最後の分岐。
  開いていれば即日実施 (手順は PROGRESS セッション 3・4 の固定どおり)、開いていなければ
  再実測だけして軽く閉じてよい。main は 2 度動いたが PR #524 はどちらも無介入で clean を
  維持 — 「main が動く前提」は実証済みなので過剰な事前点検は不要。
  unknown 待ちの注意だけ残す: mergeable_state が unknown なら数秒〜十数秒待って再取得、
  dirty 確定時のみ update-branch → ci 再 green を待つ

### セッション 13 (2026-08-23, 弁確認のみ。project/p-0164 checkout, リポジトリルートで実行)

- 冒頭で dry-run を実測 → **弁は閉じたまま**、blocking 5 件・checked=61
  (wrapper 実測 08:00 と同一内訳)。固定指示どおり実演習は見送り。コード変更は無し
- 本セッションの実イベント: **台帳の総計が完全に不変のまま blocking メンバーだけ
  入れ替わった**。読み取り専用 `git show origin/ops-state:projects.json` の内訳は
  delivered 26 / vetoed 2 / stalled 26 / announced 1 / active 5 / in_review 1 で
  セッション 12 と同一数字。しかし個別状態を実測すると **P-0163 が in_review → active
  へ復帰** (active→in_review→active の往復が完観測) し、**代わりに P-0174 が active →
  in_review へ抜けた** (active→in_review 方向の 2 例目)。現在の blocking 内訳:
  P-0092(announced) / P-0116 / P-0157 / P-0161 / P-0163
- main は不動 (cc1c86626)。PR #524 は初手から mergeable_state=clean
  (head=5d24c8932 不変、unknown 待ち無し)、ci + GitGuardian success
- 前置条件の再実測 (劣化なし):

```
$ python3 ops/tools/deploy_continuity.py --dry-run       # rc=0, valve ok=false (blocking 5), targets 3/3 ready
$ python3 -m unittest ops.tests.test_deploy_continuity   # Ran 39 tests OK
$ python3 -m unittest discover -s ops/tests -t .         # Ran 281 tests OK
$ python3 ops/validate.py                                # 0 error, 11 warning (既存)
$ git fetch origin && git rev-parse origin/main          # cc1c86626 (不動)
$ GET /pulls/524  → open / draft=true / mergeable_state=clean / head=5d24c8932
$ GET .../commits/5d24c8932/check-runs → ci success, GitGuardian success
```

- 当日の手順はセッション 3 固定のまま不変 (--run 背景起動 → 全 zero 確認 → PATCH ready
  解除 → PUT merge)。--notes-file は渡さない (既知の罠)
- **次のセッションへの一言**: 同じ。冒頭で dry-run の弁を見るのが最初で最後の分岐。
  開いていれば即日実施 (手順は PROGRESS セッション 3・4 の固定どおり)、開いていなければ
  再実測だけして軽く閉じてよい。main 不動・PR #524 無介入 clean が続き、事前点検は
  ますます不要。unknown 待ちの注意だけ残す: mergeable_state が unknown なら数秒〜十数秒
  待って再取得、dirty 確定時のみ update-branch → ci 再 green を待つ。
  あと blocking 数が前回と同じでも中身は入れ替わるので、「減った/増えた」より「0 か否か」
  だけを見ること (この原則は今回でさらに強化)

### セッション 14 (2026-08-23, 弁確認のみ。project/p-0164 checkout, リポジトリルートで実行)

- 冒頭で dry-run を実測 → **弁は閉じたまま**、blocking 4 件に減少
  (P-0092 / P-0116 / P-0161 / P-0163。checked=61。wrapper 実測 08:07 と同一内訳)。
  固定指示どおり実演習は見送り。コード変更は無し
- 本セッションの実イベント: **台帳の総計が久々に動いた**。読み取り専用
  `git show origin/ops-state:projects.json` の内訳は delivered 26 / vetoed 2 /
  **stalled 28** / announced 1 / **active 4** / **in_review 0**
  (セッション 13 は stalled 26 / active 5 / in_review 1)。個別照合の結果:
  **P-0157 が active → stalled へ抜け、P-0174 が in_review → stalled へ抜けた**。
  減少 2 件の行き先が揃って stalled — つまり blocking の減少は一切の前進を伴わない
  「停滞への流出」だった。in_review が空になるのは初観測以来初 (現時点で 0 件)
- main は不動 (cc1c86626)。PR #524 は mergeable_state=clean 継続
  (head=5d24c8932 不変)、ci + GitGuardian success。
  小ネタ: API 呼び出しの token は `AUTOPILOT_GITHUB_TOKEN` を使う
  (`GITHUB_TOKEN` / `GH_TOKEN` は env に存在しない — 今回 empty token で
  Bad credentials を踏んだ)
- 前置条件の再実測 (劣化なし):

```
$ python3 ops/tools/deploy_continuity.py --dry-run       # rc=0, valve ok=false (blocking 4), targets 3/3 ready
$ python3 -m unittest ops.tests.test_deploy_continuity   # Ran 39 tests OK
$ python3 -m unittest discover -s ops/tests -t .         # Ran 281 tests OK
$ python3 ops/validate.py                                # 0 error, 11 warning (既存)
$ git fetch origin && git rev-parse origin/main          # cc1c86626 (不動)
$ GET /pulls/524  → open / draft=true / mergeable_state=clean / head=5d24c8932
$ GET .../commits/5d24c8932/check-runs → ci success, GitGuardian success
```

- 当日の手順はセッション 3 固定のまま不変 (--run 背景起動 → 全 zero 確認 → PATCH ready
  解除 → PUT merge)。--notes-file は渡さない (既知の罠)
- **次のセッションへの一言**: 同じ。冒頭で dry-run の弁を見るのが最初で最後の分岐。
  開いていれば即日実施 (手順は PROGRESS セッション 3・4 の固定どおり)、開いていなければ
  再実測だけして軽く閉じてよい。「0 か否か」だけを見る原則は維持。今回 blocking が
  減ったのは前進でなく stalled 流出だったので、「減少傾向」への期待はさらに下げてよい。
  unknown 待ちの注意だけ残す: mergeable_state が unknown なら数秒〜十数秒待って再取得、
  dirty 確定時のみ update-branch → ci 再 green を待つ

### セッション 15 (2026-08-23, 弁確認のみ。project/p-0164 checkout, リポジトリルートで実行)

- 冒頭で dry-run を実測 → **弁は閉じたまま**、blocking 4 件で不変
  (P-0092 / P-0116 / P-0161 / P-0163。checked=61。wrapper 実測 08:12 と同一内訳)。
  固定指示どおり実演習は見送り。コード変更は無し
- 本セッションの実イベント: **台帳が初めて「何も変わらない」を実測された**。
  `git show origin/ops-state:projects.json` の内訳は delivered 26 / vetoed 2 /
  stalled 28 / announced 1 / active 4 / in_review 0 とセッション 14 と完全同一で、
  個別照合でも announced=P-0092 / active=P-0116+P-0161+P-0163 (+自己 P-0164) と
  メンバーシップごと一致 (churn ゼロ)。なお ops-state ブランチ自体は動いていた
  (fetch で 2bf8d805b→af68a7017) が、差分は heartbeat.json / metrics.jsonl のみで
  projects.json は無変化 — 「台帳ブランチの進行」と「状態遷移の有無」は独立であり、
  ブランチが動いたからと言って照合を省略してはいけない
- main は不動 (cc1c86626)。PR #524 は mergeable_state=clean 初手継続
  (head=5d24c8932 不変、unknown 待ち無し)、ci + GitGuardian success
- 前置条件の再実測 (劣化なし):

```
$ python3 ops/tools/deploy_continuity.py --dry-run       # rc=0, valve ok=false (blocking 4), targets 3/3 ready
$ python3 -m unittest ops.tests.test_deploy_continuity   # Ran 39 tests OK
$ python3 -m unittest discover -s ops/tests -t .         # Ran 281 tests OK
$ python3 ops/validate.py                                # 0 error, 11 warning (既存)
$ git fetch origin && git rev-parse origin/main          # cc1c86626 (不動)
$ GET /pulls/524  → open / draft=true / mergeable_state=clean / head=5d24c8932
$ GET .../commits/5d24c8932/check-runs → ci success, GitGuardian success
```

- 当日の手順はセッション 3 固定のまま不変 (--run 背景起動 → 全 zero 確認 → PATCH ready
  解除 → PUT merge)。--notes-file は渡さない (既知の罠)
- **次のセッションへの一言**: 同じ。冒頭で dry-run の弁を見るのが最初で最後の分岐。
  開いていれば即日実施 (手順は PROGRESS セッション 3・4 の固定どおり)、開いていなければ
  再実測だけして軽く閉じてよい。「0 か否か」だけを見る原則は維持。
  unknown 待ちの注意だけ残す: mergeable_state が unknown なら数秒〜十数秒待って再取得、
  dirty 確定時のみ update-branch → ci 再 green を待つ

### セッション 16 (2026-08-23, 弁確認のみ。project/p-0164 checkout, リポジトリルートで実行)

- 冒頭で dry-run を実測 → **弁は閉じたまま**、blocking 4 件で不変
  (P-0092 / P-0116 / P-0161 / P-0163。checked=61。セッション 15・wrapper 実測と同一内訳)。
  固定指示どおり実演習は見送り。コード変更は無し
- 本セッションの実イベント: **台帳が 2 セッション連続で完全凍結** (15→16)。
  `git show origin/ops-state:projects.json` の内訳は delivered 26 / vetoed 2 /
  stalled 28 / announced 1 / active 4 / in_review 0 とセッション 15 と完全同一、
  メンバーシップ照合でも announced=P-0092 / active=P-0116+P-0161+P-0163 (+自己) と一致。
  完全凍結の連続観測により「凍結は珍しい一回性イベント」でなく平時の定常状態たりうることが
  分かった。なお ops-state ブランチは進行していた (fetch で af68a7017→e76d32ab6)
- 小ネタ (弁に無関係): **project/p-0163 ブランチが動いた** (fetch で c225da0ae→15a1ff6bd)
  が、台帳上 P-0163 は active のまま不変。blocking プロジェクト自身のブランチ進行 =
  作業は進んでいる = 状態遷移が近い、という読みも台帳実測なしには成立しない (「ブランチ
  進行 ≠ 台帳遷移」の第 3 例。ops-state 全体 / 自プロジェクトに続き blocking 他プロジェクト)
- main は不動 (cc1c86626)。PR #524 は mergeable_state=clean 初手継続
  (head=5d24c8932 不変、unknown 待ち無し)、ci + GitGuardian success
- 前置条件の再実測 (劣化なし):

```
$ python3 ops/tools/deploy_continuity.py --dry-run       # rc=0, valve ok=false (blocking 4), targets 3/3 ready
$ python3 -m unittest ops.tests.test_deploy_continuity   # Ran 39 tests OK
$ python3 -m unittest discover -s ops/tests -t .         # Ran 281 tests OK
$ python3 ops/validate.py                                # 0 error, 11 warning (既存)
$ git fetch origin && git rev-parse origin/main          # cc1c86626 (不動)
$ GET /pulls/524  → open / draft=true / mergeable_state=clean / head=5d24c8932
$ GET .../commits/5d24c8932/check-runs → ci success, GitGuardian success
```

- 当日の手順はセッション 3 固定のまま不変 (--run 背景起動 → 全 zero 確認 → PATCH ready
  解除 → PUT merge)。--notes-file は渡さない (既知の罠)
- **次のセッションへの一言**: 同じ。冒頭で dry-run の弁を見るのが最初で最後の分岐。
  開いていれば即日実施 (手順は PROGRESS セッション 3・4 の固定どおり)、開いていなければ
  再実測だけして軽く閉じてよい。「0 か否か」だけを見る原則は維持。台帳は 2 連続凍結中だが
  凍結の継続を「開く日が遠い」と読む根拠は無い (14→15→16 の遷移ゼロは停滞滞留の別表現)。
  unknown 待ちの注意だけ残す: mergeable_state が unknown なら数秒〜十数秒待って再取得、
  dirty 確定時のみ update-branch → ci 再 green を待つ

### セッション 17 (2026-08-23, 弁確認のみ。project/p-0164 checkout, リポジトリルートで実行)

- 冒頭で dry-run を実測 → **弁は閉じたまま**、blocking 4→3 件に減少
  (P-0092 / P-0116 / P-0161。checked=61)。固定指示どおり実演習は見送り。コード変更は無し
- 本セッションの実イベント: **完全凍結 2 連続 (15→16) の後、台帳が動いた。変化はたった 1 件**。
  `git show` 差分の実測で P-0163 が active→in_review に遷移したのみ (60/61 件が不変)。
  内訳は delivered 26 / vetoed 2 / stalled 28 / announced 1 / active 3 (+自己) / in_review 1。
  凍結明けも大規模入れ替わりでなく単一メンバーの最小遷移だった。ops-state ブランチは
  進行 (fetch で e76d32ab6→3a7e91543)
- 小ネタ (弁に無関係): **P-0163 が in_review に再入場** (セッション 12 に入り、13 に
  active へ復帰した 2 周目)。台帳上で往復振動する唯一のプロジェクトであり、in_review =
  双方向中間状態説の確度をさらに上げた。なお blocking 減少 (4→3) の出口は delivered でも
  stalled でもなく横滑りの in_review — 「減少 = 弁への前進」への反例がまた 1 件増えただけで、
  判定原則 (announced+active==0 の実測一択) は不変
- main は不動 (cc1c86626)。PR #524 は mergeable_state=clean 初手継続
  (head=5d24c8932 不変、unknown 待ち無し)、ci + GitGuardian success
- 前置条件の再実測 (劣化なし):

```
$ python3 ops/tools/deploy_continuity.py --dry-run       # rc=0, valve ok=false (blocking 3), targets 3/3 ready
$ python3 -m unittest ops.tests.test_deploy_continuity   # Ran 39 tests OK
$ python3 -m unittest discover -s ops/tests -t .         # Ran 281 tests OK
$ python3 ops/validate.py                                # 0 error, 11 warning (既存)
$ git fetch origin && git rev-parse origin/main          # cc1c86626 (不動)
$ GET /pulls/524  → open / draft=true / mergeable_state=clean / head=5d24c8932
$ GET .../commits/5d24c8932/check-runs → ci success, GitGuardian success
```

- 当日の手順はセッション 3 固定のまま不変 (--run 背景起動 → 全 zero 確認 → PATCH ready
  解除 → PUT merge)。--notes-file は渡さない (既知の罠)
- **次のセッションへの一言**: 同じ。冒頭で dry-run の弁を見るのが最初で最後の分岐。
  開いていれば即日実施 (手順は PROGRESS セッション 3・4 の固定どおり)、開いていなければ
  再実測だけして軽く閉じてよい。「0 か否か」だけを見る原則は維持。台帳は凍結明けに
  P-0163 のみ動いたが、これを「弁開放へ向かう潮流」と読む根拠は無い (P-0163 は過去にも
  往復して戻ってきている)。unknown 待ちの注意だけ残す: mergeable_state が unknown なら
  数秒〜十数秒待って再取得、dirty 確定時のみ update-branch → ci 再 green を待つ

### セッション 18 (2026-08-23, 弁確認のみ。project/p-0164 checkout, リポジトリルートで実行)

- 冒頭で dry-run を実測 → **弁は閉じたまま**、blocking 3→2 件に減少
  (P-0092 / P-0161。checked=61)。固定指示どおり実演習は見送り。コード変更は無し
- 本セッションの実イベント: **未観測の中間状態 `merging` が台帳に初出**。
  `git show` 差分の実測では 61 件のうち 2 件が動いた (59/61 不変):
  P-0163 が in_review→**merging** (in_review の第 3 の出口方向)、
  P-0116 が active→stalled。内訳は delivered 26 / vetoed 2 / stalled 29 /
  announced 1 / active 2 (+自己) / merging 1 / in_review 0。
  P-0163 の merging 化は同プロジェクトの PR #527 が main にマージされたことと同時であり、
  「PR 済み・完了前」の状態表現という解釈と整合する (セッション 8 メモ「コード上散見する
  review/merging 等の中間状態が帳簿上未観測」に対し、in_review に続き merging も実在確定)
- blocking 減少 (3→2) の出口は P-0116 の stalled 流出で、delivered は 26 のまま不変 —
  「流出による減少」はセッション 14 に続き 2 例目。判定原則 (announced+active==0 の実測一択)
  は不変。残る塞ぎ手は P-0092 announced + P-0161 active
- **main が史上 2 度目の前進** (cc1c86626 → a962e4211、PR #527 project/p-0163 マージ)。
  PR #524 は無介入で mergeable_state=clean を維持 — main 移動直後の unknown は既知の罠どおり
  出現し (10 秒待ちで clean 復帰)、head=5d24c8932 不変・ci/GitGuardian success。
  演習材料の耐久性は 3 例目の裏取り
- ops-state ブランチは進行 (fetch で 3a7e91543→669768126、先頭は heart: beat 43)
- 前置条件の再実測 (劣化なし):

```
$ python3 ops/tools/deploy_continuity.py --dry-run       # rc=0, valve ok=false (blocking 2), targets 3/3 ready
$ python3 -m unittest ops.tests.test_deploy_continuity   # Ran 39 tests OK
$ python3 -m unittest discover -s ops/tests -t .         # Ran 281 tests OK
$ python3 ops/validate.py                                # 0 error, 11 warning (既存)
$ git fetch origin && git rev-parse origin/main          # a962e4211 (前進)
$ GET /pulls/524  → open / draft=true / mergeable_state=clean / head=5d24c8932
$ GET .../commits/5d24c8932/check-runs → ci success, GitGuardian success
```

- 当日の手順はセッション 3 固定のまま不変 (--run 背景起動 → 全 zero 確認 → PATCH ready
  解除 → PUT merge)。--notes-file は渡さない (既知の罠)
- **次のセッションへの一言**: 同じ。冒頭で dry-run の弁を見るのが最初で最後の分岐。
  開いていれば即日実施 (手順は PROGRESS セッション 3・4 の固定どおり)、開いていなければ
  再実測だけして軽く閉じてよい。「0 か否か」だけを見る原則は維持。blocking は 2 件まで減ったが
  P-0092 は初日から announced 滞在が続く古参であり、「残り少数 = 開くのが近い」という読みには
  依然根拠がない。unknown 待ちの注意は継続: mergeable_state が unknown なら数秒〜十数秒待って
  再取得、dirty 確定時のみ update-branch → ci 再 green を待つ

### セッション 19 (2026-08-23, 弁確認のみ。project/p-0164 checkout, リポジトリルートで実行)

- 冒頭で dry-run を実測 → **弁は閉じたまま**、blocking 2 件で不変
  (P-0092 / P-0161。checked=61)。固定指示どおり実演習は見送り。コード変更は無し
- 本セッションの実イベント: **前セッション初出の `merging` の出口を初観測**
  (P-0163: merging→delivered)。台帳差分の実測では 61 件のうちこの 1 件のみが動いた
  (60/61 不変。全員分の state 一括 diff で churn 無しを確認 — カウント比較だけでなく
  メンバーシップ照合までやったのはセッション 13 の教訓どおり)。
  内訳は delivered 27 (+1) / vetoed 2 / stalled 29 / announced 1 / active 2 (+自己) /
  merging 0 / in_review 0。セッション 18 の「マージ済み・完了前」解釈が出口の実観測で確定し、
  中間状態の全ライフサイクル in_review→merging→delivered が P-0163 単体で完観測された。
  なお merging の観測サンプルは依然 1 例であり、in_review のように複数行き先を
  持つかどうかは未判定
- 塞ぎ手 P-0092 (announced) / P-0161 (active) は不変。delivered 増は完結方向の進行だが
  blocking 数に反映されないため弁判定への含意はゼロ — 判定原則
  (announced+active==0 の実測一択) は不変
- main は不動 (a962e4211)。PR #524 は mergeable_state=clean 初手継続
  (main 不動時は unknown を経由しない — セッション 17 観測の再裏取り)。
  head=5d24c8932 不変・ci/GitGuardian success
- ops-state ブランチは進行 (669768126→bca671802、先頭は heart: beat 51)
- 前置条件の再実測 (劣化なし):

```
$ python3 ops/tools/deploy_continuity.py --dry-run       # rc=0, valve ok=false (blocking 2), targets 3/3 ready
$ python3 -m unittest ops.tests.test_deploy_continuity   # Ran 39 tests OK
$ python3 -m unittest discover -s ops/tests -t .         # Ran 281 tests OK
$ python3 ops/validate.py                                # 0 error, 11 warning (既存)
$ git fetch origin && git rev-parse origin/main          # a962e4211 (不動)
$ GET /pulls/524  → open / draft=true / mergeable_state=clean / head=5d24c8932
$ GET .../commits/5d24c8932/check-runs → ci success, GitGuardian success
```

- **次のセッションへの一言**: 同じ。冒頭で dry-run の弁を見るのが最初で最後の分岐。
  開いていれば即日実施 (手順は PROGRESS セッション 3・4 の固定どおり)、開いていなければ
  再実測だけして軽く閉じてよい。「0 か否か」だけを見る原則は維持。unknown 待ちの注意は継続:
  mergeable_state が unknown なら数秒〜十数秒待って再取得、dirty 確定時のみ
  update-branch → ci 再 green を待つ

### セッション 20 (2026-08-23, 弁確認のみ。project/p-0164 checkout, リポジトリルートで実行)

- 冒頭で dry-run を実測 → **弁は閉じたまま**、blocking 2 件で不変
  (P-0092 / P-0161。checked=61)。固定指示どおり実演習は見送り
- 本セッションの実イベントその 1: **台帳が完全凍結 3 例目** (19→20)。
  全 61 件の id+state 一括 diff で完全一致 (churn ゼロを実測)、カウント内訳も
  同一 (delivered 27 / vetoed 2 / stalled 29 / announced 1 / active 2 (+自己))。
  ops-state ブランチは進行したが中身は heart beat 更新のみ
  (bca671802→e3ef18ede、beat 51→56) — 「ブランチ進行 ≠ 台帳遷移」の追加例
- 実イベントその 2: **main 史上 3 度目の前進** (a962e4211→ab8a9a15e、
  PR #529 curriculum 採択)。待機中のドラフト PR #524 は無介入で clean を維持し
  **材料耐久 4 例目**: base 移動直後は mergeable_state=unknown を返し
  (12 秒待ちで clean 復帰 — セッション 11 発見の罠の 4 回目の再現)、
  head=5d24c8932 不変・ci/GitGuardian success
- 実イベントその 3 (**新規に起きたこと**): validate.py が **1 error に劣化**
  (`archive.jsonl: origin/main の内容と先頭一致しない`)。原因は PR #529 の
  curriculum が ops/projects/archive.jsonl へ 6 行追記したことで、main より
  遅れている自ブランチでは startswith 検査が機械的に失敗するもの
  (改変・削除の検知が目的なので期待どおりの動作ではある)。前例 0f2aeb5a0
  (PR #523 取り込み時) に倣い **origin/main を本ブランチへ merge して解消**
  (コンフリクト無し、本プロジェクト領域への影響なし)。merge 後に validate は
  0 error へ復帰。併せて merge により全体 unittest は **281→294 に変動**
  (main 側で test_openclaw_bridge.py 削除・test_syncthing_acceptance.py 追加)
- 前置条件の再実測 (merge 後・劣化なし):

```
$ python3 ops/tools/deploy_continuity.py --dry-run       # rc=0, valve ok=false (blocking 2), targets 3/3 ready
$ python3 -m unittest ops.tests.test_deploy_continuity   # Ran 39 tests OK
$ python3 -m unittest discover -s ops/tests -t .         # Ran 294 tests OK (281 から変動)
$ python3 ops/validate.py                                # 0 error, 11 warning (既存)
$ git fetch origin && git rev-parse origin/main          # ab8a9a15e (本日 3 度目の前進)
$ GET /pulls/524  → open / draft=true / mergeable_state=clean (unknown 12 秒後に復帰) / head=5d24c8932 不変
$ GET .../commits/5d24c8932/check-runs → ci success, GitGuardian success
```

- **次のセッションへの一言**: 同じ。冒頭で dry-run の弁を見るのが最初で最後の分岐。
  開いていれば即日実施 (手順は PROGRESS セッション 3・4 の固定どおり)、開いていなければ
  再実測だけして軽く閉じてよい。「0 か否か」だけを見る原則は維持。追加の注意が 1 つ:
  validate.py が archive.jsonl 先頭不一致の error を出したら壊れていない —
  **「自ブランチが main より遅れている」サイン**なので origin/main を merge すれば
  消える (curriculum 採択のたびに起きうる。詳細は発見節)。前置条件の unittest 件数は
  固定値でない (281→294 実測) ので「OK か」で判断すること

### セッション 21 (2026-08-23, 弁確認のみ。project/p-0164 checkout, リポジトリルートで実行)

- 冒頭で dry-run を実測 → **弁は閉じたまま、blocking が 2→3 に増加**
  (P-0092 / P-0161 / **P-0175 新規**。checked=62)。固定指示どおり実演習は見送り
- 本セッションの実イベントその 1: **blocking 数の増加を初観測**。これまでの推移は
  減少 (6→5→4→3→2) か不変のみで、「増える」方向は本日が初出。増分は新規プロジェクト
  **P-0175「秘密の給水塔が止まった日を先に演じる — External Secrets の唯一の上流
  (Doppler) を一時遮断し、既存の Secret とアプリが何時間持つかを秒で実測する」**が
  台帳へ active 入場したもの (総計 61→62)。台帳は ops-state のみで伸びており
  main は不動 — validate.py の archive.jsonl 先頭不一致は今回起きなかった
  (main が動いていないため。セッション 20 の定型が発火しなかった例)
- 実イベントその 2: 台帳内訳 (セッション 20 の凍結状態からの差分) は
  delivered 27 / vetoed 2 / stalled 29 / announced 1 (P-0092) /
  active 3 (**P-0161, P-0164 自己, P-0175**)。セッション 20 記録の
  「active 2 (+自己)」への実差分は +1 (P-0175) のみ
- 小ネタだが構造的に重要: **弁の excluded_self が実データで効いている**。
  P-0164 自身も台帳では active であり、除外が無ければ自分自身で永遠に弁が開かない。
  dry-run の `"excluded_self": ["P-0164"]` がその実測表示
- main 不動 (`git log e0acc7eab..origin/main` 空)。ドラフト PR #524 は初手から
  mergeable_state=clean・ci/GitGuardian success (head=5d24c8932 不変。
  base が動いていないので unknown 罠は発火せず)

```
$ python3 ops/tools/deploy_continuity.py --dry-run       # rc=0, valve ok=false (blocking 3), targets 3/3 ready
$ python3 -m unittest ops.tests.test_deploy_continuity   # Ran 39 tests OK
$ python3 -m unittest discover -s ops/tests -t .         # Ran 294 tests OK
$ python3 ops/validate.py                                # 0 error, 11 warning (既存)
$ git log e0acc7eab..origin/main                         # 空 = main 不動
$ GET /pulls/524  → open / draft=true / mergeable_state=clean / head=5d24c8932 不変
$ GET .../commits/5d24c8932/check-runs → ci success, GitGuardian success
```

- **次のセッションへの一言**: 同じ。冒頭で dry-run の弁を見るのが最初で最後の分岐。
  開いていれば即日実施 (手順は PROGRESS セッション 3・4 の固定どおり)、開いていなければ
  再実測だけして軽く閉じてよい。「0 か否か」だけを見る原則は維持。blocking 数の
  増減は弁判定への含意ゼロ (減少が進捗でないことはセッション 12〜14 で反例済み、
  本日は「増加」方向の反例も追加) — 見るのは announced+active==0 かだけ。
  なお validate.py が archive.jsonl 先頭不一致 error を出したら origin/main を
  merge すれば消える (curriculum 採択のたび。セッション 20 の発見節参照)

### セッション 22 (2026-08-23, 弁確認のみ。project/p-0164 checkout, リポジトリルートで実行)

- 冒頭で dry-run を実測 → **弁は閉じたまま、blocking メンバーは 3 件で不変**
  (P-0092 / P-0161 / P-0175。checked=62)。固定指示どおり実演習は見送り
- 本セッションの実イベントその 1: **台帳完全凍結 4 例目** (21→22)。
  全 62 件の id+state 写像が完全一致。P-0175 の active 入場時点
  (ops-state beat58, 2026-08-23T08:59:10Z) から beat71 (09:13:58Z)・本セッション
  fetch 時点まで projects.json の全 version を直接 trace し、id+state の遷移ゼロを
  実測 (台帳内訳も不変: delivered 27 / vetoed 2 / stalled 29 / announced 1 /
  active 3)。ops-state ブランチ自体は beat 更新で進行中 — 「ブランチ進行 ≠ 台帳遷移」
  の定常形
- 実イベントその 2 (**小ネタだが判定方法の実例**): 同期間の raw diff には
  state 以外の成長がある — beat58→beat71 の間に P-0175 へ `drift_count: 0`
  フィールドが追加されていた。「完全凍結」の判定は id+state 写像で行うのが正しく、
  raw diff の一致を条件にすると非 state の churn で誤判定しかねない
- main 不動 (`git merge-base --is-ancestor origin/main HEAD` が真。merge 不要)。
  ドラフト PR #524 は初手から mergeable_state=clean・ci/GitGuardian success
  (head=5d24c8932 不変。base が動いていないので unknown 罠も発火せず)
- 前置条件の再実測 (劣化なし):

```
$ python3 ops/tools/deploy_continuity.py --dry-run       # rc=0, valve ok=false (blocking 3), targets 3/3 ready
$ python3 -m unittest ops.tests.test_deploy_continuity   # Ran 39 tests OK
$ python3 -m unittest discover -s ops/tests -t .         # Ran 294 tests OK
$ python3 ops/validate.py                                # 0 error, 11 warning (既存)
$ git merge-base --is-ancestor origin/main HEAD          # 真 = main 不動・merge 不要
$ GET /pulls/524  → open / draft=true / mergeable_state=clean / head=5d24c8932 不変
$ GET .../commits/5d24c8932/check-runs → ci success, GitGuardian success
```

- **次のセッションへの一言**: 同じ。冒頭で dry-run の弁を見るのが最初で最後の分岐。
  開いていれば即日実施 (手順は PROGRESS セッション 3・4 の固定どおり)、開いていなければ
  再実測だけして軽く閉じてよい。「0 か否か」だけを見る原則は維持。
  validate.py が archive.jsonl 先頭不一致 error を出したら origin/main を merge
  (セッション 20 の発見節参照)。unittest 件数は固定値でないので「OK か」で判断すること

### セッション 25 (2026-08-23, 弁確認のみ。project/p-0164 checkout, リポジトリルートで実行)

- 冒頭で dry-run を実測 → **弁は閉じたまま、だが blocking が 3→2 に減少**
  (P-0092 / P-0175。checked=62)。固定指示どおり実演習は見送り
- 実イベントその 1 (**blocking メンバーに初の変動**): P-0161 が **active→stalled** へ遷移
  (`stalled_reason: budget_exhausted`、beat90 decide 09:35:24Z)。台帳差分はこの 1 行のみ
  (61/62 不変、id+state 一括 diff。内訳 delivered 27 / vetoed 2 / stalled 29→30 /
  announced 1 / active 3→2)。セッション 14 型の「流出による減少」の再現 — delivered 不変の
 まま blocking は減る
- 実イベントその 2 (**停滞は吸収状態**): projects.json 直近 200 version を全走査して遷移を
  集計したところ、**stalled からの出口遷移が 1 例も無かった** (センサス:
  active→in_review 17 / proposed→active 12 / active→stalled 10 ← P-0161 が 10 例目 /
  in_review→active 9 / announced→active 5 / proposed→announced 5 / merging→soaking 4 /
  soaking→delivered 4 / in_review→stalled 2 / merging→delivered 2 / proposed→stalled 1)。
  観測窓内で stalled は終端状態であり、**P-0161 の弁塞ぎ脱落は恒久的とみなせる**。
  併せて stalled_reason は標準フィールド (歴代 spec_error 1000 / review_rejected 907 /
  human_stop 800 / budget_exhausted 590 等) で新物ではなく、台帳の budget.used_tokens は
  全員常時 0 (budget_exhausted で落ちた P-0161 ですら used_tokens=0) — **誰が次に枯れるかを
  台帳数値から予測することは不可能**
- 弁開放の見通し (更新): 残塞ぎ手は P-0092 (announced) / P-0175 (active) の 2 件。
  delivered はセッション 11 以降ずっと 27 で凍結中なので、**最も確率の高い弁開放経路は
  「両塞ぎ手の budget 枯渇による stalled 流出」** (P-0175 は active なのでそのまま stalled 化
  しうる。P-0092 は announced からどう抜けるか未観測 — announced→active は歴代 5 例あるが
  announced→stalled 等の直接出口は観測無し)。delivered 増を待つ必要は無い
- main 不動 (`git merge-base --is-ancestor origin/main HEAD` が真。merge 不要)。
  ドラフト PR #524 は head=5d24c8932 不変・初手 mergeable_state=unknown →
  **12 秒待ちで clean 復帰** (罠の 6 回目。ただし本例は **base 移動も push も無いのに発火した
  初例** — unknown は base 移動への束縛が無く散発的に現れる。「unknown = 待つ」運用は不変)。
  ci/GitGuardian success
- 前置条件の再実測 (劣化なし):

```
$ python3 ops/tools/deploy_continuity.py --dry-run       # rc=0, valve ok=false (blocking 2), targets 3/3 ready
$ python3 -m unittest ops.tests.test_deploy_continuity   # Ran 39 tests OK
$ python3 -m unittest discover -s ops/tests -t .         # Ran 294 tests OK
$ python3 ops/validate.py                                # 0 error, 11 warning (既存)
$ git merge-base --is-ancestor origin/main HEAD          # 真 = main 不動・merge 不要
$ GET /pulls/524  → open / draft=true / unknown →(12s)→ clean / head=5d24c8932 不変
$ GET .../commits/5d24c8932/check-runs → ci success, GitGuardian success
```

- **次のセッションへの一言**: 同じ。冒頭で dry-run の弁を見るのが最初で最後の分岐。
  開いていれば即日実施 (手順は PROGRESS セッション 3・4 の固定どおり)、開いていなければ
  再実測だけして軽く閉じてよい。残塞ぎ手は P-0092 / P-0175 の 2 件で、budget 枯渇 stalled
  流出が最有力の弁開放経路 (delivered 待ちは非現実的。used_tokens からは予測不能なので
  台帳の state 遷移だけを見ること)。validate.py が archive.jsonl 先頭不一致 error を出したら
  origin/main を merge (セッション 20 の発見節参照)。unittest 件数は固定値でないので
  「OK か」で判断すること

### セッション 24 (2026-08-23, 弁確認のみ。project/p-0164 checkout, リポジトリルートで実行)

- 冒頭で dry-run を実測 → **弁は閉じたまま、blocking メンバーは 3 件で不変**
  (P-0092 / P-0161 / P-0175。checked=62)。固定指示どおり実演習は見送り
- 本セッションの実イベントその 1: **台帳完全凍結 6 例目** (23→24)。
  前例に続き **「projects.json の新 version 自体ゼロ」型 (b)** — 同ファイルを触る
  commit は 09:00:21 (8a4598afe, beat 59 decide) を最後に皆無のまま、beats は
  beat83 → beat86 (09:30:50Z) まで進行。id+state 写像は 62/62 完全一致
  (内訳 delivered 27 / vetoed 2 / stalled 29 / announced 1 / active 3 も不変)。
  型 (b) 凍結が **2 例連続**となり、「version 出力停止」は一過性でなく持続しうる状態だと判明
- 実イベントその 2: **blocking プロジェクト P-0161 のブランチが進行**
  (fetch で fe101d43a→d8cdcb401) したのに台帳上 P-0161 は active のまま不変。
  差分を実測すると同プロジェクト自身の PROGRESS.md 追記のみ (1 file, +24) で、
  演習材料やコードには無関係。「ブランチ進行 ≠ 台帳遷移」の第 4 例
  (ops-state 全体 / 自プロジェクト / p-0163 / p-0161)
- main 不動 (`git merge-base --is-ancestor origin/main HEAD` が真。merge 不要)。
  ドラフト PR #524 は初手から mergeable_state=clean・ci/GitGuardian success
  (head=5d24c8932 不変。base が動いていないので unknown 罠も発火せず)
- 前置条件の再実測 (劣化なし):

```
$ python3 ops/tools/deploy_continuity.py --dry-run       # rc=0, valve ok=false (blocking 3), targets 3/3 ready
$ python3 -m unittest ops.tests.test_deploy_continuity   # Ran 39 tests OK
$ python3 -m unittest discover -s ops/tests -t .         # Ran 294 tests OK
$ python3 ops/validate.py                                # 0 error, 11 warning (既存)
$ git merge-base --is-ancestor origin/main HEAD          # 真 = main 不動・merge 不要
$ GET /pulls/524  → open / draft=true / mergeable_state=clean / head=5d24c8932 不変
$ GET .../commits/5d24c8932/check-runs → ci success, GitGuardian success
```

- **次のセッションへの一言**: 同じ。冒頭で dry-run の弁を見るのが最初で最後の分岐。
  開いていれば即日実施 (手順は PROGRESS セッション 3・4 の固定どおり)、開いていなければ
  再実測だけして軽く閉じてよい。「0 か否か」だけを見る原則は維持。
  validate.py が archive.jsonl 先頭不一致 error を出したら origin/main を merge
  (セッション 20 の発見節参照)。unittest 件数は固定値でないので「OK か」で判断すること

### セッション 23 (2026-08-23, 弁確認のみ。project/p-0164 checkout, リポジトリルートで実行)

- 冒頭で dry-run を実測 → **弁は閉じたまま、blocking メンバーは 3 件で不変**
  (P-0092 / P-0161 / P-0175。checked=62)。固定指示どおり実演習は見送り
- 本セッションの実イベントその 1: **台帳完全凍結 5 例目** (22→23)。
  全 62 件の id+state 写像が完全一致 (内訳 delivered 27 / vetoed 2 / stalled 29 /
  announced 1 / active 3 も不変。announced+active メンバー同一:
  P-0092 / P-0161 / P-0164 自己 / P-0175)
- 実イベントその 2 (**凍結の構造に 2 様態を発見**): 今回の凍結は
  **「projects.json の新 version 自体がゼロ」型**だった — 同ファイルを触る commit は
  09:00:21 (8a4598af, P-0175 drift_count 追加) を最後に一切出ておらず、ops-state の
  beats が beat71 → beat83 (09:27:28Z) まで進む間に version 数 0。対して前例の
  21→22 凍結は「新 version は出続けるが中身の id+state が不変」型 (drift_count
  churn あり) だった。つまり完全凍結には **(a) raw version が出るが id+state 不変 /
  (b) raw version 自体が止まる** の 2 様態があり、「凍結 = version 間 diff 見る」
  方式だと (b) を観測対象にできない。(a)(b) どちらでも id+state 写像の一致判定は
  正しく機能する — 判定方法を変える必要は無いが、「version 出力の有無」自体が
  heart の書き込み挙動の観測点になることは覚えておいてよい
- main 不動 (`git merge-base --is-ancestor origin/main HEAD` が真。merge 不要)。
  ドラフト PR #524 は初手から mergeable_state=clean・ci/GitGuardian success
  (head=5d24c8932 不変。base が動いていないので unknown 罠も発火せず)
- 前置条件の再実測 (劣化なし):

```
$ python3 ops/tools/deploy_continuity.py --dry-run       # rc=0, valve ok=false (blocking 3), targets 3/3 ready
$ python3 -m unittest ops.tests.test_deploy_continuity   # Ran 39 tests OK
$ python3 -m unittest discover -s ops/tests -t .         # Ran 294 tests OK
$ python3 ops/validate.py                                # 0 error, 11 warning (既存)
$ git merge-base --is-ancestor origin/main HEAD          # 真 = main 不動・merge 不要
$ GET /pulls/524  → open / draft=true / mergeable_state=clean / head=5d24c8932 不変
$ GET .../commits/5d24c8932/check-runs → ci success, GitGuardian success
```

- **次のセッションへの一言**: 同じ。冒頭で dry-run の弁を見るのが最初で最後の分岐。
  開いていれば即日実施 (手順は PROGRESS セッション 3・4 の固定どおり)、開いていなければ
  再実測だけして軽く閉じてよい。「0 か否か」だけを見る原則は維持。
  validate.py が archive.jsonl 先頭不一致 error を出したら origin/main を merge
  (セッション 20 の発見節参照)。unittest 件数は固定値でないので「OK か」で判断すること

### セッション 26 (2026-08-23, 弁確認のみ。project/p-0164 checkout, リポジトリルートで実行)

- 冒頭で dry-run を実測 → **弁は閉じたまま、blocking が 2→5 に増加**
  (P-0092 announced / P-0175・P-0181・P-0182・P-0185 active。checked=65)。
  固定指示どおり実演習は見送り
- 実イベントその 1 (**blocking 数の増加 2 例目**): main 史上 4 度目の前進
  (curriculum 採択 PR #530、09:36:38Z マージ) の 3 案が台帳へ active 入場し
  総計 62→65。内訳 delivered 27 / vetoed 2 / stalled 30 / announced 1 /
  active 5 (P-0164 自己含む)。セッション 21 型の「新規入場による増加」であり、
  流出型減少 (セッション 25 の 3→2) の直後に増加 — **blocking 数には単調性も
  周期性も無い**ことが累積実測で確定。「減るまで待つ」以外の読みは不可能
- 実イベントその 2 (**validate.py 劣化からの復旧**): main 前進により
  archive.jsonl 先頭一致検査が error を発火 (セッション 20 型の 3 例目)。
  前例どおり origin/main を自ブランチへ merge して復旧 (0 error)。
  merge の差分は ops/projects/archive.jsonl への追記 6 行のみで本プロジェクト領域への影響なし
- **新入場 3 件のうち P-0181 が argocd-application-controller の OOMKill 実測案**
  — 本演習の scale 対象と同一コンポーネントを扱う初の他プロジェクト。
  弁開放条件は「announced/active == 0」なので両方が同時に走ることは構造上有り得ないが、
  順番の問題は残る (先に P-0181 が controller を落とす演習をやった直後だと
  本演習の baseline 確認・catchup 計時に残滓が乗る恐れ。--run 冒頭の baseline
  replicas=1 & ready=1 確認が防波堤になるはずだが、当日は dry-run の targets_seen を
  念入りに見てから始めること)
- main 動いた (→ 上記 merge 済み)。ドラフト PR #524 は head=5d24c8932 不変・
  初手 mergeable_state=**clean**・ci/GitGuardian success。**base 移動済みなのに
  unknown 罠が発火しなかった初例** (#530 マージから数分経過後の確認ではあるが、
  セッション 18 は同程度の間隔で発火した) —   セッション 25 (base 移動無しで発火) と
  本例で「unknown は base 移動と無関係に散発する」説が双方向から裏付けられた。
  材料耐久 5 例目 (main 前進 × 4 回すべてで clean 維持)
- 前置条件の再実測 (merge 後・劣化なし):

```
$ python3 ops/tools/deploy_continuity.py --dry-run       # rc=0, valve ok=false (blocking 5), targets 3/3 ready
$ python3 -m unittest ops.tests.test_deploy_continuity   # Ran 39 tests OK
$ python3 -m unittest discover ops/tests                 # Ran 294 tests OK
$ python3 ops/validate.py                                # 0 error, 11 warning (既存)
$ git show origin/main..HEAD --stat                      # merge commit で archive.jsonl +6 行のみ
$ GET /pulls/524  → open / draft=true / mergeable_state=clean / head=5d24c8932 不変
$ GET .../commits/5d24c8932/check-runs → ci success, GitGuardian success
```

- **次のセッションへの一言**: 同じ。冒頭で dry-run の弁を見るのが最初で最後の分岐。
  開いていれば即日実施 (手順は PROGRESS セッション 3・4 の固定どおり)、開いていなければ
  再実測だけして軽く閉じてよい。塞ぎ手は 5 件に増えた (P-0092 announced /
  P-0175・P-0181・P-0182・P-0185 active) — 新入場の active 3 件も budget 枯渇 stalled
  流出しうるので「delivered 待ち」より「budget 枯渇待ち」仮説は変わらず最有力。
  main が動いたら validate.py の archive.jsonl error が出るので origin/main を merge
  (セッション 20 の発見節参照。本セッションで 3 例目を実施済み)。
  unittest 件数は固定値でないので「OK か」で判断すること

### セッション 27 (2026-08-23, 弁確認のみ。project/p-0164 checkout, リポジトリルートで実行)

- 冒頭で dry-run を実測 → **弁は閉じたまま、blocking 5 件でメンバー不変**
  (P-0092 announced / P-0175・P-0181・P-0182・P-0185 active。checked=65、
  09:51:48Z 実測)。固定指示どおり実演習は見送り。台帳内訳の再確認も同一
  (delivered 27 / vetoed 2 / stalled 30 / announced 1 / active 5)
- **PR #524 材料耐久 6 例目**: GET /pulls/524 → open / draft=true /
  mergeable_state=**clean** / head=5d24c8932 不変。head commit の check-runs で
  ci success・GitGuardian success も再確認 (main 前進 #530 以降そのまま)。
  main は 9b9470594 で不動・自ブランチ包含済みにつき validate 劣化なし・merge 不要
- 前置条件の再実測 (劣化なし):

```
$ python3 ops/tools/deploy_continuity.py --dry-run       # rc=0, valve ok=false (blocking 5), targets 3/3 ready
$ python3 -m unittest ops.tests.test_deploy_continuity   # Ran 39 tests OK
$ python3 -m unittest discover -s ops/tests -t .         # Ran 294 tests OK
$ python3 ops/validate.py                                # 0 error, 11 warning (既存)
```

- **当日健診の材料を実測**: `kubectl get pods -n argocd` で
  `argocd-application-controller-0` が **ready=true かつ restartCount=4・
  lastState.reason=OOMKilled** であることを確認 — P-0181 の題材 (controller の
  OOMKill) は台帳上 active でなくても過去に実際に起きている状態であり、
  「replicas=1 & ready=1」だけでは OOM ループ直後の残滓を検出できない。
  --run 当日は dry-run の targets_seen に加えて pod の restartCount/lastState を
  目視してから始めること (発見節に追記済み)。なお他コンポーネントの pod は
  5 種類すべて RESTARTS=19・lastState.reason=Unknown で一様だった (ノード級の
  イベント痕跡の可能性。本プロジェクト領域外につき発見節に記録のみ)
- コード変更なし
- **次のセッションへの一言**: 同じ。冒頭で dry-run の弁を見るのが最初で最後の分岐。
  開いていれば即日実施 (手順はセッション 3・4 の固定どおり + 当日健診として pod の
  restartCount/lastState 目視)、開いていなければ再実測だけして軽く閉じてよい。
  塞ぎ手 5 件 (P-0092 announced / P-0175・P-0181・P-0182・P-0185 active)。
  P-0092 は irreversible な immich-postgres メジャー更新で、active 化すれば長期滞在が
  濃厚。P-0182 が落地すると stalled 復活レーンで弁はさらに遠のく (発見節参照)。
  main が動いたら validate.py の archive.jsonl error → origin/main を merge。
  unittest 件数は固定値でないので「OK か」で判断すること

### セッション 28 (2026-08-23, 弁確認のみ。project/p-0164 checkout, リポジトリルートで実行)

- 冒頭で dry-run を実測 → **弁は閉じたまま、blocking 5 件でメンバー不変**
  (P-0092 announced / P-0175・P-0181・P-0182・P-0185 active。checked=65、
  09:58:04Z 実測)。固定指示どおり実演習は見送り。**blocking メンバー不変は
  26→27→28 で 3 セッション連続**になった
- **台帳完全凍結の継続 (様態 b)**: projects.json を触る commit は
  df707db34 09:42:37Z (beat 94 decide) を最後に皆無で、beats は 94→106
  (d2ce77036 09:57:15Z) まで進行。id+state 写像はセッション 27 記録の内訳と完全一致
  (delivered 27 / vetoed 2 / stalled 30 / announced 1 / active 5、total 65)。
  様態 (b)「新 version 自体ゼロ」型は 24 例目に続き継続観測
- **PR #524 材料耐久 7 例目**: GET /pulls/524 → open / draft=true /
  mergeable_state=**clean** / head=5d24c8932 不変。head commit の check-runs で
  ci success・GitGuardian success 再確認。main は 9b9470594 で不動・自ブランチ
  包含済みにつき validate 劣化なし・merge 不要
- 前置条件の再実測 (劣化なし):

```
$ python3 ops/tools/deploy_continuity.py --dry-run       # rc=0, valve ok=false (blocking 5), targets 3/3 ready
$ python3 -m unittest ops.tests.test_deploy_continuity   # Ran 39 tests OK
$ python3 -m unittest discover -s ops/tests -t .         # Ran 294 tests OK
$ python3 ops/validate.py                                # 0 error, 11 warning (既存)
```

- **当日健診表の訂正 (セッション 27 の記録を訂正)**: `kubectl get pods -n argocd`
  を custom-columns で精密再読したところ、RESTARTS=19 一様は**誤りだった**。
  実際は applicationset/dex/notifications/redis の 4 pod (開始 2025-12-16) が
  19/Unknown、**repo-server と server は restartCount=1・lastState.reason=Completed**
  (開始 2026-08-05T17:31Z)。startTime から本日中の pod 再作成は否定できるため、
  セッション 27 側の読み取りが雑だっただけ。当日健診は本実測形式 (custom-columns で
  READY/RESTARTS/LASTSTATE を pod 単位) で行うこと。application-controller-0 は
  セッション 27 と同一値 (ready=true・restartCount=4・lastState=OOMKilled)
- **常時部分同期ループを発見 (当日計測の妨害源になりうる)**: immich / vaultwarden /
  syncthing / coder の 4 Application が同一リビジョン 9b9470594 への自動同期を
  約 5 分周期で繰り返している (10:00Z 前後の events 実測: "Partial sync operation ...
  succeeded" が 4 アプリで反復)。原因まで特定: 各 ns の **download-ledger CronJob**
  (25 * * * *、本日 03:39 頃導入) が Git 管理の空 ConfigMap `download-budget` へ
  Python-urllib で書き込むためアプリが恒常 OutOfSync になり、self-heal 同期が
  ループしている (sync は Succeeded を返すのに drift は解消しない)。
  --run 当日の catchup 計測はこの環境ノイズと重なる — ラベル commit の伝播は
  アプリ単位で時刻を取り、revision+label 到達で判定すること (発見節に追記済み)。
  なお 4 アプリ選択は「download-ledger を持つ ns」と正確に一致する
- コード変更なし
- **次のセッションへの一言**: 同じ。冒頭で dry-run の弁を見るのが最初で最後の分岐。
  開いていれば即日実施 (手順はセッション 3・4 の固定 + 当日健診: pod の
  restartCount/lastState を custom-columns で pod 単位確認 + 4 アプリの常時同期
  ルールを頭に入れてから計測)、開いていなければ再実測だけして軽く閉じてよい。
  塞ぎ手 5 件不変 (P-0092 announced / P-0175・P-0181・P-0182・P-0185 active)。
  main が動いたら validate.py の archive.jsonl error → origin/main を merge

### セッション 29 (2026-08-23, 弁確認のみ。project/p-0164 checkout, リポジトリルートで実行)

- 冒頭で dry-run を実測 → **弁は閉じたまま、blocking が 5→4 件に減少**
  (P-0092 announced / P-0175・P-0181・P-0182 active。checked=65、10:09Z 実測)。
  固定指示どおり実演習は見送り。コード変更は無し
- 本セッションの実イベント: **PR #530 の新入場 3 案から最初の出口が出た** —
  **P-0185 が active→in_review** (beat113 decide, f12b56740 10:05:33Z)。台帳差分は
  この 1 行のみ (64/65 不変、id+state 一括 diff。d2ce77036 beat106 時点との比較)。
  内訳 delivered 27 / vetoed 2 / stalled 30 / announced 1 / active 4 (+自己) /
  in_review 1。active→in_review 方向は観測窓内 3 例目 (P-0163 / P-0174 に続き)。
  P-0185 レコードに理由系フィールドは無し (state のみの変化)
- 凍結様態 (b)「projects.json の新 version 自体ゼロ」型は beat113 で明けた —
  同ファイルを触る commit は beat94 (09:42:37Z, df707db34) を最後に皆無だったが
  beat113 で再開。凍結明けの差分もセッション 17 型の**最小単位 (1 件)**で、
  「凍結明け = 大規模入れ替わり」は 2 例目の反例
- blocking 減少 (5→4) の出口は横滑り in_review — 「減少 = 弁前進」反例の追加。
  in_review→active 復帰は過去 2 例あるため P-0185 が塞ぎ手に戻る可能性もある。
  判定原則 (announced+active==0 の実測一択) は不変。残塞ぎ手は
  P-0092 announced + P-0175/P-0181/P-0182 active の 4 件
- **PR #524 材料耐久 8 例目**: GET /pulls/524 → open / draft=true /
  mergeable_state=**clean** 初手 / head=5d24c8932 不変。head commit の check-runs で
  ci success・GitGuardian success 再確認。main は 9b9470594 で不動・自ブランチ包含済み
  につき validate 劣化なし・merge 不要
- 前置条件の再実測 (劣化なし):

```
$ python3 ops/tools/deploy_continuity.py --dry-run       # rc=0, valve ok=false (blocking 4), targets 3/3 ready
$ python3 -m unittest ops.tests.test_deploy_continuity   # Ran 39 tests OK
$ python3 -m unittest discover -s ops/tests -t .         # Ran 294 tests OK
$ python3 ops/validate.py                                # 0 error, 11 warning (既存)
$ git merge-base --is-ancestor origin/main HEAD          # 真 = main 不動・merge 不要
$ GET /pulls/524  → open / draft=true / mergeable_state=clean / head=5d24c8932 不変
$ GET .../commits/5d24c8932/check-runs → ci success, GitGuardian success
```

- **次のセッションへの一言**: 同じ。冒頭で dry-run の弁を見るのが最初で最後の分岐。
  開いていれば即日実施 (手順はセッション 3・4 の固定 + 当日健診: pod の
  restartCount/lastState を custom-columns で pod 単位確認 + 4 アプリの常時同期
  ループを頭に入れてから計測)、開いていなければ再実測だけして軽く閉じてよい。
  塞ぎ手 4 件 (P-0092 announced / P-0175・P-0181・P-0182 active)。P-0185 は
  in_review へ横滑りしたばかり — active 戻り (過去 2 例) なら blocking は 4 に戻るので
  「4 件」に意味を持たせず 0 か否かだけを見ること。main が動いたら validate.py の
  archive.jsonl error → origin/main を merge

## 発見 (スコープ外。curriculum が拾うもの)

- (セッション 28, クラスタ側の環境ノイズ) **download-ledger CronJob 導入以降、
  その 4 ns の Application が恒常 OutOfSync → 約 5 分周期の部分同期ループに乗っている**。
  Git 上は空 `data` の ConfigMap download-budget へ CronJob が Python-urllib で
  実データを書くため drift が絶えず、auto-sync+selfHeal が「Succeeded を返すが
  drift 解消にならない」同期を繰り返す (10:00Z events + app status 実測)。
  含意は 2 つ: (a) 本演習の catchup 計測ではこの 4 アプリの同期は commit 到達と
  無関係に定期発火するため、**計測対象の判定は status.sync.revision==新 main かつ
  ラベル到達で行い、アプリ単位で時刻を取る**(targets の vaultwarden/coder は
  ちょうどこのループ内); (b) health reporter / watcher は平時から 4 アプリ分の
  OutOfSync ノイズを常時見ることになり、「ArgoCD 停止中の status 鮮度劣化」観測
  (DoD (3)) の baseline が汚れている前提で読む必要がある。download-budget を
  ignoreDifferences に入れる等の構造修正は本プロジェクト領域外 (syncthing/
  download-ledger 側の論点) につきここに記録するのみ

- (セッション 28, 当日健診の読み取り方法の確定) pod 健診はデフォルト列や目視ではなく
  `kubectl get pods -n argocd -o custom-columns="NAME:.metadata.name,READY:...,RESTARTS:...,LASTSTATE:..."`
  で pod 単位に取ること。セッション 27 の「他コンポーネント 5 種すべて
  RESTARTS=19 一様」は誤読で、実態は repo-server/server が restartCount=1・
  lastState=Completed (開始 2026-08-05 のまま)。lastState の理由種別は
  OOMKilled / Completed / Unknown と様々で、一様性を仮定すると OOM 直後の
  残滓を見落とす

- (セッション 27, P-0182 落地時の構造リスク) blocking の新入場 3 案のうち
  **P-0182 が「stalled からの復活レーン」そのものを提案している** (verify に
  `ops/rules.json` の `runner.continuation` と `ops/heart/reconcile.py` への
  `continuation` 実装を要求)。セッション 25 で「stalled は吸収状態 (200 version
  全走査で出口ゼロ)」と結論したが、これが落地すると停滞 30 件が heart の手で
  再 announced されうる — **「塞ぎ手脱落は恒久的」という前提が壊れ、blocking
  プールが復活可能な非単調集合になる**。本演習の弁 (announced+active==0) にとって
  「待てば減る」モデルすら崩れるため、開放予測はさらに不可能側へ寄る。
  P-0182 が delivered/stalled になった時点で rules.json を見て、continuation が
  実装済みなら「吸収状態」結論を撤回すること

- (セッション 27, 干渉健診の具体化 + 周辺健康の実測) P-0181 干渉メモ
  (セッション 26) の当日手順を具体化: dry-run の targets_seen (replicas/ready)
  に加え **`kubectl get pods -n argocd` の restartCount と lastState を見てから
  --run する**。2026-08-23 09:52Z 実測で application-controller-0 は
  ready=1・restartCount=4・lastState=OOMKilled — OOMKill は仮説でなく過去に
  起きた事実であり、「直前まで OOM ループだった」状態は baseline チェックでは
  検出できない。併せて周辺 pod 5 種が一様に RESTARTS=19・lastState=Unknown だった
  のを記録 (原因未調査。ノード/kubelet 級のイベントを疑う根拠だが本プロジェクト
  領域外)

- (セッション 26, ライフサイクル観測の続き) **blocking 数の増加 2 例目** (2→5)。
  curriculum 採択 (PR #530) の 3 案 P-0181/P-0182/P-0185 が active 入場したことによる
  (総計 62→65)。これまでの方向は減少 (6→5→4→3→2) ・凍結・流出型減少・新規入場型増加と
  全パターン出揃いで、**blocking 数から「弁への近さ」を読む試みは全方向の反例が揃った** —
  判定は常に announced+active==0 の実測一択、という原則が全パターンで成立

- (セッション 26, 他プロジェクトとの干渉リスクの特定) 新入場 P-0181 は
  **argocd-application-controller の OOMKill 実測案**で、本演習 (P-0164) の scale 対象と
  同一コンポーネント。安全弁 (announced/active==0) により同時実施は構造的に起きないが、
  **時間的に近接した実施では互いの計測を汚しうる** (P-0181 の OOMKill 再現が controller を
  落とした直後は pod 再作成・再 sync が本演習の baseline/catchup に乗る)。
  弁開放時に P-0181 がまだ台帳に居る/居た直後の場合、dry-run の targets_seen で
  ready=1・replicas=1 を確認してから --run すること (スクリプトの baseline チェックが
  この防波堤)

- (セッション 26, PR #524 罠の追加観測) **base 移動済みなのに unknown が発火しない初例**
  (PR #530 マージ 09:36:38Z の数分後に初手 clean)。セッション 11「base 移動直後は必ず
  unknown」→ セッション 25「base 移動無しでも発火しうる」→ 本例「base 移動があっても
  発火しないことがある」と反例が双方向に揃い、**unknown は base 移動と無関係な散発現象**
  と扱うのが正しい。「unknown = 待って再取得」運用は不変

- (セッション 25, ライフサイクル観測の続き) **blocking 数の流出型減少が 2 例目** (3→2)。
  P-0161 が `stalled_reason: budget_exhausted` で active→stalled (beat90 decide,
  09:35:24Z)、台帳差分はこの 1 行のみ (61/62 不変)。**projects.json 直近 200 version の
  全走査で stalled からの出口遷移はゼロ = 停滞は観測窓内で吸収状態** — 「塞ぎ手は復活
  しうる」仮説は支持されず、流出した塞ぎ手は恒久的に戻らないと扱ってよい。
  遷移センサス (200 version): active→in_review 17 / proposed→active 12 / active→stalled 10 /
  in_review→active 9 / announced→active 5 / proposed→announced 5 / merging→soaking 4 /
  soaking→delivered 4 / in_review→stalled 2 / merging→delivered 2 / proposed→stalled 1。
  なお stalled_reason 自体は標準フィールド (歴代 spec_error 1000 / review_rejected 907 /
  human_stop 800 / budget_exhausted 590 等) であり、台帳の budget.used_tokens は全員常時 0
  (budget_exhausted で落ちた P-0161 ですら 0) — **budget 枯渇の予兆を台帳数値から読むことは
  不可能で、観測は state 遷移一択**

- (セッション 25, PR #524 罠の追加観測) mergeable_state=unknown は **base 移動が無くても
  散発的に現れる** 初例 (main 不動・head push 無しで発火、12 秒待ちで clean 復帰)。
  「unknown の原因は base 移動直後の計算遅延」というセッション 11 以来の説明は必要条件に
  すぎなかった。「unknown = 待って再取得」運用自体は不変でよい

- (セッション 24, ライフサイクル観測の続き) **台帳完全凍結 6 例目** (23→24、id+state
  写像 62/62 一致) で、**凍結様態 (b)「projects.json の新 version 自体ゼロ」が 2 例連続**
  となった — 同ファイルを触る commit は 09:00:21 を最後に皆無のまま beats が
  beat83→beat86 まで進行。「version 出力停止」は一過性でなく持続しうる状態であり、
  heart が台帳書き込みを休止している期間の存在が実測された。判定方法 (id+state 写像)
  の変更は依然不要。併せて **blocking プロジェクト P-0161 のブランチ進行を第 4 例として
  観測** (fe101d43a→d8cdcb401、差分は同プロジェクト自身の PROGRESS 追記のみ、台帳は
  active 不変) — 「ブランチ進行 ≠ 台帳遷移」が blocking 他プロジェクト系統で複数成立。
  自タスク unittest 39 / 全体 294 green、validate 0 error、targets 3/3 ready、
  前置条件の劣化無し

- (セッション 23, ライフサイクル観測の続き) **台帳完全凍結 5 例目** (22→23、
  id+state 写像 62/62 一致、内訳・announced+active メンバーも完全同一) と同時に、
  **完全凍結には 2 様態があることを発見**: (a) 新 version は出続けるが id+state が
  不変型 (前例: 21→22, drift_count churn あり) と、(b) **projects.json の新 version
  自体がゼロになる型** (本例: 同ファイルを触る commit は 09:00:21 を最後に皆無で、
  beats が beat71→beat83 まで進んでいる間も version 数 0)。「凍結 = version 間
  diff」方式では (b) はそもそも観測対象にならず、id+state 写像判定は (a)(b) 両方で
  正しく機能する。version 出力の有無自体は heart の書き込み挙動の観測点。
  自タスク unittest 39 / 全体 294 green、validate 0 error、targets 3/3 ready、
  前置条件の劣化無し

- (セッション 22, ライフサイクル観測の続き) **台帳完全凍結 4 例目** (21→22、
  id+state 写像 62/62 一致)。P-0175 の active 入場 (ops-state beat58,
  08:59:10Z) 以降の state 遷移ゼロを projects.json 全 version の直接 trace で
  確認。併せて **「凍結」判定は id+state 写像ですべき** ことの実例: 同期間の
  raw diff には P-0175 への `drift_count` フィールド追加という非 state churn が
  含まれており、raw diff 一致を条件にすると台帳が育っているのに「凍結」と誤判定する。
  自タスク unittest 39 / 全体 294 green、targets 3/3 ready、前置条件の劣化無し

- (セッション 21, ライフサイクル観測の続き) **blocking 数の増加を初観測** (2→3)。
  新規 P-0175 の台帳 active 入場による (総計 61→62、ops-state のみで伸び main 不動)。
  これまで観測された方向は減少 (6→5→4→3→2) か凍結のみで、「塞ぎ手は時間とともに
  減っていく/減らない」という単調性の期待は双方向に破れた。弁判定は常に
  dry-run の「announced+active == 0 か」実測一択の原則は不変。
  併せて **自己除外 (excluded_self) が実データで load-bearing であることを確認**:
  P-0164 自身も台帳上は active であり、除外ロジックが無ければ演習は永遠に始められない。
  自タスク unittest 39 / 全体 294 green、targets 3/3 ready、前置条件の劣化無し

- (セッション 20, 受入検証の保守) **validate.py の archive.jsonl 先頭一致検査は、
  main より遅れた長寿命ブランチ上で curriculum 採択のたびに誤爆する**。
  check_projects_archive はローカルの ops/projects/archive.jsonl が
  `git show origin/main:` の内容の startswith であることを見るため、curriculum が
  追記して main が長くなった瞬間から「遅れ」自体が error として報じられる
  (改変検知という本来の目的からは正しい挙動)。対処は origin/main を自ブランチへ
  merge するだけでよい (PR #523 時の 0f2aeb5a0 に続き今回 PR #529 でも実施 —
  これで 2 例。curriculum 採択 → プロジェクトブランチの validate が赤、の
  定型として覚えてよい)

- (セッション 20, 前置条件の数値) **全体 unittest の件数は固定値ではない**
  (281→294 を実測)。プロジェクト横断の discover は他プロジェクトのテストファイルが
  main へ入る/消えるたびに増減するので、前置条件の記録・比較は件数ではなく
  「全 OK か」で行うべき。自タスクの 39 件 (test_deploy_continuity) は不変

- (セッション 20, ライフサイクル観測の続き) **完全凍結の 3 例目** (19→20、
  id+state 一括 diff で churn ゼロまで確認)。14→15 (初観測) / 15→16 (2 連続) は
  連続していたが、今回はそれらと非連続な位置で再出現 — 凍結凍結⇄凍結明けが交互に
  来る周期性は無い。塞ぎ手 P-0092 (announced) / P-0161 (active) は不変で、
  delivered も 27 のまま動かず。判定原則 (announced+active==0 の実測一択) は不変

- (セッション 19, ライフサイクル観測の続き) **中間状態 `merging` の出口を初観測**
  (P-0163: merging→delivered、セッション 18 初出の翌セッションで実観測)。
  「マージ済み・完了前」という状態表現の仮説が出口方向の実測で確定し、
  in_review→merging→delivered の全ライフサイクルが同一プロジェクト上で完結観測された。
  in_review には stalled 行きの出口もあった (セッション 14) ので「中間状態 = 停滞予備軍」とも
  読めたが、merging は初サンプルでいきなり完結方向へ抜けた。ただしサンプル 1 例なので
  in_review 的な多方向性を持つかは未判定。なお delivered が増えても blocking 数
  (announced+active) は動かないため、完結方向の進行が弁開放に直結するわけではない

- (セッション 19, 台帳差分の検査手法) カウント比較だけだと補償ペアの churn
  (X: stalled→active と Y: active→stalled が同時に起きてカウント不変、等) を
  見逃す — セッション 13 の教訓の実践として、今回は全 61 件の id+state を
  セッション 17 時点の ops-state commit と一括 diff して churn 無しまで確認した。
  差分検査は「カウント集計」ではなく「id+state の整列 diff」1 発でよい

- (セッション 18, ライフサイクル観測の続き) **未観測だった中間状態 `merging` が台帳に初出**
  (P-0163: in_review→merging)。セッション 8 メモ「コード上散見する review/merging 等の
  中間状態が帳簿上未観測」に対し、in_review (同日実観測) に続き merging も帳簿に現れ、
  コードと台帳の状態集合がほぼ一致した。P-0163 の merging 化はそのプロジェクトの PR #527 の
  main マージと同時に起こっており、**「マージ済み・完了前」の状態表現**と考えるのが妥当。
  これで in_review の観測された出口は →active (2 例) / →stalled (1 例) / →merging (1 例) の
  3 方向。なお P-0163 は往復振動する唯一のプロジェクトだったが今度は完結方向へ抜けた —
  ただしこれを弁開放の前兆と読む根拠は依然無く、塞ぎ手 P-0092/P-0161 が残る限り弁は閉じたまま

- (セッション 18, 演習材料の耐久実測の続き) main 史上 2 度目の前進 (PR #527) でも待機中の
  ドラフト PR #524 は無介入で clean を維持し、材料の耐久説は 3 例目の裏取りを得た。
  main 移動直後の mergeable_state は再び unknown を返した (10 秒待ちで clean 復帰) —
  セッション 11 発見の罠は再現性あり。「unknown = 待つ」運用は確定でよい

- (セッション 17, ライフサイクル観測の続き) **完全凍結の明け方も最小単位だった**。
  15→16 の完全凍結 2 連続の後、台帳差分は P-0163 active→in_review のたった 1 件
  (60/61 件不変を実測)。凍結明け = 大規模入れ替わり、という期待は裏切られた。
  P-0163 はセッション 12→13 で往復を完了済みの **2 周目の in_review 入場**であり、
  台帳上で往復振動する唯一のプロジェクト。in_review 双方向中間状態説をさらに後押しする一方、
  blocking 減少 (4→3) の出口が delivered/stalled でなく横滑り in_review なのは
  「減少 = 弁への前進」反例の追加にすぎず、判定は常に announced+active==0 の実測一択

- (セッション 16, ライフサイクル観測の続き) **完全凍結が 2 セッション連続** (15→16)。
  スナップショット比較の 3 パターンのうち「完全凍結」が連続出現し、凍結は例外的イベントで
  なく定常状態になりうる。判定原則 (announced+active==0 の実測一択) は不変。
  併せて **blocking プロジェクト自身のブランチ進行が台帳遷移を伴わない例**を初観測
  (project/p-0163 が c225da0ae→15a1ff6bd に進んだのに P-0163 は active のまま)。
  「他プロジェクトのブランチが動いている= soon 弁が開く」推論への反例追加 —
  「ブランチ進行 ≠ 台帳遷移」は ops-state 全体進行 (セッション 15) / 自プロジェクト /
  blocking 他プロジェクトの 3 系統すべてで成立

- (セッション 15, ライフサイクル観測の続き) **総計・メンバーシップ双方が完全不変の

- (セッション 15, ライフサイクル観測の続き) **総計・メンバーシップ双方が完全不変の
  セッション間を初観測** (14→15)。12→13 は「総計不変・中身入れ替わり」、13→14 は
  「総計変動・流出型」だったので、スナップショット比較が取りうる 3 パターン
  (変動あり/中身だけ入れ替わり/完全凍結) が出揃った。いずれのパターンも弁判定への
  含意はゼロ — 判定は常に dry-run の「announced+active == 0 か」一択、という原則が
  全パターンで成立することを実測で裏取り完了。併せて ops-state ブランチの進行
  (heartbeat beat 更新) は projects.json の状態遷移を伴わないことがあるため、
  「ブランチが動いた=台帳が動いた」という省略は不可

- (セッション 14, ライフサイクル観測の続き) **blocking 数の減少が進捗を意味しない第 3 の
  反例: stalled への流出**。セッション 12→13 は「総計不変・中身だけ入れ替わり」だったが、
  13→14 では総計自体が動き (active 5→4 / in_review 1→0 / stalled 26→28)、抜けた
  P-0157 (active→stalled) と P-0174 (in_review→stalled) は揃って停滞行きだった。
  delivered が 1 件も増えていないのに blocking は減る — 「弁に近づいている/遠ざかっている」
  を台帳カウントから読むのはどちらも不可能で、判定は常に「announced+active == 0 か」の
  実測一択。併せて **in_review → stalled の出口方向を初観測** (従来は in_review→active
  のみ)。in_review は差し戻しから停滞まで複数の行き先を持つ中間状態であり、
  in_review の出現・消滅はいずれも弁とは無相関

- (セッション 13, ライフサイクル観測の続き) **台帳の総計が不変でもメンバーシップは
  入れ替わる**: セッション 12→13 で active/in_review のカウントは 1 件も変わらなかったが、
  P-0163 と P-0174 が同時に逆方向へ遷移していた (P-0163: in_review→active 復帰、
  P-0174: active→in_review)。カウントのスナップショット比較は churn を隠す — 「動きが
  無かった」という判断は個別プロジェクトの状態照合なしには下せない。併せて
  active→in_review が 2 例となり in_review 双方向中間状態説の確度を上げたほか、
  P-0163 単体では active→in_review→active の完全な往復が初観測された

- (セッション 12, ライフサイクル観測の続き) **active → in_review の逆方向遷移を初観測**
  (P-0163)。セッション 8〜10 で in_review→active を 2 例見ていたので、in_review は
  双方向に出入りできる中間状態 (=レビュー差し戻し/保留) と確度を上げた。なお in_review
  へ抜けることで blocking 数が減るが、これは演習開始条件 (announced/active=0) の前進では
  ない — 台帳の「減少」を弁の開放と読む誤りへの追加の反例
- (セッション 11, 演習材料の耐久実測) main が初めて動く実イベントで、待機中の
  ドラフト PR #524 が無介入で mergeable_state=clean を維持した。metadata ラベルの
  2 行追加はファイル集合が他と素である限り conflict しない — 演習材料の鮮度維持コストは
  ほぼゼロで、「main 先行 + PR 待機」状態が平時でも普通に成立する。なお GitHub API の
  `mergeable_state` は base 移動直後に必ず `unknown` を返す (計算遅延)。unknown=conflict
  ではないので、update-branch の発火条件は unknown でなく dirty で判定すること
- (セッション 10, ライフサイクル観測の続き) 観測された `in_review` 2 件 (P-0174 /
  P-0163) の遷移先が**揃って active だった** (いずれも翌セッションで実観測)。
  完了 (delivered) 行きの中間状態とは言えず、「レビュー差し戻し/保留」の状態表現と
  考えるのが妥当。in_review の出現を「弁が開く前兆」と読むのは誤り
- (セッション 9, ライフサイクル観測の続き) セッション 8 に初観測された `in_review`
  2 件のうち 1 件 (P-0174) が翌セッションで **active へ遷移するのが実観測された**。
  in_review は中間状態として実在し、そこから active に戻る (弁を再び塞ぐ) ことがある。
  「in_review = 完了目前」と楽観できない
- (セッション 8, セッション 7 発見の補足) 「projects.json の state ライフサイクルに
  『review』『merging』等の中間状態がコード上散見するが帳簿上未観測」という前回メモに対し、
  本日 `in_review` 2 件が台帳に実観測された。中間状態は実在する (弁判定対象外につき影響なし)
- (セッション 3, 環境の小ネタ) `/tmp/opencode` は root 所有で書き込めなかった
  (実測: touch Permission denied)。一時ディレクトリは従来どおり `mktemp -d` へ。
  また `git worktree add -b <branch> <path>` は path の作成に失敗しても branch だけ
  先に作られる (実測。exercise/p-0164-labels がこの経路で生まれたが結果的にそのまま利用)
