# クラスタ内で常駐する autopilot のイメージ。
#
# クラウドのサンドボックス（claude.ai の定期実行）は homelab に到達できず、起動のたびに
# プロビジョニングのオーバーヘッドがかかり、実行中かどうかも外から分からなかった。
# このイメージはクラスタ内で動くので、k8s / Proxmox / B2 に直接届く。
#
# 入れる道具は「agent が homelab を操作するのに実際に要るもの」だけに絞る。
# 増やすときは、なぜ要るかをコメントに書くこと。
FROM node:22-alpine

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
RUN apk add --no-cache \
      git openssh-client ca-certificates python3 py3-yaml curl bash restic tzdata \
      chromium font-noto font-noto-cjk \
 && npm install -g @anthropic-ai/claude-code \
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
