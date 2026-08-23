# private-data プロファイル — 私的データを読む手に、外へ伸びる口を持たせない

P-0161 で作った Job テンプレート規約。VISION 段階 3 (Gmail / Calendar 等の生活ドメイン)
を開放する前提条件として、「lethal trifecta」の 3 要素が**同時に**揃わない器を実証するための
もの。扱うデータは合成 fixture のみで、本物の私的データには一切触れない。

## lethal trifecta (三要素) と、それぞれがどこで断たれるか

Simon Willison の「lethal trifecta」: LLM エージェント事故の条件は
**(A) 私的データへのアクセス ×(B) 信頼できない内容の読み込み ×(C) 外部への送信経路**
が同時に揃うこと。このテンプレートは 3 要素ごとに別の場所で断つ:

| 要素 | 器の中での位置 | 断たれる場所 | 機械検査 |
|------|--------------|------------|---------|
| **(A) 私的データへのアクセス** | 合成メール fixture (Secret `p0161-mail-fixture`) | model Pod だけが mount (`readOnly: true`)。publisher Pod の YAML 全体に secret 名が登場しない — mount も env 参照も不可 | `test_model_mounts_private_data_readonly` / `test_publisher_never_references_private_secret` |
| **(B) 信頼できない内容** | fixture 本文 | model 内で消費され、出力へは差出人・件名などのメタ情報のみが流れる。本文を shell 変数や eval に通す経路を作らない (command は静的スクリプト) | `test_readme_covers_all_three_elements` (本文非流入は job.yaml の model command を目視確認。機械検査は digest 産出部の静的性まで) |
| **(C) 外部への送信経路** | NetworkPolicy + credential 分離 | model Pod は egress 完全拒否 (`egress: []`、DNS 含む) 配下かつ **env ゼロ** (API 鍵も push token も受けない)。push 能力を持つのは publisher Pod だけ | `test_egress_is_empty` / `test_model_container_has_no_env_at_all` / `test_publisher_holds_push_credential_only_via_secretkeyref` |

補足: 要素 (C) は 2 重壁。「ネットワーク的に出られない」(NetworkPolicy) と
「出られても送る鍵が無い」(env ゼロ)。前者が効いていれば後者は使われないが、
k3s の netpol 効果は本実証で初めて実測されるため、片方だけの依存にしない。

## 構成

```
                        ┌─ NetworkPolicy p0161-model-deny-egress ─┐
                        │  podSelector: app=p0161-...-model       │
                        │  egress: []   ← DNS 含む全拒否          │
                        ▼                                         │
┌──────────────────────────────────────┐        ┌──────────────────────────────────────────┐
│ Job p0161-private-data-model         │        │ Job p0161-private-data-publisher         │
│                                      │        │                                          │
│ ┌────────────────────┐               │        │  initContainer wait-for-model            │
│ │ model-session      │               │        │    /handoff/DONE を待つ                  │
│ │  env: なし         │               │  PVC   │                                          │
│ │  /private-data ◀─── Secret mount  │  handoff│ ┌────────────────────┐                  │
│ │  (readOnly)        │   (fixture)   │ ──────▶ │ │ publisher           │                 │
│ │  成果物 → /handoff │               │ (使い捨て│ │  env: PUSH TOKEN   │──▶ ops-feedback  │
│ └────────────────────┘               │  64Mi)  │ │  /handoff (ro)     │    ブランチ      │
│                                      │        │ │  成果物 → emptyDir  │                 │
└──────────────────────────────────────┘        │ │    (/publish) → git push │          │
                                                │ └────────────────────┘                  │
                                                └──────────────────────────────────────────┘
```

## 設計判断の記録 (spec からの読み替えと理由)

spec dod (1) は「model セッションコンテナが egress deny-all の NetworkPolicy 配下 /
publisher コンテナが emptyDir の成果物を push」という 2 コンテナ同一 Pod を想定していた
ように見えるが、それは Kubernetes の機能として作れない:

1. **NetworkPolicy は Pod 単位**であり、同一 Pod 内のコンテナを区別できない。
   publisher の push (TCP 443 out) を許した時点で model も同じ経路を使える。
   「deny-all 配下の model」と「push する publisher」を両立させるには Pod を分けるしかない。
   → model / publisher を別 Job・別 Pod に分離した
2. **emptyDir は Pod 内にしか存在できない**ため Pod 間の受け渡し媒体にならない。
   → 使い捨て PVC (`p0161-handoff`, local-path 64Mi, 撤収時に消す) を受け渡しに使う。
     「publisher は emptyDir 上に組んだ成果物だけを push する」規約自体は維持しており、
     publisher は handoff PVC を readOnly で読んで自分の emptyDir (`/publish`) に
     取り込んでから push する (push 入力に handoff を直結させない)

この 2 点は k8s の構造的事実であり、段階 3 審査でも同じ議論が必ず出るので先に文書化した。

## ファイル一覧

| ファイル | 役割 |
|---------|------|
| `job.yaml` | 受け渡し PVC + model Job + publisher Job。実行/撤収/再実行の手順は冒頭コメント |
| `networkpolicy.yaml` | model Pod 専用の egress 完全拒否 (`egress: []`) |
| `secret-fixture.yaml` | 合成メール fixture (`.invalid` ドメインのみ)。writer SA では apply 不可なので管理者 CLI で |

## 実行手順 (demo run)

```bash
# 0. 前回 run の残骸があるなら消す (PVC 再作成が必須 — 下の「再実行時の注意」)
kubectl delete -f ops/profiles/private-data/job.yaml --ignore-not-found

# 1. fixture Secret (writer SA では不可 → 管理者 kubeconfig の CLI)
kubectl apply -f ops/profiles/private-data/secret-fixture.yaml
# 2. NetworkPolicy (Job より先に apply する — Pod 起動後の適用だと
#    接続済みコネクションの取り扱いが実装依存になるため)
kubectl apply -f ops/profiles/private-data/networkpolicy.yaml
# 3. Jobs
kubectl apply -f ops/profiles/private-data/job.yaml
# 4. 待つ
kubectl wait --for=condition=complete --timeout=15m \
  job/p0161-private-data-model job/p0161-private-data-publisher -n autopilot
# 5. 証拠を集める (demo.json の素)
kubectl logs -n autopilot -l job-name=p0161-private-data-model --tail=-1
kubectl logs -n autopilot -l job-name=p0161-private-data-publisher --tail=-1
git fetch origin ops-feedback && git show origin/ops-feedback:ops/feedback/demo/P-0161/egress_probe.txt
```

成功の判定基準:
- model ログに `DENIED rc=… url=…` が並び `ALLOWED` が 1 本も無い (rc=6 は DNS 断、
  rc=7 は接続拒否、rc=28 はタイムアウト。どれも拒否の証拠)
- `ops/feedback/demo/P-0161/digest.md` がブランチに着地している (fast-forward 追加のみ。
  既存ファイルには触れない)
- 撤収完了後に上記リソースがクラスタから消えている

## 撤収

```bash
kubectl delete -f ops/profiles/private-data/job.yaml       # PVC も一緒に消える
kubectl delete -f ops/profiles/private-data/networkpolicy.yaml
kubectl delete secret p0161-mail-fixture -n autopilot
```

apps/ 配下ではないので ArgoCD はこれらを知らず、常設リソースにはしない (PROJECT.md「やらないこと」)。

## 再実行時の注意

- **PVC を消さずに再実行すると前回 run の sentinel (`DONE`) が残り、publisher が古い
  成果物を push しうる。** model コンテナ冒頭の掃除 (`rm -rf /handoff/*`) は「掃除より
  先に待ち側が sentinel を観測する窓」までは塞げない (P-0035 型教訓)。apply 前に必ず
  PVC を消して作り直す。誤着地の検出用に digest.md には `generated_at` を入れてある
- `.spec.template` は immutable。job.yaml を変えて再実行するときは delete → apply
  (同名再適用の substrate 制約と同根)

## 制限と未実証

- **k3s で NetworkPolicy が実際に効くかは demo run の実測を待つ** (apps/ 配下に
  NetworkPolicy は本件が初めて)。効かなかった場合は実験失敗として記録する —
  「たぶん効く」を段階 3 の審査材料にしないため
- memory limits を付けていない (substrate.md「memory limits は実測の裏付けなしに付けない」)。
  `test_no_memory_limits` がこの規約を機械検査する
- model-session はデモ用の代役 (シェルスクリプト)。本番ではモデルセッションランタイムに
  差し替えるが、その際も「env ゼロ」「Secret readOnly mount」「egress deny-all」の
  3 条件は変えてはならない — 変えるならこのプロファイルの改訂 PR として
- runner / heart への組み込み (capabilities に応じた自動適用等) は範囲外。
  本ファイル群は「手で apply して実証して撤収する」1 回の実験のための器

## 機械検査

`python3 -m unittest ops.tests.test_private_data_profile` (CI の discover にも乗る)。
テストは YAML の**構造制約**を見る: model の env 非存在・mount の readOnly・
publisher からの私的データ参照ゼロ・NP の egress 空・podSelector 一致など。
挙動の実測 (probe が実際に DENIED になるか) はクラスタ上の demo run が担う —
両者は代替ではなく補完。
