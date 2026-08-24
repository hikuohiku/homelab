#!/usr/bin/env python3
"""pytest 互換の最小 shim (P-9034)。

spec P-9034 の受入検証が `python3 -m pytest ops/tests/test_reachability_probe.py -q`
を要求するが、このリポジトリの実行環境 (wrapper の verify / CI の unit tests) には
pytest モジュールが入っていない (`No module named pytest`。pip も無い)。そこで
`python3 -m pytest` を実行可能にする最小の compat レイヤをリポジトリルートに置く。

配置をルートにした理由: `python3 -m pytest` は sys.path[0] (= 実行 cwd) から `pytest`
モジュールを探す。spec の verify はリポジトリルートから実行される。

挙動:
- 本物の pytest が import できる環境ではそれに委譲する。`python -m` は cwd を sys.path
  先頭に足すため、このファイルが本物を影にしないよう自分 (ルート) を sys.path から
  除いてから再 import する
- 無い環境では unittest の収集機構で代行する。このリポジトリのテストは全て
  unittest.TestCase ベース (CI も `python3 -m unittest discover` で同じ収集経路) なので、
  pytest が無くても検証内容は変わらない

shim が扱う引数は verify が使う範囲に絞る: パス (ファイル / ディレクトリ) と -q / -v。
それ以外の pytest オプション (マーカー・プラグイン等) は shim の対象外。
"""

from __future__ import annotations

import os
import sys


def _forward_to_real_pytest() -> int | None:
    """本物の pytest が import できたら委譲する。無ければ None (fallback へ)。"""
    here = os.path.dirname(os.path.abspath(__file__))
    others = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != here]
    if not others:
        return None
    saved = sys.path[:]
    sys.path[:] = others
    try:
        import pytest as real
    except Exception:
        return None
    finally:
        sys.path[:] = saved
    if os.path.abspath(getattr(real, "__file__", "")) == os.path.abspath(__file__):
        return None
    run = getattr(real, "console_main", None) or getattr(real, "main", None)
    if run is None:
        return None
    return run()


def _dotted_name(path: str) -> str | None:
    """cwd 相対の .py パスを import 名に変換する (例: ops/tests/x.py -> ops.tests.x)。"""
    rel = os.path.relpath(path, os.getcwd())
    if rel.startswith("..") or not rel.endswith(".py"):
        return None
    return rel[:-3].replace(os.sep, ".")


def _fallback(argv: list[str]) -> int:
    import importlib.util
    import time
    import unittest

    paths = [a for a in argv if not a.startswith("-")] or ["."]
    verbosity = 2 if "-v" in argv else (0 if "-q" in argv else 1)

    if os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())

    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite()
    for p in paths:
        if os.path.isdir(p):
            suite.addTests(loader.discover(start_dir=p, pattern="test_*.py",
                                           top_level_dir="."))
            continue
        if not os.path.isfile(p):
            print(f"pytest (shim): 対象が存在しない: {p}", file=sys.stderr)
            return 4
        dotted = _dotted_name(p)
        if dotted:
            try:
                suite.addTests(loader.loadTestsFromName(dotted))
                continue
            except Exception:
                pass
        mod_name = "_pytest_shim_target"
        spec = importlib.util.spec_from_file_location(mod_name, p)
        if spec is None or spec.loader is None:
            print(f"pytest (shim): 読み込めない: {p}", file=sys.stderr)
            return 4
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        suite.addTests(loader.loadTestsFromModule(mod))

    start = time.monotonic()
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    elapsed = time.monotonic() - start
    total = result.testsRun
    if result.wasSuccessful():
        print(f"{total} passed in {elapsed:.2f}s")
        return 0
    print(f"{len(result.failures) + len(result.errors)} failed, {total} tests in {elapsed:.2f}s")
    return 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    forwarded = _forward_to_real_pytest()
    if forwarded is not None:
        return forwarded
    return _fallback(argv)


if __name__ == "__main__":
    sys.exit(main())