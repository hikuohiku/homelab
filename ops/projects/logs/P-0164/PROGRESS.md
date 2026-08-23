# P-0164 — PROGRESS

## 状態

- initializer 完了: PROJECT.md 初版を作成し commit 済み。
- **セッション 2 (2026-08-23) 完了**: 演習統括スクリプト + unittest を実装し、
  verify のうち unittest と dry-run の 2 項目を green 自力実測。
  残るは report.json (verify 第 1 項) = 実演習の実施のみ。

### 次のセッションへの一言

**実演習 (--run) をやって report.json を出すのが残りすべて。** ただし安全弁が
閉まっている間は絶対に始めないこと (下記「安全弁の現在値」)。弁が開いたら:
`python3 ops/tools/deploy_continuity.py --run --notes-file <watcher等の観察JSON>`
で統括スクリプトに任せ、自分は Git 側 (main への可逆小変更 2 commit を PR merge)
と観察記録だけを担う。手順詳細は deploy_continuity.py モジュール docstring と
このファイルの「実演習の手順」節。**dry-run を先に回して弁の判定を見てから。**

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

### 実演習の手順 (次セッション用チェックリスト)

1. `python3 ops/tools/deploy_continuity.py --dry-run` → 弁 ok:true を確認
   (2026-08-23 時点: P-0092 announced + P-0116/P-0157/P-0161/P-0163 active で閉鎖中。
   他プロジェクトが delivered/stalled になるのを待つしかない)
2. バックグラウンドで `--run --max-wait 900 --settle 30 --dwell 60` を起動
   (nohup 等。標準出力に進行 JSON、stderr に復帰ログ)
3. 演習アプリ 2 つ (vaultwarden / coder。EXERCISE_APPS 定数) の
   application.yaml にラベル追加 commit を 2 本作り、PR 経由で main に merge:
   - apps/vaultwarden/application.yaml の metadata に
     `labels: {p0164.continuity: "1"}` 追加
   - apps/coder/application.yaml に同様 `p0164.continuity: "2"` を追加
   - LABEL_KEY=`p0164.continuity` はスクリプトが live の Application CR 上で見に行く
     キー。値は何でもよい (key の存在だけを見る)
   - metadata.labels 変更はワークロード再起動を誘発しない (touches_apps=false 両立)
   - **1 PR に 2 commit でも 2 PR でもよい** (merge で main に積めばいい)
4. スクリプトが自動で scale 1 → 計時 → report.json 書き出しまで運ぶ
5. watcher/critic の報告は `--notes-file` で observations に入れるか、
   PROGRESS.md の証跡に時刻付きで貼る (DoD (3) の対象。verify は見ない)

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

## 発見 (スコープ外。curriculum が拾うもの)

- なし (今回の範囲では)。強いて挙げれば「projects.json の state ライフサイクルに
  『review』『merging』等の中間状態がコード上散見されるが帳簿上は
  delivered/stalled/active/announced/vetoed の 5 状態しか観測していない」程度。
  弁の判定に影響する話ではないので深追いしていない
