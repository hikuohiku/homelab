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

## 発見 (スコープ外。curriculum が拾うもの)

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
