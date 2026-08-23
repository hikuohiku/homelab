# heart-bus-sidecar のイメージ。heart Pod に同居する 1 バイナリだけが入る。
#
# autopilot-core のイメージに相乗りさせなかったのは、heart を core の都合で
# 再起動させないため。core-driver や telegram-adapter を直すたびに digest が動くと、
# 同じ digest を共有する heart も入れ替わる。停止経路を運ぶ側は、他の都合で
# 揺らさない。

# golang:1.25-alpine
FROM golang@sha256:1ae0735f00daffa3aaf1363a5184c0d2dc55c78e3db4ec70241cdac97bf84b59 AS build
WORKDIR /src
COPY go.mod go.sum ./
COPY *.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/heart-bus-sidecar .

# gcr.io/distroless/static-debian12:nonroot — シェルもパッケージマネージャも無い。
# 書く先は共有ボリュームだけなので readOnlyRootFilesystem で動く。
# digest は telegram-adapter の Dockerfile と同じ値。機械検査は無いので、
# base を上げるときは両方を同じ手順 (registry API の docker-content-digest) で揃える
FROM gcr.io/distroless/static-debian12@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab
COPY --from=build /out/heart-bus-sidecar /heart-bus-sidecar
# Pod 側の securityContext (runAsUser 10001) が上書きするが、素で走らせても
# root にならないようにしておく
USER 65532:65532
ENTRYPOINT ["/heart-bus-sidecar"]
