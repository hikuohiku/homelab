# P-0144 — tailnet が静かに失効する日を先に数える — 全デバイスの鍵期限を実測し、node01 の identity を失ったときの再認証台本を作る

## 目的

クラスタへの到達性 (tailscale-operator、MCP、人間のアクセスすべて) は tailscale 一点依存なのに、
ノード identity や OAuth client には寿命があることを確認した者がいない。失効すれば heart は
生きているのに外部から何も届かず、watchdog (GitHub Actions) だけが鳴る — 「9 日沈黙」の別形が
起きうる。読み取り専用の Tailscale MCP (devices:core:read) が既にあるので、実測と再認証台本を
今日作れる。security セルの空白地帯で、過去 141 案に同型なし。

## 受入チェックリスト

initializer が実測した結果、**3 項目とも現時点で failing**
(2026-08-23、`project/p-0144` の checkout で、リポジトリルートから実行)。
通っている項目は無かったので spec の誤りは無いと判断して進む。

- [ ] `python3 -c "import json; d=json.load(open('ops/projects/logs/P-0144/devices.json')); assert len(d.get('devices',[]))>=1 and all('name' in x for x in d['devices'])"`
  — devices.json が実測の原本として存在し、`devices` 配列が 1 件以上・各要素が `name` を
  持つこと。実測 rc=1 (FileNotFoundError、ファイル未存在)。
- [ ] `test -f docs/tailscale-recovery.md && grep -qE '再認証|reauth|tailscale up' docs/tailscale-recovery.md`
  — 再認証台本が docs/ に存在し、node01 の再ログイン手順を本文に含むこと。
  実測 rc=1 (docs/tailscale-recovery.md 未存在)。
- [ ] `grep -qE 'key_expiry|expiry' ops/projects/logs/P-0144/devices.json`
  — 鍵期限を推測ではなく実測値として devices.json に保持していること。
  実測 rc=2 (grep 対象のファイル未存在)。

verify は DoD の下限であって DoD そのものではない。dod (2) の「key_expiry を期限が近い順に
並べ、node01 と autopilot 関連に印を付けた表」、(3) の台本 3 要素 (node01 再認証 /
OAuth client 失効が MCP・ExternalSecret に与える影響 / briefing へ乗せるべきリードタイムの
提案)、(4) の「読み取り専用で可逆・変更なし」は verify が見張らない —
PROGRESS.md の証跡で示すこと。

## 設計方針

### 前提 (initializer が 2026-08-23 に実読した。調べ直さなくてよい)

- Tailscale MCP は `.mcp.json` に `@yawlabs/tailscale-mcp` で定義済み。
  `TAILSCALE_READONLY=1` / `TAILSCALE_PROFILE=minimal` / tailnet `hikuohiku@gmail.com`。
  credential は Doppler の `TAILSCALE_AGENT_CLIENT_ID/SECRET` (scope `devices:core:read`)
  → `.envrc`。**インフラ参照は必ず `mcp__tailscale__*` 経由** (CLAUDE.md のエージェント操作ルール)
- OAuth client の消費側は 2 系統ある: `apps/tailscale-operator/` (クラスタ内) と
  `apps/external-secrets/tailscale-oauth-external-secret.yaml`
  (Doppler `TAILSCALE_CLIENT_ID/SECRET` → tailscale ns の Secret `tailscale-oauth`)。
  エージェント用 read-only client とは別物なので、台本 (b) の影響記述はこの区別から書く
- `tailscale up` / `tailscale status` / `just *` は CLI 許可済みの例外 (CLAUDE.md)。
  台本の「どこまで器が自動化でき、どこからが人間の作業か」の線引きはこの事実を根拠にする
- recovery 系ドキュメントの既存流儀: `docs/sops-recovery.md` / `docs/pveproxy-tls.md` /
  `docs/node01-storage.md`。docs/ 直置きの手順書が慣習。新規もこれに倣う
- #36: K8s は API プロキシが Bearer を無視するため ServiceAccount 分離が不可能。
  本件では OAuth client 失効時の影響を書くまでに留め、分離問題の解決は扱わない

### 作り方

1. **実測**: `mcp__tailscale__*` で全デバイスの一覧 (名前・最終接続・鍵失効設定) を取得し、
   生の応答をそのまま `ops/projects/logs/P-0144/devices.json` に保存する
   (整形前の実測形を優先。MCP 応答に expiry 相当のフィールドが無かった場合は
   「取れた事実 / 取れなかった事実」を正直に分けて記録する。捏造しない原則)
2. **表**: key_expiry 昇順の整形表を作り、log ディレクトリに置く。
   node01 と autopilot 関連 (tailscale-operator / proxy 類) に印。対応付けは
   デバイス名の実測後に行い、名前パターンの先験を持たない
3. **台本**: `docs/tailscale-recovery.md` を新設する。(a) node01 の identity 再認証
   (`tailscale up` からの再ログイン。器が自動化できる範囲と人間の手作業の境界を明示)
   (b) OAuth client 失効が MCP (.envrc の agent credentials) と ExternalSecret に与える
   影響と復旧 (c) 期限切れ前に briefing に乗せるべきリードタイムの提案 —
   Tailscale の既定寿命を実測値と突き合わせ、余裕を持った日数を数字で提案する
4. **可逆性**: 全工程が読み取り専用。expiry disable・鍵再発行等の変更は一切しない (DoD (4))

## やらないこと

- **鍵失効 disable、OAuth client の再発行・スコープ変更など、tailnet への変更作業**。
  本件は実測と台本のみ (DoD (4): 読み取り専用で可逆)。鍵作業そのものは人間の担当
- **`.envrc` の credential 重複解消 (旧 T-0141 の残り)**。人間の鍵作業の整理であり触れない
- **#36 の ServiceAccount 分離問題への対処**。API プロキシの構造制約で、解決は人間側の領域
- **失効監視の常設化 (CronJob / heart beat の新設)**。今回は 1 回の実測 + 台本。
  監視に乗せるかどうかはリードタイム提案を出してからの次の論点 (1 PR 1 論点)
- **apps/ 配下の変更** (spec: touches_apps=false)。tailscale-operator / external-secrets の
  manifest 改修はしない
- **ops/backlog.json / ops/state.json / ops/journal/ の編集**。autopilot 直接 push 領域で
  コンフリクトする (CLAUDE.md)
