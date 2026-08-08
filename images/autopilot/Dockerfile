# クラスタ内で常駐する autopilot のイメージ。
#
# クラウドのサンドボックス（claude.ai の定期実行）は homelab に到達できず、起動のたびに
# プロビジョニングのオーバーヘッドがかかり、実行中かどうかも外から分からなかった。
# このイメージはクラスタ内で動くので、k8s / Proxmox / B2 に直接届く。
#
# 入れる道具は「agent が homelab を操作するのに実際に要るもの」だけに絞る。
# 増やすときは、なぜ要るかをコメントに書くこと。
#
# ── バージョンの固定について（P-0004）─────────────────────────────────
# ベースイメージ（node）と claude-code CLI は、このエージェントの「土台」と「思考エンジン」
# そのもの。タグ参照 / pin 無しのままだと、再ビルドのたびに誰の判断も経ずに中身が入れ替わり、
# 壊れたときに「いつから壊れたか」を後から特定できない。だから両方を固定し、
# ops/inventory.json（autopilot-base-image / claude-code-cli）で監視対象に入れている。
# 上げるときは inventory の policy: manual に従い、必ず人間の確認を通すこと。
#
# ── 更新したあとにやること（イメージ digest の反映は別 PR）───────────────
# .github/workflows/build-autopilot-image.yml は main への push で ghcr.io に push する
# だけで、manifest 側の digest は更新しない。この Dockerfile を変えた PR が merge されて
# イメージが再ビルドされた「後」に、新しい digest を manifest へ反映する別 PR が要る。
#
#   1. merge 後に走った build-autopilot-image.yml の run を確認する
#      （Actions ログの `::notice title=autopilot image::` に push したタグが出る）
#   2. そのタグ（main の merge commit SHA）の index digest を ghcr.io から実測する:
#        SHA=<main の merge commit SHA>
#        TOK=$(curl -fsS "https://ghcr.io/token?scope=repository:hikuohiku/homelab-autopilot:pull" \
#              | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
#        curl -fsSI -H "Authorization: Bearer $TOK" \
#          -H "Accept: application/vnd.oci.image.index.v1+json" \
#          "https://ghcr.io/v2/hikuohiku/homelab-autopilot/manifests/$SHA" \
#          | grep -i docker-content-digest
#   3. 得た digest を次の 3 箇所すべてに反映する（ops/check_version_sync.py の
#      "autopilot image digest" グループが一致を検査する。1 箇所でも漏らすと CI が落ちる）:
#        - apps/autopilot/deployment.yaml         の image
#        - apps/autopilot/heart-deployment.yaml   の image
#        - apps/autopilot/heart-deployment.yaml   の RUNNER_IMAGE env
#
# ベースイメージは digest 参照で固定する（タグは人間が読むために残す）。
# 必ず index（マルチアーキ）の digest を使うこと。単一アーキの manifest digest を書くと
# GitHub Actions runner のアーキが変わった時点でビルドが壊れる。
# 2026-08-08 実測: node:22-alpine の index digest = 下記。中身は NODE_VERSION=22.23.2 /
# Alpine 3.24.1 で、稼働中の Pod（node v22.23.2 / Alpine 3.24.1）と一致する。
FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32

# git         : リポジトリの取得と push
# openssh-client / ca-certificates : TLS と一部の git 操作
# python3 / py3-yaml : ops/validate.py・ops/dashboard/build.py の実行に必須（標準ライブラリのみで動く方針）
# curl        : 外部 API の疎通確認
# bash        : ループスクリプト
# restic      : バックアップの検証（クラスタ内からしかできない）
# tzdata      : ログの時刻を読める形にする
# chromium    : レビュー役（利用者視点）がダッシュボードを実際に描画して見るため。
#               「人間として使って詰まる」ことを求めているのに見る手段が無いと、
#               マークアップを読んで想像するしかなくなり、レンズの意味が消える
# font-noto-cjk / font-noto : 日本語が描画されないと画面を見た意味が無い
#               （フォント不在だと文字が一切出ないまま描画は成功する。実測済み）
#
# claude-code は自分の思考エンジンなので、明示バージョンで固定する。
# 2026-08-08 実測: 稼働中の Pod は 2.1.223（`claude --version` /
# `npm ls -g --depth=0` の @anthropic-ai/claude-code@2.1.223）。挙動を変えないため、
# その実測値でそのまま固定する（上げるのは別の論点）。
RUN apk add --no-cache \
      git openssh-client ca-certificates python3 py3-yaml curl bash restic tzdata \
      chromium font-noto font-noto-cjk \
 && npm install -g @anthropic-ai/claude-code@2.1.223 \
 && npm cache clean --force

# kubectl は apk に無いので公式バイナリを取る。バージョンは k3s 側（1.35 系）に合わせる。
ARG KUBECTL_VERSION=v1.35.0
RUN ARCH="$(uname -m | sed 's/x86_64/amd64/; s/aarch64/arm64/')" \
 && curl -fsSLo /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl" \
 && chmod +x /usr/local/bin/kubectl \
 && kubectl version --client=true

# 非 root で動かす。npm と git がホームに書ける必要があるので HOME を用意する。
RUN adduser -D -u 10001 -h /home/autopilot autopilot \
 && mkdir -p /work && chown -R 10001:10001 /work /home/autopilot

USER 10001
ENV HOME=/home/autopilot
WORKDIR /work

# 実際の起動コマンドは k8s 側の ConfigMap（ループスクリプト）が渡す。
# イメージに振る舞いを焼き込まないことで、挙動の変更を Git だけで完結させる。
CMD ["/bin/bash", "-c", "echo 'autopilot image: no command given'; sleep 3600"]
