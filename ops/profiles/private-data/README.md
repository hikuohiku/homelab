# private-data 分離プロファイル (P-0243)

私的データを読む Job を「外へ話せる口がない」状態に置くための錠前。
lethal trifecta (私的データ × 信頼できない内容 × 外部送信経路) の 3 要素の同時存在を、
**ネットワーク層で既定拒否**にして断つ。

| ファイル | 役割 |
|---------|------|
| `networkpolicy.yaml` | NetworkPolicy 正本。`apps/autopilot/networkpolicy.yaml` に同一内容の本番コピー (ArgoCD 管理) |
| `job-template.yaml` | private-data Job の参照断片。実ジョブは `ops/heart/spawn.py` が組み立てる |
| `exfil_drill.py` | in-cluster で拒否を実測するドリル。結果 JSON が台帳基準 1 の証拠になる |
| `demo.json` | ドリル実績の証拠ファイル (`ops/stage3/readiness.json` 基準 1 の evidence_path) |

## 脅威モデル

- **守るもの**: Job が読んだ私的データが外部 (攻撃者管理の endpoint、誤設定先、
  prompt injection で指された任意 URL) へ流れないこと
- **敵の位置**: 私的データを読むコンテナ自身。信頼できない内容 (メール本文等) に
  よって操られて外部へ接続を試みるコードを想定する — 「コードが正直である」ことに
  賭けない。Pod 内のコードが何をしようと、ポリシーが許した宛先にしか届かない
- **なぜコンテナ分割ではなくネットワーク分離か**: P-0161 構想の model/publisher
  コンテナ分割は「私的データを読む手」と「外へ話す口」を物理的に分ける案だったが、
  egress 既定拒否はそれより強い — 同一コンテナに同居しているコードすら外に出られない。
  分割運用は将来の改良として残す (本プロファイルはその前段)

## ポリシーの意味論 (fail-closed)

- 対象: ラベル `private-data=true` を持つ Pod **のみ**。他の Pod・namespace には触れない
- ingress: 全拒否 (入口は不要)。livenessProbe を持つ Pod をこのラベルにしないこと
- egress: kube-dns (kube-system, UDP/TCP 53) のみ許可。**LLM API / GitHub / k8s API
  含め他は全て落ちる**。これは意図的:
  - DNS だけ生かすのは「ポリシーによる拒否」と「名前解決不能」を区別するため
    (P-0224/P-0233 の知見)。殻の中でも名前は解ける
  - 追加の穴は P-0203 egress census (docs/security/) の**実測**由来でのみ開ける。
    推測で穴を開けない (PROJECT.md 設計方針)
- 宣言制: PROJECT.md (仕様 JSON) の `capabilities` に `"private-data"` を書いた
  プロジェクトの Job だけがこのラベルを付く (`ops/heart/spawn.py`)。
  今日この capability を宣言すること = 「DNS 以外出られない envelope で動かす」こと。
  成果物は Pod 外のオーケストレータが回収する形で設計すること

## ドリル (`exfil_drill.py`)

```bash
python3 ops/profiles/private-data/exfil_drill.py --report /tmp/opencode/exfil-drill.json
```

in-cluster の writer 権限で走らせると:

1. ドリル専用 NetworkPolicy (`private-data-drill`) を一時敷設 (本番物とは別名。
   掃除で本番物を壊さない)
2. labeled Pod (ラベル付き) と control Pod (ラベル無し) を立て、両者とも
   example.com の名前解決 → HTTPS GET を試みる。持ち出すデータは存在しない
3. 判定: labeled は解決できて HTTPS だけ落ちる (= ポリシー由来の拒否)、
   control は同じ送信が成功する (= ネットワーク全体が死んでいるわけではない)
4. try/finally + SIGINT/SIGTERM ハンドラで Pod・NP を必ず消し、404 確認までして
   `cleaned_up` を出す。残骸ができても Pod 側の `activeDeadlineSeconds` が自死させる

終了コード 0 = 全判定 true。結果 JSON の判定キーは
`labeled_blocked / unlabeled_allowed / dns_ok_labeled / dns_ok_control / cleaned_up`。

## 由来と決着

- 台帳基準 1 (`ops/stage3/readiness.json`) の元閾値は P-0161 の成果物を想定して
  鋳造されていたが、P-0161 は採択済み・成果未着で pass=false が静止していた
- 本プロファイル (P-0243) がその予約を正当に回収する。NetworkPolicy 系 4 死
  (P-0039 / P-0086 / P-0129 / P-0178) の共倒れ条件だった「クラスタ一発導入」を避け、
  「ラベル選択 × 実測ドリル × 本番恒久痕跡」で決着させる
- 人間レビュー: `apps/` と `ops/heart/` は auto-merge されないパス。本 PR は人間の目を通る
