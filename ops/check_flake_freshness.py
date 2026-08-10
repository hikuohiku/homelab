#!/usr/bin/env python3
"""
土台（NixOS の flake input = カーネル・k3s・systemd の供給元）が腐っていないか検証する。
CI (ops job) から実行する。古すぎる input があれば非 0 で終了する。

なぜ要るのか: inventory の 39 対象のうち 37 はコンテナと chart で、追従の目は入っている。
一方その全部が乗っている nixpkgs は policy=manual で、誰も上げなければ据え置かれる。
「pin は誰も上げなければ据え置かれる」（CLAUDE.md、vaultwarden 1.36.0 の放置で
クライアント同期が全停止した #49）はアプリ層で起きた事故で、同じ構造が土台層に残っていた。
腐敗を人間の記憶ではなく機械の目にする（P-0055）。

標準ライブラリのみ（ops/validate.py / ops/check_version_sync.py と同じ方針。実行環境に
何も入っていなくても動くこと。issue #56 2026-08-04 19:53:35 の指摘: pyyaml に依存すると
autopilot のサンドボックスで手元検証できなくなる）。

これは ops/CHARTER.md §4 の「縛る変更」（これまで無かった失敗条件の新設）である。
ci.yml の ops job は ruleset の必須チェックなので、ここが落ちると全 PR がマージ不能になり、
autopilot のループごと止まる。**落ちたときに次の起動がそのまま直せる形で落とすこと。**
ロールバックは ci.yml から当該ステップを消す revert 1 本。
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "nix" / "images" / "proxmox-cloud" / "flake.lock"
INVENTORY = ROOT / "ops" / "inventory.json"

# 閾値の根拠（rules.json ではなくここに置く。この検査以外に効かせる先が無いため）:
# nixos-unstable のチャンネル更新は数日〜十数日おきで、60 日はその何周期分にもあたる。
# 数サイクル分の遊びを残すのは、土台の更新が「flake 更新 → image build → node01 の
# 差し替え」という別経路で、アプリの tag 上げのように即日では回らないから。
# 一方でカーネルや k3s の修正が四半期（90 日）放置されるのは防ぎたい。60 日は
# 「1 回落としても次の起動で間に合う」上限として、その手前に置いている。
MAX_AGE_DAYS = 60
# 閾値に触ってから慌てないための予告。落とさない。
WARN_AGE_DAYS = 45

# flake.lock の rev と二重管理になっている inventory 側の対象。
# lock を更新して inventory を直し忘れたら、ここで落ちる（P-0055 DoD 4）。
INVENTORY_TARGET_ID = "nixpkgs"
LOCK_NODE_FOR_INVENTORY = "nixpkgs"

FIX_HINT = """直し方（この順に踏む。詳細は docs/os-updates.md）:
  1. .github/workflows/nixos-image.yml の update job を起動する。
     - main 以外のブランチで nix/images/proxmox-cloud/** に触って push する、または
     - curl -X POST -H "Authorization: Bearer $GITHUB_TOKEN" \\
         https://api.github.com/repos/hikuohiku/homelab/dispatches \\
         -d '{"event_type":"flake-update"}'
       （repository_dispatch は default ブランチの定義しか実行しない。workflow_dispatch は
        器の token では 403 で叩けない）
  2. その job のログの P0055_FLAKE_LOCK_BASE64_BEGIN / END に挟まれた 1 行を base64 -d して
     nix/images/proxmox-cloud/flake.lock に書き、commit する。
  3. 同じ PR で ops/inventory.json の nixpkgs の current / upstream_rev / last_checked を更新する。
  4. その push で同 workflow の build job が、commit した lock そのものをビルドする。
     ビルドが通ることまで確認してからマージする。"""


def emit(kind: str, message: str) -> None:
    """GitHub Actions ではアノテーションに、手元では素の行に出す（ops/validate.py と同じ）。"""
    if "GITHUB_ACTIONS" in os.environ:
        print(f"::{kind}::{message}")
    else:
        print(f"{kind}: {message}")


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_lock_freshness(lock: dict, now: float) -> list[str]:
    errors: list[str] = []
    checked = 0
    for name, node in sorted(lock.get("nodes", {}).items()):
        locked = node.get("locked")
        if not isinstance(locked, dict) or "lastModified" not in locked:
            # root ノードなど。locked を持たないものは検査対象外。
            continue
        checked += 1
        age = (now - locked["lastModified"]) / 86400
        where = f"{name} (rev={str(locked.get('rev'))[:12]}, {iso(locked['lastModified'])})"
        if age > MAX_AGE_DAYS:
            errors.append(
                f"flake.lock の input {where} が {age:.1f} 日前で、上限 {MAX_AGE_DAYS} 日を超えている"
            )
        elif age > WARN_AGE_DAYS:
            emit(
                "warning",
                f"flake.lock の input {where} が {age:.1f} 日前。"
                f"あと {MAX_AGE_DAYS - age:.1f} 日で CI が落ちる（上限 {MAX_AGE_DAYS} 日）",
            )
        else:
            print(f"ok: {name} = {age:.1f} 日前 (上限 {MAX_AGE_DAYS} 日)")
    if checked == 0:
        errors.append(
            f"{LOCK.relative_to(ROOT)}: lastModified を持つ input が 1 つも無い"
            "（lock の形が変わった可能性がある。この検査が空振りしている）"
        )
    return errors


def check_inventory_sync(lock: dict) -> list[str]:
    """inventory の nixpkgs エントリが、実際の lock の rev を指しているか。

    lock だけ更新して inventory を直し忘れると、autopilot が監視している「今の版」が
    実物とずれる。ずれた台帳は無いより悪い（T-0114 の検出網の穴と同じ構図）ので落とす。
    """
    errors: list[str] = []
    try:
        inventory = json.loads(INVENTORY.read_text())
    except Exception as e:
        return [f"{INVENTORY.relative_to(ROOT)} が読めない: {e}"]

    target = next(
        (t for t in inventory.get("targets", []) if t.get("id") == INVENTORY_TARGET_ID), None
    )
    if target is None:
        return [f"inventory.json に id={INVENTORY_TARGET_ID} の target が無い"]

    locked_rev = lock.get("nodes", {}).get(LOCK_NODE_FOR_INVENTORY, {}).get("locked", {}).get("rev")
    if not locked_rev:
        return [f"flake.lock の {LOCK_NODE_FOR_INVENTORY} に rev が無い"]

    if target.get("current") != locked_rev:
        errors.append(
            f"inventory.json の {INVENTORY_TARGET_ID}.current={target.get('current')!r} が "
            f"flake.lock の rev={locked_rev!r} と一致しない"
            "（lock を更新したら inventory も同じ PR で更新する）"
        )
    else:
        print(f"ok: inventory.json の {INVENTORY_TARGET_ID}.current = flake.lock の rev ({locked_rev[:12]})")

    # last_checked は「器が最後に上流を見た日」。壊れた値を置くと台帳が嘘をつくので形だけ検査する
    # （CHARTER §7.2: UTC の ISO8601）。
    last_checked = target.get("last_checked")
    if last_checked:
        try:
            datetime.strptime(last_checked, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            errors.append(
                f"inventory.json の {INVENTORY_TARGET_ID}.last_checked={last_checked!r} が "
                "UTC の ISO8601 (YYYY-MM-DDThh:mm:ssZ) ではない"
            )
    return errors


def main() -> int:
    try:
        lock = json.loads(LOCK.read_text())
    except Exception as e:
        emit("error", f"{LOCK.relative_to(ROOT)} が読めない: {e}")
        return 1

    now = time.time()
    errors = check_lock_freshness(lock, now) + check_inventory_sync(lock)

    for e in errors:
        emit("error", e)
    if errors:
        print()
        print(FIX_HINT)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
