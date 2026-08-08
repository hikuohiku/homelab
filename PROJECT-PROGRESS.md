# P-0004 — 進捗

<!--
worker が毎セッション追記する。次のセッションのあなたはこれと PROJECT.md と git log しか読まない。
何をやったか / 分かったこと / 未解決の罠 / 次への一言 を残すこと。
-->

## セッション記録

### 2026-08-08 セッション 1 — 受入 3 項目すべて green（commit 3c2d622）

やったこと:

- `images/autopilot/Dockerfile`
  - `npm install -g @anthropic-ai/claude-code` → `@anthropic-ai/claude-code@2.1.223`
  - `FROM node:22-alpine` → `FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32`
  - 冒頭コメントに (a) なぜ pin するか (b) merge → 再ビルド後に digest を manifest
    3 箇所へ反映する別 PR の手順（ghcr.io から digest を実測する curl 込み）を追記
- `ops/inventory.json` に 2 エントリ追加。どちらも `policy: manual` +
  `observability_impact` 付き
  - `claude-code-cli` (kind: npm, upstream: `npm:@anthropic-ai/claude-code`)
  - `autopilot-base-image` (kind: image, upstream: `dockerhub:library/node`)

実測（PR 本文にも同じものを残した）:

| 対象 | 値 | 取り方 |
|---|---|---|
| claude-code | 2.1.223 | `claude --version` / `npm ls -g --depth=0` |
| node | v22.23.2 | `node --version` |
| Alpine | 3.24.1 | `/etc/os-release` |
| node:22-alpine index digest | `sha256:c610fcdf…` | `HEAD registry-1.docker.io/v2/library/node/manifests/22-alpine` の `docker-content-digest` |

自分で回した検証:

- 受入 3 項目 → すべて exit 0
- `python3 ops/validate.py` → 0 error, 2 warning（backlog の T-0035 refs 切れと
  「todo が 0 件」。どちらも本 PR とは無関係の既存 warning）
- `python3 ops/check_version_sync.py` → 全 10 グループ ok

分かったこと / 罠:

- **このセッション自身が実測装置だった。** worker Pod は heart-deployment.yaml の
  `RUNNER_IMAGE`（= 現行 digest `sha256:6d0cf8b1…`）から起動しているので、
  Pod 内で `claude --version` を叩いた結果がそのまま「稼働中の Pod の実測」になる。
  外から調べに行く必要は無い。
- **必ず index（マルチアーキ）の digest を使う。** `Accept:` に
  `application/vnd.oci.image.index.v1+json` を入れて HEAD すると index が返る
  （content-type で確認済み）。Accept を付け忘れると単一アーキの manifest digest を
  掴む可能性があり、その場合 GitHub Actions runner のアーキが変わった時点で壊れる。
- **digest が「挙動を変えない」ことの確認方法**: index → linux/amd64 manifest
  (`sha256:76789712…`) → config blob と辿ると `NODE_VERSION=22.23.2` が読める。
  稼働中の Pod と一致したので、この pin は現状固定であって更新ではない。
- **CI は落ちない**（PROJECT.md の想定どおり）。`ops/check_autopilot_image_pin.py` は
  存在するが `.github/workflows/` のどこからも呼ばれていない（grep 済み）。
  `check_version_sync.py` の "autopilot image digest" グループは manifest 3 箇所の
  相互一致だけを見ており、Dockerfile は見ていないので、この PR 単体で ok のまま。
- `validate.py` の `check_inventory` は `kind` の値を検査しない（非空のみ）。
  `kind: npm` は新種だが問題なし。`kind`/`upstream` を parse するコードは
  リポジトリ内に無いことを grep で確認済み。
- 2 エントリとも `mirrors` を持たせていない（同じバージョンを書く別ファイルが無いため）。
  よって T-0051 の「mirrors を持つ target は check_version_sync.py の GROUPS にも
  対応エントリを持つこと」には該当しない。

次のセッションへの一言:

- **受入は 3/3 green。実装としてはこれで完結している。** 次のセッションがあるとしたら
  レビュー指摘の解消のはず。上の「罠」の節を先に読めば、なぜこの値なのかは全部たどれる。
- レビューで「digest が古い/違う」と言われたら、まず上の HEAD コマンドを叩き直すこと。
  Docker Hub の `22-alpine` タグは動くので、日が変われば index digest も変わりうる。
  ただし**追随して上げる必要は無い** — この PR の目的は「今動いているものに固定する」で、
  稼働中の Pod は node v22.23.2 だから、それを指す `sha256:c610fcdf…` が正しい。
- merge 後に image digest を manifest 3 箇所へ反映する別 PR が要る。手順は Dockerfile
  冒頭に書いた。**それはこのプロジェクトの範囲外**（PROJECT.md「やらないこと」）。

## 発見（仕様外。後で curriculum が拾う）

- **`ops/check_autopilot_image_pin.py` が孤児のまま。** `images/autopilot/` を変えたのに
  manifest の digest が更新されていなければ落とす、という意図のスクリプトが存在するが、
  `.github/workflows/` のどこからも呼ばれていない（grep で確認）。今回の PR はまさに
  「Dockerfile を変えて digest は次の PR」という状態を作るので、この配線が入るときは
  「同一 PR で揃える」ではなく「merge 後に別 PR で追う」運用と整合する形にする必要がある。
  P-0003 の題目とされているので、その前提として記録しておく。
- **`ops/memory/substrate.md` の「claude-code (npm pin 無し)」記述が本 PR で古くなる。**
  `ops/memory/` の書き手は consolidation の PR のみ（`ops/memory/README.md`）なので
  ここでは触っていない。次の consolidation で拾われる想定。
- **Dockerfile に残っている他の非 pin / 非監視の依存。** `KUBECTL_VERSION=v1.35.0`
  （pin されているが inventory 未登録）と apk パッケージ群（バージョン指定なし＝
  ビルドのたびに変わる）。「自分の土台を監視する」という本プロジェクトの論点の
  続きだが、1 PR 1 論点として分離した。
