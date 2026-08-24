"""heart の心拍鮮度判定。

heart 本体 (heart.py) の beat() は gitutil.sync_main() など git のサブプロセス呼び出しで
毎回落ちる壊れ方 (2026-08-12 の fork 不能事故) をすると write_heartbeat() まで到達できず、
heartbeat.json の "at" が更新されなくなる。beat() 自体は run() の try/except に包まれて
例外を握りつぶすため、heart は死なずに回り続け、外部からは "生きているが仕事をしていない"
状態に見える。ここでの判定が唯一、その状態を検知して kubelet に再起動させる経路になる
(P-0065)。

判定はすべて純関数。kubelet の exec probe (heart-deployment.yaml) と単体テストの両方が
check() / is_stale() を呼ぶ — しきい値の数値はこのファイルの外に持ち出さない。
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .statefiles import parse_iso

# ビート間隔ちょうどで stale 判定すると、git fetch/push がネットワーク事情で伸びただけの
# 正常系まで再起動に倒れる (誤検知)。倍率・余白の具体的な数値に実測の裏付けは無い —
# 安全側に振った初期値 (heart-deployment.yaml の probe コメントにも明記)。
STALE_MULTIPLIER = 5
STALE_MARGIN_SECONDS = 60


def max_age_seconds(beat_seconds):
    return beat_seconds * STALE_MULTIPLIER + STALE_MARGIN_SECONDS


def is_stale(heartbeat_at, now, max_age):
    """heartbeat_at (ISO8601 文字列) が now から見て max_age 秒より古いか。"""
    age = (now - parse_iso(heartbeat_at)).total_seconds()
    return age > max_age


def heartbeat_path(data_dir):
    """heartbeat.json の場所。新しい置き場 (PVC の state/) を優先する。

    4b-2b で ops-state の checkout から state/ へ移した。**移行前の置き場も見る**
    — 新しいイメージの最初のビートが済むまではまだ古い方にしかなく、そこで
    「無い = stale」に倒すと起動直後に再起動を繰り返す。
    """
    new = data_dir / "state" / "heartbeat.json"
    if new.exists():
        return new
    return data_dir / "ops-state" / "heartbeat.json"


def check(data_dir=None, beat_seconds=None, now=None):
    """heartbeat.json を読んで新鮮なら True。無い/壊れているは stale (False) 扱いにする。

    「無い」を「元気」と誤読しない — PVC がまだ空の起動直後の一瞬を除けば、
    heartbeat.json が無いのは異常のはずだから。
    """
    data_dir = Path(data_dir or os.environ.get("HEART_DATA_DIR", "/data"))
    beat_seconds = int(beat_seconds or os.environ.get("HEART_BEAT_SECONDS", "120"))
    now = now or datetime.now(timezone.utc)
    try:
        with open(heartbeat_path(data_dir)) as f:
            doc = json.load(f)
        at = doc["at"]
    except (OSError, ValueError, KeyError):
        return False
    return not is_stale(at, now, max_age_seconds(beat_seconds))


def main():
    sys.exit(0 if check() else 1)


if __name__ == "__main__":
    main()
