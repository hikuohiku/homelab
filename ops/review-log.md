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
- 状態: 未処理

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
- 状態: 未処理

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
- 状態: 未処理
