# P-0181 — controller memory 実測根拠 (values.yaml 引き直しの裏付け)

2026-08-23 時点。集計は `ops/tools/argocd_memory_series.py` (stdlib のみ) による。
証跡 JSON は同ディレクトリの `memory-series.json` (`--check` で冪等検査可能)。

## 変更内容

`apps/argocd/values.yaml` の `controller.resources` を引き直した。

| 項目 | 旧 | 新 | 決め方 |
|---|---|---|---|
| requests.memory | 256Mi | **320Mi** | p95 (314.9Mi) の切り上げ |
| limits.memory | 0.5Gi (出典のない推測値) | **768Mi** | max(観測ピーク, OOMKill 実績) x 1.5 マージン |
| CPU | 500m / 250m | 変更なし | 今回の論点は memory のみ |

## 実測データ

- データ源: `origin/ops-health-report` ブランチ `ops/health/history/*.jsonl`
  19 日分 (**2026-08-05T08:00:04Z .. 2026-08-23T09:30:05Z**)、**867 サンプル**
  (約 30 分間隔)。pod `argocd-application-controller-0` / コンテナ
  `application-controller` の usage
- **ピーク: 398.0Mi @ 2026-08-23T04:30:08Z** — OOMKill 当日 (finished_at
  08:57:03Z, restarts 4) の午前のサンプル
- **p95: 314.9Mi** / 中央値 261.4Mi / 最小 180.5Mi
- 日次ピーク: 通常日は **297–349Mi** で往復。398Mi への跳ね上がりは
  2026-08-23 のみ (= 事故日だけ異常で、恒常的に増えているわけではない)
- 成長率: **+15.9Ki/day** (30 日外挿 +478Ki)、leak_suspect=False /
  significant=False。判定規則は「30 日外挿 >= 中央値の 10%」で、+478Ki は
  中央値 (261.4Mi) の 0.2% 未満 → spec の「成長率が有意なら seeds.md に恒久策を
  1 行」は**発火条件未達のため見送り** (条件未達での記載は帳簿を汚す)

## なぜ limit を 768Mi にするか (観測ピーク x マージンだけでは足りない)

観測ピーク 398.0Mi は metrics-server 約 30 分間隔の瞬間値なので真のピークの
**下限**である。2026-08-23 は観測 398Mi 未満のまま旧 limit (0.5Gi) で
OOMKilled が 4 回発生しており、サンプルの隙間で使用量が **0.5Gi 以上に達した
ことが確定する**。そこで limit は「観測ピーク 398Mi」と「OOMKill 実績値
0.5Gi」の大きい方にマージン 1.5 を掛けて 768Mi とした。事故日の実績値の
さらに 1.5 倍であり、通常日の日次ピーク (~350Mi) とも 2 倍以上の距離がある。

request を p95 相当 (320Mi) へ引き上げたのは旧 256Mi が中央値 261.4Mi をすら
下回りスケジューリングの実態を反映していなかったため。単一ノード構成なので
この程度の引き上げが即リソース圧迫にはならない。

## 再検査方法

```bash
python3 ops/tools/argocd_memory_series.py            # 人間可読サマリ
python3 ops/tools/argocd_memory_series.py --check    # 証跡 JSON との冪等検査
python3 -m unittest ops.tests.test_argocd_memory_series
```

履歴は append-only だが `--check` は観測窓ピン留め再計算なので追記で落ちない。
より新しい窓で証跡を作り直す場合は
`python3 ops/tools/argocd_memory_series.py --json > ops/projects/logs/P-0181/memory-series.json`
で上書きしてコミットする。

## 既知の限界

- 観測ピークは下限 (上記の通り OOMKill 実績で補正済み)
- 鋸歯状のゆっくりした leak は slope (+15.9Ki/day) でしか拾えない。今回有意なし
- pod 名を StatefulSet 固定名で決め打ちしている (ツール docstring 参照)
