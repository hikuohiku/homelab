# autopilot-core の副資材イメージ。コア本体 (opencode) はこれに入っていない。
#
# 入っているのは 2 つのバイナリだけ:
#
#   core-driver      — イベントを常駐セッションへ渡す係 (このディレクトリの Go)
#   telegram-adapter — MCP 返信ツール。opencode が子プロセスとして起動する
#
# telegram-adapter を「イメージ間 COPY」ではなくソースから焼くのは、adapter 側の
# digest をここに書くと二重管理になるため。ビルドコンテキストを apps/ に取ることで
# 両方のソースに届く (build-autopilot-core-image.yml の context を参照)。
#
# 実行段が alpine で distroless でないのは、initContainer で `cp` を使って
# opencode コンテナへ MCP バイナリを渡す必要があるため (シェルが要る)。

# golang:1.25-alpine
FROM golang@sha256:1ae0735f00daffa3aaf1363a5184c0d2dc55c78e3db4ec70241cdac97bf84b59 AS build
WORKDIR /src

COPY autopilot-core/app/go.mod autopilot-core/app/go.sum ./core/
COPY autopilot-core/app/*.go ./core/
RUN cd core && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/core-driver .

COPY telegram-adapter/app/go.mod telegram-adapter/app/go.sum ./adapter/
COPY telegram-adapter/app/*.go ./adapter/
RUN cd adapter && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/telegram-adapter .

# alpine:3.24
FROM alpine@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b
# git / ca-certificates は driver が main の作業コピーを PVC 上に保つために使う
# (repo.go)。コアはその作業コピーを read-only で読むだけで、git 自体は触らない
RUN apk add --no-cache git ca-certificates \
 && adduser -D -u 10001 core
COPY --from=build /out/core-driver /usr/local/bin/core-driver
COPY --from=build /out/telegram-adapter /usr/local/bin/telegram-adapter
USER 10001
ENTRYPOINT ["/usr/local/bin/core-driver"]
