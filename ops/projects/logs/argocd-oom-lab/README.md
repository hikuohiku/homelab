# argocd OOM lab — 現在 **人間の一手 1 回で再開する**

P-0196 の計測環境。worker 側のコード・検証は完了しており、2026-08-23 のセッション 3 以降
**11 セッション連続**で次の適用待ちに停止している。

## 必要なこと (これだけ)

```bash
kubectl apply -f ops/projects/logs/argocd-oom-lab/proposed-rbac-for-human.yaml
```

- 中身: lab 用 Namespace 2 個 (`argocd-lab-916` / `argocd-lab-1040`) + 最小権限の
  ServiceAccount/Role/RoleBinding。すべて名前空間スコープで、ClusterRole や secrets は含まない
- なぜ自動化しないか: autopilot-writer は rbac.authorization.k8s.io を意図的に持たない
  (自分の権限を自分で広げる経路を作らない設計)。根拠の詳細は YAML 冒頭のコメントと
  PROGRESS.md セッション 2
- 適用後の人間の作業は**無し**。次の worker セッションが冒頭の probe で適用を自動検出し、
  lab 構築 → RSS サンプリング → verdict.json 生成 → lab 削除まで進める
- 適用したくない判断なら、そのままでもよい — curriculum による descope 判断の材料になる

## 現状 (2026-08-23 時点)

| 項目 | 状態 |
|------|------|
| 計画 (`--plan`) | green (クラスタ非接触で通過) |
| rendered manifest | `rendered/` に同梱済み (両系統) |
| lab 構築 (`up`) | RBAC 未適用のため未実施 |
| `rss_series.csv` / `verdict.json` | 未存在 (実測データが無いため。仕様どおり) |

## ファイル一覧

- `proposed-rbac-for-human.yaml` — 人間適用用の最小権限 RBAC (上記コマンド)
- `rendered/` — chart 9.1.6 / 10.4.0 の render 済み manifest (禁止オブジェクト除去済み)
- 実行ツール: `ops/projects/scripts/argocd_oom_lab.py`
  (`up` / `sample` / `status` / `verdict` / `down`)
