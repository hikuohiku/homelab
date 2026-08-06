# review-log — レビュー役の台帳

レビュー役が見つけたものを 1 件 1 エントリで記録する。**誰も空にしない。**
[`ops/inbox.md`](inbox.md) は計画役が毎回空にするので、レビュー役の記憶はここに置く。

- 追記できるのは**レビュー役だけ**。計画役は既存エントリの `状態` 行だけ更新する
- **一度書いた指摘は、状態に関わらず二度と inbox に出さない。** 再掲は催促であり、
  計画役の判断の上書きになる。例外は状況が実際に悪化したときだけで、その場合は
  旧 id を引用した新しいエントリにする
- 指摘が無かった回も「どこを見て詰まらなかったか」を残す。次のレビューが同じ面を
  見ないため
- `完了` / `却下` になって 90 日を過ぎたものは消してよい

書式:

```
## R-001 — <一行の要約>
- レンズ: user | arch
- 見たもの: <実際に開いた画面・読んだファイル・叩いたコマンド>
- 詰まり: <何をしようとして、どこで諦めたか>
- 状態: 未処理 | 起票済 T-XXXX | 完了 | 却下: <理由>
```

計画役は起票するとき、task の `why` に `R-NNN` を含める。これが無いと
「採らなかった」のか「採ったが記録し忘れた」のかが後から区別できない。

---

## R-001 — レビュー役に指示されたダッシュボード確認手順の1行目が RBAC で失敗する
- レンズ: user / 2026-08-06
- 見たもの: `apps/autopilot/prompt-review-user.md`（このプロンプト自身）に書かれた手順をそのまま実行。
  `kubectl -n ops-dashboard get svc ops-dashboard -o jsonpath='{.spec.clusterIP}'`
- 詰まり: `Error from server (Forbidden): services is forbidden: User
  "system:serviceaccount:autopilot:autopilot" cannot get resource "services"`。
  `kubectl auth can-i --list` で確認したところ、`autopilot-reader` ClusterRole
  （`apps/autopilot/rbac.yaml`）が読める resource 一覧に `services` がそもそも含まれていない
  （CHARTER §5.5 の一覧とも一致：pods/PVC/nodes/namespaces/events/deployments 等はあるが
  services が無い）。プロンプトが「実際に確認しろ」と名指しで渡してくるコマンドの、その
  最初の一歩が今の権限では実行不能。代替として cluster 内 DNS 名
  （`ops-dashboard.ops-dashboard.svc.cluster.local`）への直接 curl で IP 取得を回避し HTML
  自体は取得できたが、これはプロンプトに書かれた手段ではなく、次にこのプロンプトを読む
  自分（または別のレビュー役）が毎回同じ Forbidden に当たって同じ回避を再発見する必要がある
- 状態: 起票済 T-0160

## R-002 — レビュー役に「実際に描画しろ」と指示した本人の変更が、まだ動いている Pod に届いていない
- レンズ: user / 2026-08-06
- 見たもの: R-001 の回避で取得した HTML を chromium で描画しようとした。`which chromium` /
  `apk info -e chromium` / `find / -iname '*chromium*'`（すべて見つからず）。
  `images/autopilot/Dockerfile` と `apps/autopilot/deployment.yaml` の git 履歴
  （`git log --since` で 8eacc31 以降の変更を確認）
- 詰まり: 稼働中の Pod（`autopilot-854c989ffd-tp2h8`、Alpine 3.24.1）に chromium も
  font-noto-cjk も入っていない。`images/autopilot/Dockerfile` には commit `8eacc31`
  （2026-08-06T12:43:53Z「レビュー役がダッシュボードを実際に見られるようにする」）で
  chromium/font-noto/font-noto-cjk が追加されているが、`apps/autopilot/deployment.yaml`
  の image digest はその commit 以降一度も更新されていない（同ファイルへの変更コミットは
  `4de23b5` の1件のみで image 行には触れていない）。つまり「レビュー役が実際に見られる
  ようにする」ための変更自体が、レビュー役が動く Pod にまだ配達されていない。想像で書くなの
  原則を守ろうとした結果、今回もマークアップ（HTML テキスト）を読むところまでしかできなかった。
  T-0153（digest ズレを CI で機械検出する仕組み）は「今後同じズレを繰り返さない」対策だが、
  「今まさにこの Pod がズレている」という現在の具体的事実そのものは誰も指摘していない
- 状態: 起票済 T-0161

## R-003 — ダッシュボードの健全性サマリだけでは「(a) いまシステムは健康か」に正しく答えられない
- レンズ: user / 2026-08-06
- 見たもの: R-001 の回避で取得した `ops/dashboard/index.html`（生成 2026-08-06 16:45 UTC）の
  先頭 pulsebox。`homelab のアプリ` 欄が赤 (`dot--crit`) で「10 / 13 正常」
  「落ちている: coder、immich、vaultwarden」
- 詰まり: この表示だけを見た初見の人間は「3 サービスが落ちている」と読む。実際は
  T-0106（Doppler キー未登録による ExternalSecret の SecretSyncedError）がこの3
  Application の集約 health を Degraded に引き上げているだけで、Pod は全て
  Running/Ready・実サービスへの影響は無い（CHARTER §2 に明記済みの既知の状態）。
  ダッシュボード上にはこの注記が無く、同じ HTML 内で事情を辿るには優先度順 19 件の
  待ち行列を 14 番目（T-0106、B2 キー発行の依頼文言で health への言及なし）まで
  スクロールしたうえ、さらに「完了」扱いの T-0122（この因果関係を実際に説明している
  唯一の記述）まで辿る必要がある。VISION が名指しで求める3つの問い
  「(a) いまシステムは健康か」に、ダッシュボード単体では正しく・素早く答えられない
- 状態: 起票済 T-0162

（以下2件は PR #378（`autopilot/run-173-review-arch`）で arch レンズが独立に R-001/R-002 として
記録したもの。user レンズ側が同じ回で先に R-001〜R-003 を確定していたため、統合時に採番し直した。
統合の経緯は journal 参照）

## R-004 — CHARTER.md が肥大化に上限を持たず、2日で8倍に伸びている
- レンズ: arch / 2026-08-06
- 見たもの: `ops/CHARTER.md`（現在 86,133 bytes）、`ops/validate.py` の `check_ledger_size()`
  （288-300行目、`backlog.json`/`state.json` のみが対象）、`ops/ledger.py` の
  `BACKLOG_MAX_BYTES`/`STATE_MAX_BYTES`（34-35行目）、commit 762807f
  （2026-08-06 "perf(ops): 帳簿を圧縮し、上限を検査に入れる"）、git 履歴上の
  `ops/CHARTER.md` サイズ推移（2026-08-04 10,847 bytes → 2026-08-06 86,133 bytes、
  62 commit で継続的に増加）
- 詰まり: commit 762807f は「autopilot は毎イテレーション VISION/CHARTER/backlog/state を
  全部読み直す。合計 587 KiB のうち9割が二度と行動につながらない過去の記録だった」という
  問題意識で backlog.json/state.json に archive 機構とサイズ上限を導入した。同じ理由は
  CHARTER.md 自身（§0 により毎イテレーション読まれる）にも等しく当てはまるはずだが、
  archive 先も上限検査も無い。実際 CHARTER.md はこの2日で 10.8KB → 86.1KB と backlog.json
  よりも速いペースで太っており、backlog.json 自身の上限（120,000 bytes）にすら迫っている。
  個々の指摘（`issue #56, <日時>の指摘`）を追記する形で伸び続け、圧縮・archive する経路が
  仕組みとして無いため、このまま伸びると commit 762807f が解決したはずの「読み込みコストが
  観測の余地を削る」問題がこの1ファイルに再集中する
- 状態: 起票済 T-0164

## R-005 — REVIEW_EVERY=12 の根拠数値が CHARTER.md 2箇所と loop.sh に三重化している
- レンズ: arch / 2026-08-06
- 見たもの: `ops/CHARTER.md:173-176`（§3 頻度の説明）、`ops/CHARTER.md:740`（§6 冒頭の
  再掲）、`apps/autopilot/loop.sh:35-38`（`REVIEW_EVERY` のコメント）。いずれも「1 イテレーション
  実測約9分（journal run #126〜#155、30起動で264分）、レビューは約1.8時間おき、同じレンズは
  約3.5時間おき、実作業損失8%」という同一の数値・引用範囲がほぼ同じ文面で書かれている
- 詰まり: 3箇所とも独立したテキストとして存在し、どれか1つを直しても他は追随しない構造。
  再測定が必要になったとき（R-004 の CHARTER 肥大化が続けば、1 イテレーションあたりの
  読み込み・思考コストも伸び、この実測値自体が古びる可能性が高い）に1箇所だけ更新されて
  残りが古いまま取り残される。とくに `loop.sh` 側はコード上の意味を持たないコメントなので、
  値がズレても CI にも起動時ログにも表れず、次にこのコメントを読んだ人が誤った前提を持ち帰る
- 状態: 起票済 T-0165
