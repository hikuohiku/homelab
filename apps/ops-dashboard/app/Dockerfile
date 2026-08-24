# Initial publish flow:
# 1. Merge the application/workflow PR so build-dashboard-image.yml publishes the SHA tag.
# 2. Measure that tag's OCI index digest from GHCR.
# 3. Pin the measured digest in ../deployment.yaml in a follow-up PR.
FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS builder
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS runner
# git は入れない (設計 state-out-of-git 4b-2b)。読み先は Kubernetes API と
# heart の /healthz と PVC だけで、このイメージから git を打つ経路は無い
RUN addgroup -g 10001 dashboard \
 && adduser -D -u 10001 -G dashboard -h /home/dashboard dashboard
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000 \
    HOME=/home/dashboard
COPY --from=builder --chown=10001:10001 /app/public ./public
COPY --from=builder --chown=10001:10001 /app/.next/standalone ./
COPY --from=builder --chown=10001:10001 /app/.next/static ./.next/static
USER 10001
EXPOSE 3000
CMD ["node", "server.js"]
