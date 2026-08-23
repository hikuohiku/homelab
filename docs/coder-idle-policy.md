# coder workspace のアイドル停止ポリシー（草案）

node01 は単一ノード（4 vCPU / 11.7 GiB allocatable）で、coder namespace の workspace
Pod は常時稼働している。本書は workspace をいつ止め、何が解放されるかの方針を定める。
P-0143 の産出物。実消費の数字は `ops/tools/coder_idle_audit.sh` の収集結果
（`ops/projects/logs/P-0143/idle-audit.json`、schema `coder-idle-audit/v1`）が一次資料。

> **状態: 草案。** §1 の実測表が埋まっていないため、§2 の推奨閾値は暫定値である。
> 実測が committed された時点で「草案」を外す。

## 1. 実測表（未計測）

**この節にはまだ数字が無い。** 収集スクリプトは完成しているが、実行には kubeconfig または
SA token を持つ環境（runner Job 内など）が必要で、2026-08-23 時点で実行できていない。
捏造した数字を置かないため、計測完了まで空のまま残す。

計測方法（read のみ。クラスタ到達可能な環境で repo root から）:

```console
ops/tools/coder_idle_audit.sh -s 5 -i 10
```

出力 `ops/projects/logs/P-0143/idle-audit.json` の各要素を下表へ転記する。
列定義は schema `coder-idle-audit/v1` と対応:

| workspace | user | 分類 | requests CPU/メモリ | 実使用 平均/最大 CPU | 実使用 メモリ | PVC 要求 GiB | PVC 使用 GiB |
|-----------|------|------|---------------------|----------------------|---------------|--------------|--------------|
| （未計測） | | | | | | | |

解放量の合計は JSON の `reclaimable` をそのまま引用する:

| 指標 | requests_based（capacity 差分） | usage_based（観測窓の実解放） |
|------|--------------------------------|-------------------------------|
| CPU | （未計測） | （未計測） |
| メモリ | （未計測） | （未計測） |
| PVC GiB | （未計測。**Pod 停止では空かない**。local-path のディスクを実際に空けるには PVC 削除が前提で不可逆） | — |

分類の意味と再判定: 分類は観測窓内の平均 CPU が閾値未満かつ最大 CPU がスパイク上限未満
であれば idle（既定 50m / 500m、`CODER_AUDIT_IDLE_CPU_*` で上書き可）。生サンプルと
閾値は各 workspace の `classification_basis` に残るので、後から人間が再判定できる。

## 2. 対処案 — Coder の autostop（テンプレート設定）

### Coder の autostop の仕組み（公式ドキュメントによる事実、2026-08-23 閲覧）

- autostop は「ユーザー活動が無くなってから N 時間後に停止」する機能。活動の判定は
  **Coder が検知するセッション**（code-server / VS Code Remote、JetBrains Gateway、
  web terminal、`coder ssh`、Coder Tasks の agent status）で行われる。
  **OS 内の背景プロセスや `kubectl exec` は活動と数えられない。**
- テンプレート側の設定は `coder templates edit personal --default-ttl <duration>`
  （UI の "Default autostop"）。活動のたびに停止期限が activity bump（既定 1h）だけ延びる。
  `--autostop-reminder <duration>` で期限前の通知も出せる。
- 公式の注記: **autostop は workspace 作成前にテンプレートで有効化されている必要があり、
  既存の稼働中 workspace には遡って適用されない。** 既存分は UI の Workspace settings →
  Schedule での個別設定か手動 stop を併用する。
- 強制的な定期再起動（autostop requirement）、quiet hours、dormancy（休止 workspace の
  自動削除）は **Premium 機能**でこの環境では使えない。「最後の活動から N 時間」の
  下限は作れるが、「セッション張りっぱなしの放置」に対する強制停止は作れない。

### 推奨閾値（暫定 — §1 の実測が出るまで確定しない）

| 設定 | 推奨値 | 理由 |
|------|--------|------|
| `--default-ttl` | 8h | 人間の利用は夜間中心の断続利用。最終活動から 8h で止まるため毎晩のアイドル分が回収される。連続作業中は activity bump が延長し続けるので日中の作業は中断しない |
| `--activity-bump` | 既定（1h）のまま | 短くすると離席中の誤停止が増える。停止コスト自体は低い（次節）ので 1h で十分 |
| `--autostop-reminder` | 30m | 期限が近いことを人間が見える化する |

- 停止のコストは低い: `/home/coder` は PVC に永続し、コンテナイメージは digest pin 済み
  なのでノードにキャッシュされる。再起動は Pod 再スケジュール + startup script 数十秒〜。
  失うのはメモリ上の状態のみ（tmux/screen 内のプロセス等）。
- §1 の実測で「日中も大半が idle」のようなパターンが出たら 4h へ短縮を検討する。
  逆に agent の長時間タスクが頻繁に切れるなら bump の延長または個別 TTL で緩める。
- **autostop で解放されるのは CPU とメモリのみ。** PVC（`home_disk_size` パラメータ、
  既定 10Gi・mutable=false）は止めても残り、ディスク圧迫は解消しない。PVC サイズ過大の
  是正は作り直しを伴う不可逆な判断なので、実測表の PVC 使用率が出てからの別プロジェクト。

## 3. 除外条件 — 器自身の足場を止めない

Coder の活動判定は「セッションが開いているか」であって CPU 使用量ではない。ここが
本ポリシーの落とし穴であり、除外条件はこれから導く:

1. **homelab 自身の常駐系を coder workspace に入れない**（原則）。autopilot 本体 /
   ops-dashboard / 監視系は autopilot namespace の Deployment・CronJob で動いており
   workspace 外である。workspace は「止まってよい器」だけが入る。例外を作るときは
   本書に列挙してから置く。autostop が効く前提なら、常駐系は自然に除外される
   （常駐系はセッションを開かず動く → 活動無し扱いで止まる → 止まって困るものは
   そもそも workspace に置いてはいけない、という整理）。
2. **長時間の無人ジョブ**（エージェントの長時間タスクなど）を workspace で回すときは、
   `coder ssh` 等のセッションを保持するか Coder Tasks 経由で活動報告させる。
   生の `kubectl exec` や workspace 内だけで完結する背景処理は活動と数えられず、
   「忙しいのに止まる」。終わったら切れる運用にすれば TTL との共存も不要になる。
3. **逆方向の抜け**: SSH / terminal を開きっぱなしにする使い方をすると autostop は
   実質効かない（永続的に active 扱い）。「autostop を入れたのに夜間も解放されない」の
   典型原因。接続を切るのが利用規律となる。

データ安全性: workspace stop は Pod を消すだけで `/home/coder`（PVC）は残る。
データ消失はない。

## 4. 本書の限界

- §1 未計測のため推奨閾値は暫定。idle-audit.json の committed 版が §1 に入った時点で
  草案を外し、必要なら閾値を改訂する。
- OSS の autostop は「最後の活動から N 時間」であり、セッション放置には効かない
  （強制停止は Premium の autostop requirement が必要）。node01 が requests ベースで
  逼迫していることが実測された場合の抜本的対処（workspace の廃止・統合、memory limits
  の付与判断 — T-0055 の教訓にある「実測の裏付け」）は本書の外の別論点とする。

## 参考

- Coder docs — Workspace scheduling（user guide / admin templates）:
  https://coder.com/docs/user-guides/workspace-scheduling 、
  https://coder.com/docs/admin/templates/managing-templates/schedule （2026-08-23 閲覧）
- Coder CLI — templates edit（`--default-ttl` / `--activity-bump` / `--autostop-reminder`）:
  https://coder.com/docs/reference/cli/templates_edit （2026-08-23 閲覧）
- 収集スクリプト: `ops/tools/coder_idle_audit.sh`（P-0143、read のみ）
