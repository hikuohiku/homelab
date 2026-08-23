# telegram-adapter — 標準ライブラリのみの単一バイナリ。
# 実行段は distroless/static (シェルもパッケージマネージャも無い)。TLS 検証に必要な
# ca-certificates は static イメージに同梱されている。
FROM golang:1.25-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY *.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/telegram-adapter .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/telegram-adapter /telegram-adapter
USER 65532:65532
ENTRYPOINT ["/telegram-adapter"]
