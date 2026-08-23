# coder v2.35.3 → v2.35.4 (stable) リリースノート読了の証跡

- 読了日: 2026-08-23 (P-0145 worker セッション)
- 現在版: ghcr.io/coder/coder:v2.35.3 (`apps/coder/deployment.yaml`)
- 目標版: ghcr.io/coder/coder:v2.35.4
- 情報源: GitHub Releases API (`https://api.github.com/repos/coder/coder/releases/tags/v2.35.4`, HTTP 200, 当日実測)
- リリース URL: https://github.com/coder/coder/releases/tag/v2.35.4
- published_at: 2026-08-10T08:29:43Z / prerelease: false

## チャンネル判定 (note の除外規定「stable のみ・mainline v2.36.x は避ける」)

- **v2.35.4 の本文冒頭は `> ## Stable (since August 10, 2026)`** — stable チャンネルのリリースであることを
  本文自身が宣言している (P-0029 が v2.35.3 の channel 判定に使ったのと同じ手法)。
- 上流には v2.36.0 (2026-08-04) / v2.36.1 (2026-08-20) があるが、これは mainline 系なので触らない。
  v2.35.4 は stable 系で v2.35.3 より新しい唯一のタグ (ホップ数 1)。
- coder は autopilot 自身の開発環境をホストする対象のため、足場を消さないよう mainline への
  跳躍はしない (inventory note / CHARTER §4)。

## 原文 (release body 全文)

> > ## Stable (since August 10, 2026)
>
> ## Changelog
>
> ### Security patches
>
> - Server: Reject workspace proxy hostname prefixes (https://github.com/coder/coder/pull/27544, [d547bea24a](https://github.com/coder/coder/commit/d547bea24a)) (@geokat) ([GHSA-h58h-qvv5-xvwg](https://github.com/coder/coder/security/advisories/GHSA-h58h-qvv5-xvwg))
>
> ### Bug fixes
>
> - Remove 403 from key failover and cooldown on 401 (#27419, c9388a1afe) (@ssncferreira)
> - Dashboard: Backport admin settings dropdown visibility fix to release/2.35 (#27850, fe656fc94c)
> - Update google.golang.org/grpc to v1.82.1 (#27925, fd5efa57ff)
> - Update github.com/DataDog/dd-trace-go/v2 to v2.8.1 (#27938, 81add7e766)
>
> Compare: [`v2.35.3...v2.35.4`](https://github.com/coder/coder/compare/v2.35.3...v2.35.4)
>
> ## Container image
>
> - `docker pull ghcr.io/coder/coder:2.35.4`
>
> ## Install/upgrade
>
> Refer to our docs to [install](https://coder.com/docs/install) or [upgrade](https://coder.com/docs/install/upgrade) Coder, or use a release asset below.

## 判定材料になった箇所

- **Security patch 1 件 (GHSA-h58h-qvv5-xvwg)** — workspace proxy の hostname prefix を server 側で
  拒否する修正。この homelab は workspace proxy 機能を使っていない (deployment.yaml に proxy 関連
  設定なし、CODER_ACCESS_URL 直結) ため攻撃面には当たらないが、server 側の防御修正なので
  取り込む方向が安全。
- Bug fix 群: aibridge (AI gateway) の key failover/cooldown 改善、dashboard の admin settings
  dropdown 表示修正 (release/2.35 への backport)、grpc v1.82.1 / dd-trace-go v2.8.1 の依存更新。
  いずれもこの環境の使い方 (単一ユーザー・dex 未接続・GitHub OAuth 無効化済み) に対する
  breaking change は無い。**BREAKING CHANGES 節は無い** (mainline v2.36.0 にある節が stable
  backport には含まれていない)。
- **DB migration: 無し** — T-0023 と同じ file listing diff 手法を git partial clone で実測:
  `git diff --name-status v2.35.3 v2.35.4 -- coderd/database/migrations/` = 空 (0 ファイル)。
  diff 全体 (25 ファイル) も `migrat|schema` を含まず、aibridge / dashboard / go.mod 系のみ。
  PostgreSQL スキーマ不変 → 「コード revert とスキーマの非対称」問題は今回は発生しない。
- **イメージタグの実在形** — GHCR tags API (`ghcr.io/v2/coder/coder/tags/list`, token 付き, 当日実測):
  `v2.35.4` あり / `2.35.4` (v 無し) 無し。リリース本文の `docker pull ghcr.io/coder/coder:2.35.4`
  は v 無し表記だがレジストリには v 付きしか無いため、現在 pin (`v2.35.3`) と同じ v 付き形式を維持する。
