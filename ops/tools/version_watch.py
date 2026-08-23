#!/usr/bin/env python3
"""ops/inventory.json の全 target の上流最新版を見に行き、drift を一覧する (P-0126)。

なぜ要るか: inventory の上流追従は 2026-08-06 以降誰もやっていない (#49 の構造原因
「人間も器も上流を見ていない」が続行中)。既存の ops/check_version_sync.py は
manifest↔inventory の内向き整合しか見ない。このモジュールは上流→inventory の
外向き計器で、観測と記録のみを行う (更新 PR は出さない — 次のプロジェクトの仕事)。

判定の考え方:

- upstream の scheme 接頭辞 (github: / dockerhub: / npm:) ごとに最新版の取り方を替える。
  github は Releases API の latest (prerelease/draft を除いた安定版)、dockerhub は
  Hub API のタグ一覧、npm は dist-tags.latest。
- 比較は「数字列を抜き出したタプル」(core) 同士で行う。`v` 接頭子・`argo-cd-` のような
  リリース名接頭子・`-alpine` のような派生タグの揺れに引きずられないため。
  片方の core が他方の接頭辞になっている場合は同値とみなす (major 系だけを pin して
 いる target `v7` が、上流 `v7.0.5` に対して永遠に drift 扱いされないように)。
- dockerhub では current と同じ variant (`-alpine` 等) のタグだけを候補にする。
  plain タグで運用している対象が alpine 版の新番に引きずられて誤報するのを防ぐ。
  variant は最後の `-` 以降が英字を含むときだけそれとみなす (16.9-0.4.3 の
  ような数値結合タグを壊さないため)。
- current が版数でないもの (digest pin「sha256:...」「sha256 digest pin」「flake.lock の
  rev」等、x.y 形式を含まない文字列) は status=uncomparable として素通しし、偽の
  drift を作らない。digest pin は意図的な固定なので「上がっているか」自体が論点外。

fail の扱い: 個別 target のネットワーク失敗・404・未知 scheme は result 内の
status=error として記録し、全体は止めない (部分的な観測も観測)。inventory 自体が
読めないときだけ rc!=0。「エラー 0 件」を見せかけて沈黙しない。

既知の死角 (伏せずに書き残る):
  - Releases の latest を持たない repo (安定リリースを切っていない) は 404 に、
    release_prefix 指定対象は先頭 100 件に一致が無いと None になり error 記録
    になる。tags 直参照での補完はやっていない
  - dockerhub は「current の数字頭部で絞った家族ページ」と「絞り込み無しの最近更新
    順ページ」の 2 リクエスト。pin 自身の家族の patch 追従と、push されて間もない
    新系列は確実に見えるが、push から時間が経ちすぎて全体ページの 100 件から
    漏れた minor/major 更新は取りこぼしうる
  - 上流が current より古い安定版を latest にした場合も drift 扱いになる
    (方向付き比較はしない。上げ下げどちらでも「動いた」ことだけを計器に載せる)

判定ロジックの固定テストは ops/tests/test_version_watch.py
(`python3 -m unittest ops.tests.test_version_watch`)。HTTP 層は注入可能で、
テストはレスポンス JSON の fixture だけで通る (ネットワークなし)。

このモジュールは apps/version-watcher/version_watch.py に**手動同期コピー**されている
(kustomize の configMapGenerator が kustomization.yaml の外のファイルを読めないため)。
ロジックを変えたらコピーへの反映を忘れないこと — コピー側には単体テストが無い。
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "ops" / "inventory.json"

# inventory.json 実測 (P-0126 initializer, 2026-08-23) の scheme 種別。
# ここに無い scheme が inventory に増えたら check_all が error 記録を出すので
# 「黙って見逃す」ことはない。対応を追加したらこの行も広げる
SUPPORTED_SCHEMES = ("github:", "dockerhub:", "npm:")

_DIGITS = re.compile(r"\d+")
# current が「版数っぽい」かの判定用。x.y のドット区切り数字、または
# major 系だけの pin ("v7") を許す。sha256 hex blob や日本語プレースホルダは
# どちらにも掛からない
_DOTTED_VERSION = re.compile(r"\d+\.\d+")
_MAJOR_ONLY = re.compile(r"v?\d+")


def parse_core(s):
    """文字列から数字の並びを全部抜いて int タプルにする。

    "v1.98.9" -> (1, 98, 9) / "argo-cd-9.1.7" -> (9, 1, 7) /
    "1.37.1-alpine" -> (1, 37, 1) / "16.9-0.4.3" -> (16, 9, 0, 4, 3)
    """
    return tuple(int(m) for m in _DIGITS.findall(s))


def variant_of(tag):
    """タグの派生種 (-alpine 等) を返す。無ければ None。

    最後の '-' 以降に英字を含むときだけ variant とみなす。"1.37.1-alpine" ->
    "alpine"、"16.9-0.4.3" -> None ("-0.4.3" は英字を含まない)、"17.10" -> None。
    """
    if "-" not in tag:
        return None
    tail = tag.rsplit("-", 1)[1]
    if re.search(r"[A-Za-z]", tail):
        return tail
    return None


def cores_equal(a, b):
    """core 同士が同値か。片方が他方の接頭辞の場合も同値とみなす。

    (7,) == (7, 0, 5): actions/checkout のような major 系 pin (current "v7") が、
    上流の patch 番号の存在だけで永遠に drift 報告されるのを防ぐ。
    一方 (3, 21, 3) != (3, 22, 0) は普通に drift になる。
    """
    n = min(len(a), len(b))
    return a[:n] == b[:n]


def is_comparable_current(current):
    """current の文字列が版数比較に耐えるか。

    比較できないもの: digest pin ("sha256:c610..." / "sha256 digest pin")、
    プレースホルダ ("flake.lock の rev")。major 系だけの pin ("v7") は
    cores_equal の接頭辞規則で比較できるので対象に含める。
    """
    return bool(_DOTTED_VERSION.search(current) or _MAJOR_ONLY.fullmatch(current.strip()))


def strip_version_prefixes(tag, release_prefix=None):
    """リリース名接頭子 (release_prefix) と v 接頭子を剥がす。"""
    if release_prefix and tag.startswith(release_prefix):
        tag = tag[len(release_prefix):]
    if tag.startswith("v") and tag[1:2].isdigit():
        tag = tag[1:]
    return tag


def http_get(url, timeout=30):
    """(status, body_bytes) を返す。urllib の HTTPError もステータスに変換する。

    この関数が本物のネットワークを叩く唯一の場所。テストでは fetch 可能オブジェクト
    に差し替えられるよう、呼び出し側は常に (status, body) の契約に依存する
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "homelab-version-watcher",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def github_latest(fetch, repo, release_prefix=None):
    """GitHub Releases の安定版のうち最新のタグを剥がして返す。

    release_prefix が無い対象は Releases API の latest (prerelease/draft を除いた
    安定版) をそのまま使う。ある対象は「prefix 一致の安定リリースのうち最初のもの」
    を使う。1 repo に複数チャートのリリースが混在する argo-helm では repo 全体の
    latest が別チャートのものになり、接頭辞を後から剥がしても比較にならないため
    (2026-08-23 実測: argocd-chart の latest 欄に argo-workflows-2.0.2 が載った)。
    安定リリースが 1 つも無い repo、prefix 一致が先頭 100 件に見つからない repo は
    404/None -> None (呼び出し側で error 記録)。
    """
    if release_prefix:
        # /releases は新しい順。prerelease は一覧に含まれるので自分で除く
        # (draft は未認証では返らない)
        status, body_bytes = fetch(
            "https://api.github.com/repos/{}/releases?per_page=100".format(repo)
        )
        if status == 404:
            return None
        if status != 200:
            raise RuntimeError("GitHub API が {} を返した ({})".format(status, repo))
        for rel in json.loads(body_bytes):
            if rel.get("draft") or rel.get("prerelease"):
                continue
            tag = rel.get("tag_name", "")
            if tag.startswith(release_prefix):
                return strip_version_prefixes(tag, release_prefix)
        return None

    status, body_bytes = fetch(
        "https://api.github.com/repos/{}/releases/latest".format(repo)
    )
    if status == 404:
        return None
    if status != 200:
        raise RuntimeError("GitHub API が {} を返した ({})".format(status, repo))
    tag = json.loads(body_bytes)["tag_name"]
    return strip_version_prefixes(tag, release_prefix)


def hub_tags_url(path, name=None):
    """Docker Hub タグ一覧の URL。name を付けるとタグ名の部分一致でサーバ側絞り込み。

    タグ総数が数千あるイメージ (library/python は実測 3911) でも目的の家族だけを
    1 ページ (100 件) で受け取れるようにするための絞り込み。
    """
    url = (
        "https://hub.docker.com/v2/repositories/{}/tags"
        "?page_size=100&ordering=-last_updated".format(path)
    )
    if name:
        url += "&name={}".format(urllib.parse.quote(name, safe=""))
    return url


def numeric_head(tag):
    """タグ先頭の連続する数字とドットを抜き出す。"3.14-alpine" -> "3.14" /
    "17.10" -> "17.10" / "9.1.1-alpine" -> "9.1.1"。数字で始まらないタグは ""。"""
    m = re.match(r"\d[\d.]*", tag)
    return m.group(0).rstrip(".") if m else ""


def dockerhub_latest(fetch, path, want_variant, current):
    """Docker Hub のタグ一覧から current と同じ variant の最新を返す。

    path は upstream 接頭辞を剥がしたレジストリパス ("library/postgres" 等 —
    target.name は表示名でレジストリパスと一致しない ("busybox (initContainer)") ので
    upstream から取る)。current は比較基準になる現在の pin。見つからなければ None。

    単純な「最近更新順の先頭 100 件」では足りない。2026-08-23 の初回実測で、
    library/python (3911 タグ) 等では alpine 家族が 100 件にほぼ入らず、古代タグ
    ("3.6.0a4-alpine") が最大 core を取って「3.14-alpine -> 3.6.0a4-alpine」という
    偽 drift を報告することが分かった。そこで 2 ページ構成にした:

    - 家族アンカー: numeric_head(current) で name 絞り込みした頁から、さらに
      「head で始まる」候補だけを見る。pin 自身の家族は必ずここに現れるので下限が
      保証され、古い系列への誤降下が起きない (部分一致の "19.1" が head "9.1" に
      引っかかる事故は startswith で弾く)
    - 全体ページ: 絞り込み無しの最近更新順。新系列 (minor/major 更新) は push された
      直後なら必ずここに現れるので、家族アンカーが原理的に見えない線の更新を拾う

    どちらの頁でも「数字で始まり数字を含む・variant 一致」の候補に絞って最大 core を
    取る。数字始まり制限は "buildroot-2014.02" 型の別系統タグが (2014, 2) という巨大
    core で全体を汚染するのを防ぐ (実測で busybox がこれで誤報した)。両頁の結果を
    比べ大きい方を返す — 全体ページが勝ったときだけ「新系列あり」という意味になる。
    """
    def best_from(names):
        best, best_core = None, ()
        for name in names:
            if not name[:1].isdigit():
                continue
            core = parse_core(name)
            if not core:
                continue
            if variant_of(name) != want_variant:
                continue
            if best is None or core > best_core:
                best, best_core = name, core
        return best

    def fetch_names(url):
        status, body_bytes = fetch(url)
        if status != 200:
            raise RuntimeError("Docker Hub API が {} を返した ({})".format(status, path))
        return [r.get("name", "") for r in json.loads(body_bytes).get("results", [])]

    head = numeric_head(current)
    anchor_best = None
    if head:
        anchor_best = best_from(
            n for n in fetch_names(hub_tags_url(path, head)) if n.startswith(head)
        )
    global_best = best_from(fetch_names(hub_tags_url(path)))

    if anchor_best is None:
        return global_best
    if global_best is None:
        return anchor_best
    return global_best if parse_core(global_best) > parse_core(anchor_best) else anchor_best


def npm_latest(fetch, package):
    """npm レジストリの dist-tags.latest を返す。"""
    status, body = fetch(
        "https://registry.npmjs.org/{}/latest".format(urllib.parse.quote(package, safe="@/"))
    )
    if status != 200:
        raise RuntimeError("npm registry が {} を返した ({})".format(status, package))
    return json.loads(body)["version"]


def _scheme_of(upstream):
    for scheme in SUPPORTED_SCHEMES:
        if upstream.startswith(scheme):
            return scheme
    return None


def check_target(target, fetch):
    """target 1 件の上流観測結果を dict で返す。

    status: ok (比較できた。drifted が効力を持つ) / uncomparable (current が版数で
    ないため比較対象外) / error (取得失敗。error フィールドに理由)
    """
    base = {
        "id": target.get("id"),
        "kind": target.get("kind"),
        "upstream": target.get("upstream"),
        "current": target.get("current"),
    }
    current = target.get("current") or ""
    upstream = target.get("upstream") or ""

    if not is_comparable_current(current):
        return dict(
            base,
            status="uncomparable",
            reason="current が x.y 形式の版数を含まない (digest pin / プレースホルダ)",
        )

    scheme = _scheme_of(upstream)
    try:
        if scheme == "github:":
            latest_raw = github_latest(
                fetch,
                upstream[len("github:"):],
                target.get("release_prefix"),
            )
            if latest_raw is None:
                return dict(base, status="error", error="安定リリースが 1 つも無い (404)")
        elif scheme == "dockerhub:":
            latest_raw = dockerhub_latest(
                fetch,
                upstream[len("dockerhub:"):],
                variant_of(current),
                current,
            )
            if latest_raw is None:
                return dict(base, status="error", error="同一 variant の版数タグが見つからなかった")
        elif scheme == "npm:":
            latest_raw = npm_latest(fetch, upstream[len("npm:"):])
        else:
            return dict(base, status="error", error="未知の upstream scheme: {}".format(upstream))
    except Exception as e:  # noqa: BLE001 — 1 target の失敗で全体を止めない
        return dict(base, status="error", error="{}: {}".format(type(e).__name__, e))

    drifted = not cores_equal(parse_core(current), parse_core(latest_raw))
    return dict(base, status="ok", latest=latest_raw, drifted=drifted)


def check_all(targets, fetch):
    """全 target を観測して結果のリストを返す。順序は inventory の並びを保つ。"""
    return [check_target(t, fetch) for t in targets]


def load_inventory(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    targets = data.get("targets")
    if not isinstance(targets, list) or not targets:
        raise RuntimeError("inventory に targets が無いか空 (path={})".format(path))
    return targets


def summarize(results):
    """人間 / briefing 用の集計。"""
    return {
        "total": len(results),
        "ok": sum(1 for r in results if r["status"] == "ok"),
        "drifted": sum(1 for r in results if r.get("drifted")),
        "errors": sum(1 for r in results if r["status"] == "error"),
        "uncomparable": sum(1 for r in results if r["status"] == "uncomparable"),
    }


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    inventory_path = argv[0] if argv else str(INVENTORY)
    try:
        targets = load_inventory(inventory_path)
    except (OSError, ValueError, RuntimeError) as e:
        print("inventory を読めなかった: {}".format(e), file=sys.stderr)
        return 1

    results = check_all(targets, http_get)
    summary = summarize(results)
    print(json.dumps({"summary": summary, "targets": results}, ensure_ascii=False, indent=2))
    # drift や個別 target の error は正常系の観測結果 (rc にはしない)。
    # 「全部取れた/一部壊れた」の違いは summary.errors と各要素の status で分かる
    return 0


if __name__ == "__main__":
    sys.exit(main())
