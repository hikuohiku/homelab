# telegram-adapter — 標準ライブラリのみの単一バイナリ。
# 実行段は distroless/static (シェルもパッケージマネージャも無い)。TLS 検証に必要な
# ca-certificates は static イメージに同梱されている。
#
# base image は OCI index digest で pin する (apps/ops-dashboard/app/Dockerfile と同じ)。
# 更新するときは registry API の docker-content-digest ヘッダを読んで差し替えること。
# builder のタグは go.mod の go ディレクティブと揃える。
#
# 依存: nats.go / nkeys (イベント経路。ops/inventory.json に登録済み)。
# go.sum を COPY しないと再現ビルドにならないので必ず一緒に入れること。

# golang:1.25-alpine
FROM golang@sha256:1ae0735f00daffa3aaf1363a5184c0d2dc55c78e3db4ec70241cdac97bf84b59 AS build
WORKDIR /src
COPY go.mod go.sum ./
COPY *.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/telegram-adapter .

# gcr.io/distroless/static-debian12:nonroot
FROM gcr.io/distroless/static-debian12@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab
COPY --from=build /out/telegram-adapter /telegram-adapter
USER 65532:65532
ENTRYPOINT ["/telegram-adapter"]
