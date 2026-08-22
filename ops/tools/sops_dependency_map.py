#!/usr/bin/env python3
"""repo 内の SOPS 暗号化資産と age 鍵の依存地図を JSON で産出する (P-0105)。

なぜ要るか: nix/images/proxmox-cloud/secrets.yaml は node01 cloud-init シークレットの
唯一の暗号化源だが、age 秘密鍵の所在・復元経路は repo のどこにも文書化されていなかった。
node01 全損時に鍵が無ければ暗号化資産は全滅する。「鍵の所在が人間の記憶にしか無い」
状態を、機械が毎回再構築できる地図に置き換える。test_backup_coverage.py /
check_credential_map.py (P-0047 / P-0071) と同じ発想。

地図の作り方 (ハードコードしない):

1. 暗号化ファイルの発見は探索的。`.sops.yaml` の creation_rules の path_regex と、
   本文の実物形の `ENC[...]` マーカー + sops メタデータブロックの両方から探す。
   「secrets.yaml という名前」に依存しないので、2 つ目の暗号化ファイルが生えたら
   勝手に地図に載る。逆に言うと **sops 以外の形式で書かれた秘密は映らない**
2. 消費者の発見も探索的。暗号化ファイルの basename への言及を repo 全体から集め、
   種類 (nix / terraform / ci / doc / log / tooling) に分類する。
   実行の連鎖 (nix build → sops-install-secrets) を解釈するのは人間と docs の仕事で、
   ここは「どこに言及があるか」の事実だけを残す
3. 鍵の所在は 3 層で出力: Terraform 変数 (repo 外から渡す値・repo に実体なし)、
   node01 上の鍵ファイル (/var/lib/sops-nix/key.txt、全損時の単一障害点)、
   この環境 (実行時の env / config / バイナリの在否のみ。**値は絶対に出力しない**)

出力は ops/sops-dependency-map.json (タイムスタンプを持たない — コミットした生成物の
diff を「変化のない実行で汚さない」ため)。stdout には人間用サマリを出す。

    python3 ops/tools/sops_dependency_map.py            # 地図を生成して rc を返す

fail-closed: .sops.yaml の欠落・破損、暗号化ファイルのゼロ件、creation rule に
一致しない暗号化ファイル、recipient を抽出できないファイル、nix/terraform/ci の
どれからも消費されない暗号化ファイルはすべて問題として rc!=0。
「地図が何も見つけられなかった」は整合ではなく走査の失敗として扱う。

判定ロジックの固定テストは ops/tests/test_sops_dependency_map.py
(`python3 -m unittest ops.tests.test_sops_dependency_map`)。

既知の死角 (静的スキャンでは埋められないので伏せずに書き残る):
  - 実物形の ENC マーカーと sops メタデータを両方引用した文書は誤検出されうる
    (現状の ops/projects/ ログはどちらも片方しか書いていないので検出されない)
  - basename の言及一致なので、別ディレクトリの同名ファイルへの言及も
    このファイルへの参照として数えてしまう (消費者の過大計上方向の誤差)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path, PurePosixPath

import yaml

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "ops" / "sops-dependency-map.json"

# 走査対象外。__pycache__ は CI ランナーにもローカルにも出現しうる生成物
SKIP_DIRS = {".git", ".terraform", "node_modules", "__pycache__"}
MAX_SCAN_BYTES = 2 * 1024 * 1024

# 「実物の形」の暗号化マーカーだけを認める。data: 以降に実長の base64 を要求するのは、
# PROJECT.md の `ENC[` のような散文言及を誤検出しないため (実在した false-positive 元)
_ENC_MARKER_RE = re.compile(
    r"ENC\[(?:AES256_GCM|CHACHA20_POLY1305|X25519|AWS_KMS|GCP_KMS"
    r"|AZURE_KEYVAULT|HC_VAULT),data:[A-Za-z0-9+/=]{20,}"
)
# YAML (sops:) と JSON ("sops":) の両方のメタデータブロック
_SOPS_METADATA_RE = re.compile(r'(?m)^\s*sops\s*:|"sops"\s*:')

# node01 上で age 秘密鍵が置かれるパス。cloud-init (vm-nixos.tf) が書き込み、
# sops.age.keyFile (configuration.nix) が読む。node01 全損時に消える実体
NODE_KEY_FILE = "/var/lib/sops-nix/key.txt"
_KEY_FILE_RE = re.compile(re.escape(NODE_KEY_FILE))
_PRIVATE_KEY_VAR_RE = re.compile(r'variable\s+"age_private_key"')

# 在否だけを見る env 名。値は読んでも決めて出力に載せない (平文漏洩防止)
KEY_ENV_VARS = ("SOPS_AGE_KEY", "SOPS_AGE_KEY_FILE")
AGE_CONFIG_KEYS = "sops/age/keys.txt"


def iter_text_files(root: Path):
    """走査対象のテキストファイルを (相対パス, 本文) で列挙する。

    バイナリ・巨大ファイル・SKIP_DIRS 配下は黙ってスキップする (列挙の網から
    外れること自体は問題にしない。暗号化資産がそれらの場所に置かれていたら
    それは運用の問題であって走査の責任ではない)
    """
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path == OUTPUT:
            continue
        try:
            if not (0 < path.stat().st_size <= MAX_SCAN_BYTES):
                continue
            yield rel.as_posix(), path.read_text(encoding="utf-8")
        except OSError:
            continue
        except UnicodeDecodeError:
            continue


def is_sops_encrypted(text: str) -> bool:
    """本文から SOPS 暗号化ファイルと判定できるか。マーカーとメタデータの AND。

    OR にすると散文が誤検出され、片方だけだと sops 以外の ENC 形式を拾ってしまう。
    """
    return bool(_ENC_MARKER_RE.search(text)) and bool(_SOPS_METADATA_RE.search(text))


def parse_encrypted_file(rel: str, text: str) -> tuple[dict, list[str]]:
    """暗号化ファイル 1 つの事実 (鍵名・recipient) を抜き出す。

    戻り値は (entry, problems)。YAML として壊れている場合も entry の骨格は返す
    (呼び側が他の検査を続けられるように)。
    """
    problems: list[str] = []
    entry = {
        "path": rel,
        "keys_in_file": [],
        "recipients": [],
        "matched_creation_rule": None,
        "references": {},
    }
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return entry, [f"{rel}: YAML が読めない (recipient を抽出できない): {e}"]
    if not isinstance(doc, dict):
        return entry, [f"{rel}: YAML ドキュメントが mapping でない"]
    entry["keys_in_file"] = sorted(k for k in doc if k != "sops")
    meta = doc.get("sops") or {}
    for item in meta.get("age") or []:
        recipient = (item or {}).get("recipient")
        if recipient:
            entry["recipients"].append(recipient)
    if not entry["recipients"]:
        problems.append(f"{rel}: sops メタデータから recipient を抽出できない")
    return entry, problems


def load_creation_rules(root: Path) -> tuple[list[dict], list[str]]:
    """.sops.yaml の creation_rules を (正規表現, age recipients) に展開する。

    YAML アンカー (&node01 /*node01) は safe_load が解決するので素通しでよい。
    """
    problems: list[str] = []
    rules: list[dict] = []
    path = root / ".sops.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return [], [f".sops.yaml が読めない: {e}"]
    try:
        config = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return [], [f".sops.yaml が読めない: {e}"]
    if not isinstance(config, dict) or not isinstance(
        config.get("creation_rules"), list
    ):
        return [], [".sops.yaml に creation_rules (list) が無い"]
    for i, rule in enumerate(config["creation_rules"]):
        if not isinstance(rule, dict):
            problems.append(f".sops.yaml creation_rules[{i}] が mapping でない")
            continue
        raw_regex = rule.get("path_regex")
        compiled = None
        if raw_regex is not None:
            try:
                compiled = re.compile(raw_regex)
            except re.error as e:
                problems.append(f".sops.yaml creation_rules[{i}] の path_regex が不正: {e}")
        recipients: list[str] = []
        groups = rule.get("key_groups") or ([rule] if (rule.get("age") or rule.get("pgp")) else [])
        for group in groups:
            recipients.extend(a for a in (group.get("age") or []) if a)
        rules.append(
            {
                "path_regex": raw_regex,
                "compiled": compiled,
                "age_recipients": sorted(set(recipients)),
                "matches_any_encrypted_file": False,
            }
        )
    return rules, problems


def classify_reference(rel: str) -> str:
    """参照元ファイルの種類。消費者 (nix/terraform/ci) と記録 (doc/log) を分ける。"""
    p = PurePosixPath(rel)
    parts = p.parts
    if parts[:2] == (".github", "workflows"):
        return "ci"
    if parts[:2] == ("ops", "projects"):
        # autopilot の帳簿。実行連鎖の一部ではない (作業記録がパスに言及するだけ)
        return "log"
    if p.suffix == ".nix":
        return "nix"
    if p.suffix == ".tf":
        return "terraform"
    if parts[:1] == ("docs",) or p.suffix in (".md", ".rst"):
        return "doc"
    if parts[:1] == ("ops",):
        return "tooling"
    if p.suffix in (".sh", ".py"):
        return "tooling"
    return "other"


FUNCTIONAL_CONSUMER_KINDS = ("nix", "terraform", "ci")


def find_references(
    encrypted_rel: str, files: dict[str, str]
) -> dict[str, list[dict]]:
    """basename への言及を repo 全体から集め、種類ごとに分類する。

    行番号付き。自分自身・.sops.yaml (ルール定義そのもの)・生成物は除く。
    """
    basename = Path(encrypted_rel).name
    # 左にも右にも名前の切れ目を要求する (my-secrets.yaml / secrets.yaml.bak を弾く)
    needle = re.compile(rf"(?<![\w.-])(?:\./)?{re.escape(basename)}(?![\w.-])")
    refs: dict[str, list[dict]] = {}
    for rel, text in files.items():
        if rel in (encrypted_rel, ".sops.yaml"):
            continue
        lines = [
            no
            for no, line in enumerate(text.splitlines(), start=1)
            if needle.search(line)
        ]
        if lines:
            kind = classify_reference(rel)
            refs.setdefault(kind, []).append({"path": rel, "lines": lines})
    return refs


def find_key_locations(files: dict[str, str]) -> dict:
    """age 秘密鍵の所在に関する repo 内の事実 (Terraform 変数と node01 上の鍵ファイル)。"""
    defined_in: list[str] = []
    referenced_in: list[str] = []
    key_file_mentions: list[dict] = []
    for rel, text in files.items():
        has_var = _PRIVATE_KEY_VAR_RE.search(text) is not None
        has_ref = "age_private_key" in text
        if has_ref:
            (defined_in if has_var else referenced_in).append(rel)
        if _KEY_FILE_RE.search(text):
            key_file_mentions.append({"path": rel, "kind": classify_reference(rel)})
    return {
        "private_key_variable": {
            # 値は repo に存在しない。terraform apply のたびに外部から渡される
            "name": "age_private_key",
            "defined_in": sorted(defined_in),
            "referenced_in": sorted(referenced_in),
            "note": "値は repo 外。terraform apply 時に渡す",
        },
        "key_file": {
            "path_on_node": NODE_KEY_FILE,
            "mentioned_in": sorted(key_file_mentions, key=lambda m: m["path"]),
            "note": "node01 全損時に消える実体。cloud-init が書き、sops.age.keyFile が読む",
        },
    }


def probe_agent_env(env=None, home=None, which_fn=None) -> dict:
    """この環境で復号できる気配があるか。**在否のみ。値は一切出力しない。**

    can_decrypt_now は楽観判定 (バイナリがあり、鍵らしきものがある) なので、
    True でも実際に復号できる保証はない。False ならほぼ確実に不可能。
    """
    env = os.environ if env is None else env
    home = Path.home() if home is None else Path(home)
    which_fn = shutil.which if which_fn is None else which_fn
    present = sorted(k for k in KEY_ENV_VARS if env.get(k))
    xdg = env.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else home / ".config"
    has_config_keys = (base / AGE_CONFIG_KEYS).exists()
    has_sops = which_fn("sops") is not None
    return {
        "sops_binary_found": has_sops,
        "age_binary_found": which_fn("age") is not None,
        "key_env_vars_present": present,
        "config_keys_txt_found": has_config_keys,
        "can_decrypt_now": has_sops and (bool(present) or has_config_keys),
        "note": "在否のみを出力し値は出力しない。can_decrypt_now は楽観判定 (True でも復号成功を保証しない)",
    }


def build_map(root: Path, agent_env: dict | None = None) -> tuple[dict, list[str]]:
    """repo 走査 → 地図 (dict) と問題リスト。main とテストの共通本体。"""
    problems: list[str] = []

    files = dict(iter_text_files(root))
    encrypted_rels = [rel for rel, text in files.items() if is_sops_encrypted(text)]
    if not encrypted_rels:
        problems.append(
            "SOPS 暗号化ファイルを 1 つも見つけられなかった。"
            "暗号化資産が消えたか、走査の網 (マーカー判定) が壊れている"
        )

    rules, rule_problems = load_creation_rules(root)
    problems.extend(rule_problems)

    entries = []
    for rel in encrypted_rels:
        entry, file_problems = parse_encrypted_file(rel, files[rel])
        problems.extend(file_problems)

        matched = None
        for rule in rules:
            if rule["compiled"] is not None and rule["compiled"].search(rel):
                matched = rule
                break
        if matched is None:
            problems.append(
                f"{rel}: 一致する creation_rule が .sops.yaml に無い"
                " (宣言外の方法で暗号化されている)"
            )
        else:
            matched["matches_any_encrypted_file"] = True
            entry["matched_creation_rule"] = {
                "path_regex": matched["path_regex"],
                "age_recipients": matched["age_recipients"],
            }

        entry["references"] = find_references(rel, files)
        functional = {
            kind: entry["references"][kind]
            for kind in FUNCTIONAL_CONSUMER_KINDS
            if entry["references"].get(kind)
        }
        if encrypted_rels and not functional:
            problems.append(
                f"{rel}: nix/terraform/ci のどれからも消費されていない"
                " (誰も複号しない孤児資産か、消費者が消えた)"
            )
        entries.append(entry)

    return (
        {
            "schema_version": 1,
            "encrypted_files": entries,
            "creation_rules": [
                {k: v for k, v in rule.items() if k != "compiled"} for rule in rules
            ],
            "key_locations": find_key_locations(files),
            "agent_environment": probe_agent_env(**(agent_env or {})),
            "problems": problems,
        },
        problems,
    )


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    out_path = Path(argv[0]).resolve() if argv else OUTPUT

    try:
        map_data, problems = build_map(ROOT)
    except Exception as e:  # noqa: BLE001 — 走査に失敗したら成功扱いにしない
        print(f"::error::走査に失敗しました: {type(e).__name__}: {e}")
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(map_data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    for enc in map_data["encrypted_files"]:
        kinds = ",".join(enc["references"]) or "消費者なし"
        print(
            f"# {enc['path']} keys={enc['keys_in_file']}"
            f" recipients={len(enc['recipients'])} consumers=[{kinds}]"
        )
    print(
        f"# key: var={map_data['key_locations']['private_key_variable']['name']}"
        f" on_node={map_data['key_locations']['key_file']['path_on_node']}"
        f" this_env_can_decrypt={map_data['agent_environment']['can_decrypt_now']}"
    )
    if problems:
        for problem in problems:
            print(f"::error::{problem}", file=sys.stderr)
        print(f"::error::依存地図に問題が {len(problems)} 件あります", file=sys.stderr)
        return 1
    print(f"ok: 地図を {out_path} に書きました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
