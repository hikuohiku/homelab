"""全 backup 対象を drill-* 隔離 namespace へ同時復元し、生き返りまでの壁時計 (RTO) を測る。

P-0080。backup 5 対象 (vaultwarden-data / immich-library / coder-postgres-data /
coder-workspace-homes / syncthing-data) の restic snapshot を、本番 namespace とは別の
drill-restore-* namespace の新規 PVC へ restore し、アプリ相当の最低限の起動確認
(liveness 判定) を通るまでの wall clock を秒で出す。**本番 PVC には一切触れない**
(参照するのは credential の Doppler キーだけで、volume はすべて drill 側の新規 PVC)。

使い方 (リポジトリルートで):
    python3 ops/drills/restore_drill.py                  # 実行。cluster 接続が要る
    python3 ops/drills/restore_drill.py --preflight-only # B2 download 可否だけ確認して終了
    python3 ops/drills/restore_drill.py --dry-run        # manifest 生成だけ確認して終了
    python3 ops/drills/restore_drill.py --keep-namespaces  # 後片付け (namespace delete) を省略

設計 (PROJECT.md「決めてあること」):
- RTO = 「PVC 作成要求の時刻」から「liveness 合格の時刻」までの壁時計。どちらも
  kubernetes API server 側のタイムスタンプ (PVC creationTimestamp / Job completionTime)
  を使うので、image pull や PVC provision の待ち時間も含んだ誇張なしの数字になる
- liveness 合格 == Job の Complete。各 Job は restore の後に判定スクリプトを走らせ、
  set -eu 相当で失敗すれば非 0 で落ちる。合格しない限り Complete にならない
- credential は既存の `<app>-restic-backup-credentials` (append-only 鍵) と同じ Doppler
  キーを ClusterSecretStore 経由で drill namespace 内に同期した ExternalSecret から引く。
  新規登録不要・削除鍵は持ち出さない (P-0028 / P-0047 実績どおり)
- 復元 Job には CHOWN / FOWNER / DAC_OVERRIDE の 3 capability + restore 前 rm -rf が必須
  (docs/backup.md「復元試験」。P-0047 が再踏んだ罠)
- 夜間帯 (JST 02:40–05:00) との重なりを避けるため、その帯では起動時に中断する

CI (cluster 接続なし) 向けに、計画生成・manifest 生成・RTO 計算・report 検査は純関数に
分離してある。ops/tests/test_restore_drill.py が合成入力で両方向を固定する。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from datetime import time as day_time
from pathlib import Path

# ---------------------------------------------------------------------------
# 定数 (既存の pin と揃える。新しい image tag を持ち込まない)
# ---------------------------------------------------------------------------

RESTIC_IMAGE = "restic/restic:0.19.1"      # 既存 backup CronJob と同じ pin
CHECK_IMAGE = "python:3.12-alpine"         # vaultwarden backup initContainer と同じ pin
POSTGRES_IMAGE = "postgres:17.10"          # apps/coder/postgres.yaml と同じ pin

CLUSTER_SECRET_STORE = "doppler"
STORAGE_CLASS = "local-path"

JOB_ACTIVE_DEADLINE_SECONDS = 1500
POLL_INTERVAL_SECONDS = 10
DEFAULT_OVERALL_TIMEOUT_SECONDS = 1800

# B2 download preflight。本番の restore を起こす前に最小リポジトリ 1 本で
# 「今すぐ download できるか」を確かめる (2026-08-22 実測: B2 の download cap を
# 超えた状態で起動すると全 unit が最初の Stat(<config/>) から 403 になり、
# activeDeadline (25分) までリトライを続けて時間だけが溶ける)
PREFLIGHT_NAMESPACE = "drill-preflight"
PROBE_JOB_NAME = "restore-drill-preflight"
PROBE_POLL_INTERVAL_SECONDS = 10
# server 側の backstop (Job の activeDeadlineSeconds)。スクリプトが死んでも probe が
# 無期限に Class C transaction を消費しないようにする壁。**client の観測窓より十分長く
# する**: deadline で Job コントローラが pod を消すとログも数分で消えるため、証拠は
# 生きた pod から取る必要がある (2026-08-22 実測: 180s で同時判定すると pod 跡が消えて
# 「Failed なのに証拠なし」になった)
PROBE_ACTIVE_DEADLINE_SECONDS = 900
# client 側の観測窓。この秒数で Complete/Failed が出なければ判定不能として中断する。
# 健常なら syncthing 最小リポジトリの snapshots は数十秒、403 リトライなら窓内に
# ログが溜まる。観測打ち切り時に pod はまだ生きている (server deadline 未達) ので、
# ログを採ってから後片付けできる
PROBE_OBSERVE_SECONDS = 240

# B2 が download cap 超過時に返す 403。raw API body の code 名、restic ログに現れる
# メッセージ全文に加え、**文面が「403: 」で切れた形式**にも対応させる
# (2026-08-22 実測: 同じ cap 超過でも restic のリトライ行が
#  "Stat: b2_download_file_by_name: 403: " で終わり、理由文言が空のまま出ることがある。
#  download API 名 + 403 の組み合わせ自体を cap 超過扱いにする — 認証不足は 401 で来るので
#  download 系 403 は実運用上すべて cap/上限系。見逃した場合のコスト (全 unit 盲走) の方が
#  過剰中断 (数分後の再試行) より大きい)
DOWNLOAD_CAP_ERROR_MARKERS = (
    "download_cap_exceeded",
    "download bandwidth or transaction (Class B) cap exceeded",
    "b2_download_file_by_name: 403",
)

# 夜間帯の回避 (JST)。backup CronJob 群 (2:45/3:10/3:30/3:40/3:55) と retention
# (日曜 3:45–4:50)。drill は snapshot の読み取りしかしないが lock 競合の警告を
# 出させないため、この帯での実行を避ける (PROJECT.md 前提節)
FORBIDDEN_JST_START = day_time(2, 40)
FORBIDDEN_JST_END = day_time(5, 0)
JST = timezone(timedelta(hours=9), name="JST")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORT_PATH = REPO_ROOT / "ops/projects/logs/P-0080/report.json"

# ---------------------------------------------------------------------------
# 対象の定義 (docs/backup.md「バックアップ対象一覧」と 1 対 1 で対応)
# ---------------------------------------------------------------------------

TARGETS = [
    {
        # SQLite 本体 (online backup API で一貫コピー済み db.sqlite3 が /staging 配下に
        # 入る) + attachments 等。snapshot パス: /mnt/vaultwarden-data と /staging/db.sqlite3
        "name": "vaultwarden-data",
        "kind": "sqlite",
        "source_namespace": "vaultwarden",
        "repo_suffix": "vaultwarden",
        "secret_name": "vaultwarden-restic-backup-credentials",
        "drill_namespace": "drill-restore-vaultwarden",
        "pvc_size": "1Gi",
    },
    {
        # ライブラリ本体 + immich 内蔵の日次 DB ダンプ (backups/*.sql.gz)。
        # snapshot パス: /mnt/immich-library
        "name": "immich-library",
        "kind": "library-with-db-dump",
        "source_namespace": "immich",
        "repo_suffix": "immich",
        "secret_name": "immich-restic-backup-credentials",
        "drill_namespace": "drill-restore-immich",
        "pvc_size": "5Gi",
    },
    {
        # PGDATA ではなく pg_dump -Fc ダンプ 1 ファイル (/staging/coder-postgres.dump)。
        # 生 PGDATA を戻しても動かないため、新規 postgres cluster へ pg_restore する
        "name": "coder-postgres-data",
        "kind": "pg-dump",
        "source_namespace": "coder",
        "repo_suffix": "coder-postgres",
        "secret_name": "coder-restic-backup-credentials",
        "drill_namespace": "drill-restore-coder-postgres",
        "pvc_size": "1Gi",  # 新規 cluster の PGDATA を載せる drill PVC
    },
    {
        # 動的 PVC coder-<workspace-id>-home の集合。1 リポジトリを --host <workspace-id>
        # で共有しているため、workspace ごとに 1 unit へ展開する (expand_units)
        "name": "coder-workspace-homes",
        "kind": "workspace-home",
        "source_namespace": "coder",
        "repo_suffix": "coder-workspace-homes",
        "secret_name": "coder-restic-backup-credentials",
        "drill_namespace": "drill-restore-workspace-home",
        "pvc_size": "5Gi",
    },
    {
        # identity (cert.pem/key.pem) + 設定。index DB は意図して除外済み
        # (docs/backup.md「除外するものと、その理由」)。XML パース + cert/key 存在で判定
        "name": "syncthing-data",
        "kind": "syncthing-config",
        "source_namespace": "syncthing",
        "repo_suffix": "syncthing",
        "secret_name": "syncthing-restic-backup-credentials",
        "drill_namespace": "drill-restore-syncthing",
        "pvc_size": "1Gi",
    },
]

# liveness 判定基準。PROJECT.md 設計方針 2「判定基準はスクリプト内の定数として明示し、
# テストからも使う」。ここにある文言がそのまま Job 内スクリプトの実装要件になる
LIVENESS_CRITERIA = {
    "sqlite": "db.sqlite3 の SQLite マジックバイト ('SQLite format 3\\x00') + "
              "PRAGMA integrity_check が ok。加えて rsa_key*.pem (JWT 署名鍵) の存在を報告",
    "library-with-db-dump": "ライブラリのファイル数 > 0 + 最新 backups/*.sql.gz が最後まで "
                            "展開できる (gzip 展開バイト数 > 0)",
    "pg-dump": "pg_dump -Fc ダンプを新規 postgres cluster (postgres:17.10、サーバと同バージョン) "
               "へ --exit-on-error で pg_restore し、public スキーマのテーブル数 > 0、"
               "最後に pg_isready が通る",
    "workspace-home": "restic restore latest --host <id> が通り、復元されたエントリ数 "
                      "(ファイル+ディレクトリ+シンボリックリンク。restic ls の平文出力は dir に "
                      "末尾 / を付けないため全部数えて突き合わせる) が restic ls --host <id> latest の "
                      "行数と一致し、かつ > 0",
    "syncthing-config": "config/config.xml が XML としてパースでき、同ディレクトリに "
                        "cert.pem / key.pem (デバイス ID の本体) が存在する",
}

# ---------------------------------------------------------------------------
# 純関数 (cluster 接続なしでテスト可能)
# ---------------------------------------------------------------------------


def in_forbidden_jst_window(now_utc: datetime) -> bool:
    """与えた UTC 时刻が JST の夜間帯 [02:40, 05:00) に入っていれば True。

    backup CronJob 群との重なりを避けるためのガード。境界値 (02:40 / 05:00) も含めて
    テストで固定する。
    """
    jst_time = now_utc.astimezone(JST).time()
    return FORBIDDEN_JST_START <= jst_time < FORBIDDEN_JST_END


def parse_k8s_timestamp(value: str) -> datetime:
    """kubernetes の RFC3339 タイムスタンプ (例: 2026-08-22T13:14:15Z) を aware UTC に。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def compute_rto_seconds(start_iso: str, end_iso: str) -> int:
    """RTO を秒で返す。「PVC 作成要求」から「liveness 合格」までの壁時計。

    秒未満は切り捨て (誇張しないための方向付け)。
    """
    delta = parse_k8s_timestamp(end_iso) - parse_k8s_timestamp(start_iso)
    return max(int(delta.total_seconds()), 0)


def expand_units(targets: list[dict], workspace_ids: list[str]) -> list[dict]:
    """targets を実行単位 (unit) に展開する。

    workspace-home だけは 1 リポジトリを --host <workspace-id> で共有しているため、
    workspace ごとに独立した Job+PVC になる。他 target は 1 target = 1 unit。
    各 unit には一意な job_name / pvc_name を付ける。
    """
    units = []
    for target in targets:
        if target["kind"] == "workspace-home":
            for ws_id in workspace_ids:
                slug = "ws-" + ws_id[:8]
                units.append(_make_unit(target, slug, workspace_id=ws_id))
        else:
            slug = target["name"]
            units.append(_make_unit(target, slug))
    return units


def _make_unit(target: dict, slug: str, workspace_id: str | None = None) -> dict:
    return {
        "target_name": target["name"],
        "kind": target["kind"],
        "repo_suffix": target["repo_suffix"],
        "secret_name": target["secret_name"],
        "namespace": target["drill_namespace"],
        "pvc_size": target["pvc_size"],
        "job_name": f"restore-drill-{slug}",
        "pvc_name": f"drill-pvc-{slug}",
        "workspace_id": workspace_id,
    }


def build_namespace_manifest(name: str) -> dict:
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name}}


def build_externalsecret_manifest(namespace: str, secret_name: str) -> dict:
    """drill namespace 内に append-only credential を同期する ExternalSecret。

    本番 namespace の Secret は namespace スコープで参照できないため、同じ Doppler キー
    (`*_APPEND_ONLY`) から drill 側に同名 Secret を作らせる。削除鍵は復元に不要なので
    参照しない (P-0028 実測: 復元は readFiles で足りる)。
    """
    return {
        "apiVersion": "external-secrets.io/v1",
        "kind": "ExternalSecret",
        "metadata": {"name": secret_name, "namespace": namespace},
        "spec": {
            "refreshInterval": "1h",
            "secretStoreRef": {"kind": "ClusterSecretStore", "name": CLUSTER_SECRET_STORE},
            "target": {
                "name": secret_name,
                "creationPolicy": "Owner",
                # namespace ごと消すので実際にはどちらでもよいが、本番の manifest と
                # 同じ形を揃える (apps/*/restic-external-secret.yaml)
                "deletionPolicy": "Retain",
            },
            "data": [
                {"secretKey": "RESTIC_PASSWORD", "remoteRef": {"key": "RESTIC_PASSWORD"}},
                {"secretKey": "RESTIC_B2_BUCKET", "remoteRef": {"key": "RESTIC_B2_BUCKET"}},
                {
                    "secretKey": "B2_ACCOUNT_ID",
                    "remoteRef": {"key": "B2_ACCOUNT_ID_APPEND_ONLY"},
                },
                {
                    "secretKey": "B2_ACCOUNT_KEY",
                    "remoteRef": {"key": "B2_ACCOUNT_KEY_APPEND_ONLY"},
                },
            ],
        },
    }


def build_pvc_manifest(namespace: str, name: str, size: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": STORAGE_CLASS,
            "resources": {"requests": {"storage": size}},
        },
    }


# 既存 backup CronJob と同じ env ブロック。credential は append-only 鍵のみ。
# 注意: kubernetes の $(VAR) 依存展開は「リスト内で前方に定義された変数」しか参照できない
# ので、RESTIC_B2_BUCKET を RESTIC_REPOSITORY より先に置くこと (初回実行で後ろに置いて
# bucket 名が空になり Fatal になった実測あり)
RESTIC_ENV_KEYS = ["RESTIC_PASSWORD", "B2_ACCOUNT_ID", "B2_ACCOUNT_KEY"]


def restic_env(secret_name: str, repo_suffix: str) -> list[dict]:
    env = [
        {
            "name": "RESTIC_B2_BUCKET",
            "valueFrom": {"secretKeyRef": {"name": secret_name, "key": "RESTIC_B2_BUCKET"}},
        },
        {"name": "RESTIC_REPOSITORY", "value": f"b2:$(RESTIC_B2_BUCKET):{repo_suffix}"},
    ]
    for key in RESTIC_ENV_KEYS:
        env.append(
            {
                "name": key,
                "valueFrom": {"secretKeyRef": {"name": secret_name, "key": key}},
            }
        )
    return env


# docs/backup.md「復元試験」の教訓。この 3 つが無いと restic は lchown / utimensat で
# EPERM になり Fatal で終わる (P-0047 が 2 回踏んだ)
RESTORE_CAPABILITIES = ["CHOWN", "FOWNER", "DAC_OVERRIDE"]


def restore_security_context() -> dict:
    return {
        "runAsUser": 0,
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"], "add": list(RESTORE_CAPABILITIES)},
    }


def restore_script(target_path: str, extra_args: str = "") -> str:
    """restic restore の共通スクリプト。restore 前の rm -rf は必須の教訓。"""
    return (
        "set -eu\n"
        "rm -rf " + target_path + "/* 2>/dev/null || true\n"
        f"restic restore latest {extra_args}--target {target_path}\n"
    )


# 判定スクリプトは DRILL_BASE (default: /data) を起点に走る。環境変数で差し替えられる
# のはテストのため — ops/tests がこの定数そのものを実行して判定ロジックを固定する
VAULTWARDEN_CHECK_PY = """\
import glob, os, sqlite3
base = os.environ.get("DRILL_BASE", "/data")
dbs = sorted(glob.glob(base + "/**/db.sqlite3", recursive=True))
assert dbs, "VAULTWARDEN_LIVENESS_FAIL: db.sqlite3 が復元されていない"
path = dbs[0]
with open(path, "rb") as f:
    magic = f.read(16)
assert magic == b"SQLite format 3\\x00", f"{path}: bad magic bytes {magic!r}"
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
try:
    row = con.execute("PRAGMA integrity_check").fetchone()
finally:
    con.close()
assert row and row[0] == "ok", f"{path}: integrity_check={row!r}"
keys = sorted(glob.glob(base + "/**/rsa_key*.pem", recursive=True))
print(f"VAULTWARDEN_LIVENESS_OK path={path} rsa_keys={len(keys)}")
"""

SYNCTHING_CHECK_PY = """\
import glob, os
import xml.etree.ElementTree as ET
base = os.environ.get("DRILL_BASE", "/data")
configs = sorted(glob.glob(base + "/**/config/config.xml", recursive=True))
assert configs, "SYNCTHING_LIVENESS_FAIL: config/config.xml が復元されていない"
cfg = configs[0]
root = ET.parse(cfg).getroot()
d = os.path.dirname(cfg)
for required in ("cert.pem", "key.pem"):
    assert os.path.isfile(os.path.join(d, required)), f"missing {required}"
print(f"SYNCTHING_LIVENESS_OK config={cfg} xml_root=<{root.tag}> cert_and_key=present")
"""


def _restic_restore_container(unit: dict, volume_name: str, mount_path: str,
                              script: str, name: str) -> dict:
    return {
        "name": name,
        "image": RESTIC_IMAGE,
        "terminationMessagePolicy": "FallbackToLogsOnError",
        "command": ["sh", "-c", script],
        "env": restic_env(unit["secret_name"], unit["repo_suffix"]),
        "securityContext": restore_security_context(),
        "resources": {
            "requests": {"cpu": "50m", "memory": "128Mi"},
            "limits": {"cpu": "500m"},
        },
        "volumeMounts": [{"name": volume_name, "mountPath": mount_path}],
    }


def _python_liveness_container(script: str) -> dict:
    return {
        "name": "liveness-check",
        "image": CHECK_IMAGE,
        "terminationMessagePolicy": "FallbackToLogsOnError",
        "command": ["python", "-c", script],
        "resources": {
            "requests": {"cpu": "10m", "memory": "32Mi"},
            "limits": {"cpu": "300m", "memory": "256Mi"},
        },
        "volumeMounts": [{"name": "data", "mountPath": "/data"}],
    }


IMMICH_LIVENESS_SH = """\
set -eu
set -o pipefail
BASE="${DRILL_BASE:-/data}"
FILES=$(find "$BASE/mnt" -type f | wc -l)
echo "library_files=$FILES"
[ "$FILES" -gt 0 ]
DUMP=$(find "$BASE" -type f -name '*.sql.gz' | sort | tail -n 1)
[ -n "$DUMP" ] || { echo "IMMICH_LIVENESS_FAIL: backups/*.sql.gz が無い"; exit 1; }
BYTES=$(gzip -cd "$DUMP" | wc -c)
echo "latest_dump=$DUMP decompressed_bytes=$BYTES"
[ "$BYTES" -gt 0 ]
echo "IMMICH_LIVENESS_OK library_files=$FILES latest_dump=$DUMP decompressed_bytes=$BYTES"
"""

WORKSPACE_HOME_LIVENESS_SH = """\
set -eu
set -o pipefail
BASE="${DRILL_BASE:-/data}"
# restic ls の平文出力は dir に末尾 / を付けない (0.19.1 実測。docs/backup.md T-0071 の
# 「サマリ 3904 files/dirs vs find -type f 3156」と同じ事実)。dir だけを除外するフィルタは
# 平文では書けないため、全エントリ数で突き合わせる: ls 行数(ヘッダ1行を除く) ==
# 復元木のエントリ数(find は起点自身も数えるので引く)
LS_TOTAL=$(restic ls --host "$WORKSPACE_ID" latest | grep -vc '^snapshot ')
FOUND=$(( $(find "$BASE" | wc -l) - 1 ))
echo "ls_entries=$LS_TOTAL restored_entries=$FOUND"
[ "$FOUND" -gt 0 ]
if [ "$FOUND" -ne "$LS_TOTAL" ]; then
  echo "WORKSPACE_HOME_LIVENESS_FAIL: restic ls と復元結果の件数が一致しない"; exit 1
fi
echo "WORKSPACE_HOME_LIVENESS_OK workspace=$WORKSPACE_ID entries=$FOUND"
"""

POSTGRES_SERVER_SH = """\
set -eu
rm -f /coord/done
docker-entrypoint.sh postgres &
SERVER=$!
i=0
while [ ! -f /coord/done ]; do
  sleep 2
  i=$((i+1))
  if ! kill -0 $SERVER 2>/dev/null; then
    echo "POSTGRES_SERVER_FAIL: postgres プロセスが予期せず終了した"; exit 1
  fi
  if [ "$i" -gt 300 ]; then
    echo "POSTGRES_SERVER_FAIL: check コンテナの完了通知が 600 秒来ない"; exit 1
  fi
done
echo "check passed; stopping postgres"
kill -TERM $SERVER
wait $SERVER || true
echo "POSTGRES_SERVER_CONTAINER_OK"
"""

PG_CHECK_SH = """\
set -eu
DUMP=$(find /dump -type f ! -path '*/lost+found/*' | head -n 1)
[ -n "$DUMP" ] || { echo "PG_CHECK_FAIL: ダンプファイルが無い"; exit 1; }
echo "dump_file=$DUMP"
i=0
until pg_isready -h 127.0.0.1 -p 5432 -U postgres >/dev/null 2>&1; do
  i=$((i+1))
  [ "$i" -lt 90 ] || { echo "PG_CHECK_FAIL: postgres が ready にならない"; exit 1; }
  sleep 2
done
echo "postgres ready after $((i * 2))s"
pg_restore -h 127.0.0.1 -p 5432 -U postgres -d drill --no-owner --no-privileges \\
  --exit-on-error "$DUMP"
TABLES=$(psql -h 127.0.0.1 -U postgres -d drill -tAc \\
  "select count(*) from information_schema.tables where table_schema='public'")
pg_isready -h 127.0.0.1 -p 5432 -U postgres
echo "CODER_POSTGRES_LIVENESS_OK dump=$DUMP public_tables=$TABLES"
[ "$TABLES" -gt 0 ] || { echo "PG_CHECK_FAIL: public テーブルが 0"; exit 1; }
touch /coord/done
"""


def build_job_manifest(unit: dict) -> dict:
    """unit の復元+liveness Job。liveness 合格まで落ちないので Complete == 生き返り。"""
    spec_meta = {
        "backoffLimit": 0,  # 失敗を隠さず即終わらせる
        "activeDeadlineSeconds": JOB_ACTIVE_DEADLINE_SECONDS,
        "template": {
            "metadata": {"labels": {"app.kubernetes.io/name": unit["job_name"]}},
            "spec": {
                "automountServiceAccountToken": False,
                "restartPolicy": "Never",
                "containers": [],
                "initContainers": [],
                "volumes": [],
            },
        },
    }
    pod_spec = spec_meta["template"]["spec"]

    if unit["kind"] in ("sqlite", "syncthing-config"):
        # initContainer (restic restore) → main container (python で判定) の 2 段。
        # restic イメージに python は無く、python イメージに restic は無いため分離する
        pod_spec["initContainers"] = [
            _restic_restore_container(
                unit, "data", "/data", restore_script("/data"), "restic-restore"
            )
        ]
        check = VAULTWARDEN_CHECK_PY if unit["kind"] == "sqlite" else SYNCTHING_CHECK_PY
        pod_spec["containers"] = [_python_liveness_container(check)]
        pod_spec["volumes"] = [
            {"name": "data", "persistentVolumeClaim": {"claimName": unit["pvc_name"]}}
        ]

    elif unit["kind"] == "library-with-db-dump":
        # restic イメージ単独で restore + gzip 展開検査まで (busybox で足りる)
        script = restore_script("/data") + IMMICH_LIVENESS_SH
        pod_spec["containers"] = [
            _restic_restore_container(
                unit, "data", "/data", script, "restic-restore-liveness"
            )
        ]
        pod_spec["volumes"] = [
            {"name": "data", "persistentVolumeClaim": {"claimName": unit["pvc_name"]}}
        ]

    elif unit["kind"] == "workspace-home":
        assert unit["workspace_id"], "workspace-home unit には workspace_id が必須"
        script = restore_script("/data", "--host \"$WORKSPACE_ID\" ") + WORKSPACE_HOME_LIVENESS_SH
        container = _restic_restore_container(
            unit, "data", "/data", script, "restic-restore-liveness"
        )
        container["env"].append({"name": "WORKSPACE_ID", "value": unit["workspace_id"]})
        pod_spec["containers"] = [container]
        pod_spec["volumes"] = [
            {"name": "data", "persistentVolumeClaim": {"claimName": unit["pvc_name"]}}
        ]

    elif unit["kind"] == "pg-dump":
        # 構成: initContainer がダンプを emptyDir へ復元 → postgres-server が新規 cluster
        # を drill PVC 上に起こす → check が localhost へ pg_restore + pg_isready。
        # postgres-server は check の完了マーカー (/coord/done) を見て自分も終わる
        pod_spec["initContainers"] = [
            _restic_restore_container(
                unit, "dump", "/dump", restore_script("/dump"), "restic-restore"
            )
        ]
        pg_server = {
            "name": "postgres-server",
            "image": POSTGRES_IMAGE,
            "terminationMessagePolicy": "FallbackToLogsOnError",
            "command": ["sh", "-c", POSTGRES_SERVER_SH],
            "env": [
                {"name": "POSTGRES_DB", "value": "drill"},
                {"name": "POSTGRES_HOST_AUTH_METHOD", "value": "trust"},
            ],
            "resources": {
                "requests": {"cpu": "50m", "memory": "256Mi"},
                "limits": {"cpu": "500m", "memory": "512Mi"},
            },
            "volumeMounts": [
                {"name": "pgdata", "mountPath": "/var/lib/postgresql/data"},
                {"name": "dump", "mountPath": "/dump"},
                {"name": "coord", "mountPath": "/coord"},
            ],
        }
        pg_check = {
            "name": "restore-check",
            "image": POSTGRES_IMAGE,
            "terminationMessagePolicy": "FallbackToLogsOnError",
            "command": ["sh", "-c", PG_CHECK_SH],
            "resources": {
                "requests": {"cpu": "20m", "memory": "64Mi"},
                "limits": {"cpu": "300m", "memory": "128Mi"},
            },
            "volumeMounts": [
                {"name": "dump", "mountPath": "/dump", "readOnly": True},
                {"name": "coord", "mountPath": "/coord"},
            ],
        }
        pod_spec["containers"] = [pg_server, pg_check]
        pod_spec["volumes"] = [
            {"name": "pgdata", "persistentVolumeClaim": {"claimName": unit["pvc_name"]}},
            {"name": "dump", "emptyDir": {}},
            {"name": "coord", "emptyDir": {}},
        ]

    else:  # pragma: no cover - TARGETS の kind を増やしたらここに来る
        raise ValueError(f"未知の kind: {unit['kind']}")

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": unit["job_name"], "namespace": unit["namespace"]},
        "spec": spec_meta,
    }


def is_download_cap_error(text: str) -> bool:
    """与えたログ/レスポンス本文が B2 の download cap 超過 (403) かどうか。

    B2 無料枠の download 帯域 (1GB/day) や Class B transaction 数の上限を超えると
    b2_download_file_by_name が 403 を返し、restic はリトライを繰り返す。cap は
    アカウント全体で共有されるため、1 対象でもこれが出たら全対象が同じ運命になる。
    download API 名 + 403 の組合せは理由文言が欠けていても cap 超過扱いにする
    (DOWNLOAD_CAP_ERROR_MARKERS の注記を参照)。
    """
    return any(marker in text for marker in DOWNLOAD_CAP_ERROR_MARKERS)


def build_probe_job(secret_name: str, repo_suffix: str) -> dict:
    """preflight probe。最小リポジトリで `restic snapshots` するだけの Job。

    snapshots の読み取りは config + index の download (Class B) を伴うため、
    rc=0 == 「download が今すぐ通る」。失敗したらログから cap 超過を判定する。
    """
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": PROBE_JOB_NAME, "namespace": PREFLIGHT_NAMESPACE},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": PROBE_ACTIVE_DEADLINE_SECONDS,
            "template": {
                "metadata": {"labels": {"app.kubernetes.io/name": PROBE_JOB_NAME}},
                "spec": {
                    "automountServiceAccountToken": False,
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "probe",
                            "image": RESTIC_IMAGE,
                            "terminationMessagePolicy": "FallbackToLogsOnError",
                            "command": ["restic", "snapshots", "--compact"],
                            "env": restic_env(secret_name, repo_suffix),
                            "resources": {
                                "requests": {"cpu": "10m", "memory": "64Mi"},
                                "limits": {"cpu": "200m", "memory": "256Mi"},
                            },
                        }
                    ],
                },
            },
        },
    }


def validate_report(report: dict) -> list[str]:
    """report.json のスキーマ検査 (verify #3 と同じ条件を構造として固定する)。

    戻り値は問題の説明文リスト。空なら合格。rto_seconds は「失敗対象を null で偽装しない」
    設計 (PROJECT.md 方針 6) なので、null は不合格扱いになる — 失敗した時点で report は
    書かず PROGRESS.md に事実を残すのが正しい終わり方。
    """
    problems = []
    targets = report.get("targets")
    if not isinstance(targets, list):
        return ["targets が list ではない"]
    if len(targets) < 5:
        problems.append(f"targets が 5 未満 ({len(targets)})")
    seen = set()
    for entry in targets:
        name = entry.get("name")
        if not name:
            problems.append("name を持たない target エントリがある")
            continue
        if name in seen:
            problems.append(f"target {name} が重複している")
        seen.add(name)
        rto = entry.get("rto_seconds")
        if not isinstance(rto, int) or isinstance(rto, bool) or rto < 0:
            problems.append(f"target {name}: rto_seconds が 0 以上の整数ではない ({rto!r})")
        ns = entry.get("namespace", "")
        if not ns.startswith("drill-"):
            problems.append(f"target {name}: namespace {ns!r} が drill-* ではない")
    return problems


# ---------------------------------------------------------------------------
# kubectl glue (ここから下は cluster 接続が要る)
# ---------------------------------------------------------------------------


class DrillError(RuntimeError):
    pass


def log(message: str) -> None:
    """進捗表示は stderr へ。stdout は report / dry-run manifest の JSON 専用に保つ
    (--dry-run の出力をそのままパイプで渡せるように)。"""
    print(message, file=sys.stderr)


def kubectl(args: list[str], stdin: str | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["kubectl", *args], input=stdin, text=True, capture_output=True
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def kubectl_json(args: list[str]) -> dict:
    rc, out, err = kubectl([*args, "-o", "json"])
    if rc != 0:
        raise DrillError(f"kubectl {' '.join(args)} 失敗: {err}")
    return json.loads(out)


def apply_manifest(manifest: dict) -> None:
    kind = manifest.get("kind")
    name = manifest.get("metadata", {}).get("name")
    ns = manifest.get("metadata", {}).get("namespace", "")
    rc, out, err = kubectl(["apply", "-f", "-"], stdin=json.dumps(manifest))
    if rc != 0:
        raise DrillError(f"apply 失敗 ({kind} {ns}/{name}): {err}")
    log(f"  applied: {kind} {ns}/{name}")


def preflight() -> str:
    """cluster 到達性・権限・時刻帯の確認。戻り値は whoami の SA 名。"""
    rc, out, err = kubectl(["auth", "whoami", "-o", "json"])
    if rc != 0:
        raise DrillError(f"cluster に到達できない: {err}")
    sa = json.loads(out).get("status", {}).get("userInfo", {}).get("username", "?")

    now = datetime.now(timezone.utc)
    if in_forbidden_jst_window(now):
        jst = now.astimezone(JST).strftime("%H:%M")
        raise DrillError(
            f"現在 JST {jst} は夜間帯 ({FORBIDDEN_JST_START:%H:%M}–{FORBIDDEN_JST_END:%H:%M}) "
            "内のため中断する。backup CronJob 群の帯との重なりを避ける (PROJECT.md)"
        )
    log(f"preflight OK: {sa}, JST {now.astimezone(JST):%H:%M}")
    return sa


def list_workspace_homes() -> list[str]:
    """coder namespace の workspace home PVC から workspace id を列挙する。

    ラベルは apps/coder/templates/personal/main.tf が付与するもの
    (apps/coder/workspace-home-backup-cronjob.yaml のオーケストレータと同じ selector)。
    """
    data = kubectl_json(
        [
            "get", "pvc", "-n", "coder",
            "-l", "app.kubernetes.io/name=coder-pvc",
        ]
    )
    ids = []
    for item in data.get("items", []):
        ws_id = item.get("metadata", {}).get("labels", {}).get("com.coder.workspace.id")
        if ws_id:
            ids.append(ws_id)
    if not ids:
        raise DrillError(
            "coder namespace に workspace home PVC が無い (--host <id> で復元する対象がない)"
        )
    return sorted(ids)


def wait_externalsecret_ready(namespace: str, name: str, timeout_seconds: int = 120) -> None:
    rc, _, err = kubectl(
        [
            "wait", "--for=condition=Ready",
            f"externalsecret/{name}", "-n", namespace,
            f"--timeout={timeout_seconds}s",
        ]
    )
    if rc != 0:
        raise DrillError(f"ExternalSecret {namespace}/{name} が Ready にならない: {err}")
    log(f"  secret synced: {namespace}/{name}")


def run_download_preflight(target: dict) -> None:
    """本番の restore を起こす前に、B2 からの download が今すぐ通るか確かめる。

    最小リポジトリ (syncthing) を drill-preflight namespace の使い捨て Job で
    `restic snapshots` し、**Job が Complete のときだけ「通る」と判定する。**
    それ以外はすべて中断する (fail-closed):

    - cap 超過 (403) を検出したら中断。cap はアカウント全体・日次で共有されるため、
      この状態で本番の restore 群を起こしても全 unit が activeDeadline まで
      リトライを繰り返すだけで時間と Class B transaction の予算だけを消す (実測済み)
    - cap 以外の失敗・判定不能 (pod が上がらない、deadline 内に終わらない、ログが空)
      も中断する。「syncthing 固有の問題なら他対象は復元できるはず」と続行させる案も
      あったが (旧実装)、probe の存在理由は「予算を溶ける前に止まること」であり、
      原因不明のまま全 unit を盲走させるのは本末転倒。2026-08-22 実測: 判定不能を
      WARN+続行した結果 false OK を出し、本命 run を起こしかけた。数分後に
      再試行すればよいだけのコストで済む方に倒す

    戻れば成功。それ以外は DrillError。
    """
    log("== phase 0: B2 download preflight ==")
    apply_manifest(build_namespace_manifest(PREFLIGHT_NAMESPACE))
    try:
        apply_manifest(build_externalsecret_manifest(
            PREFLIGHT_NAMESPACE, target["secret_name"]
        ))
        wait_externalsecret_ready(PREFLIGHT_NAMESPACE, target["secret_name"])
        apply_manifest(build_probe_job(target["secret_name"], target["repo_suffix"]))
        # 判定は client 側の観測窓で行う (PROBE_OBSERVE_SECONDS)。server 側の
        # backstop はこれより長いので、窓を超えた時点でも pod は生きており、
        # リトライ storm のログを採ってから後片付けできる
        observe_until = datetime.now(timezone.utc) + timedelta(
            seconds=PROBE_OBSERVE_SECONDS
        )
        state = "Running"
        while datetime.now(timezone.utc) < observe_until:
            job = kubectl_json(["get", "job", PROBE_JOB_NAME, "-n", PREFLIGHT_NAMESPACE])
            state = job_status(job)
            if state != "Running":
                break
            time.sleep(PROBE_POLL_INTERVAL_SECONDS)
        if state == "Complete":
            log("  download OK: B2 から復元できる状態")
            return
        # 生きた pod から即回収する。deadline 超過で Job コントローラに消された後は
        # ログも短時間で消える (2026-08-22 実測)
        logs = fetch_logs_tail(PREFLIGHT_NAMESPACE, PROBE_JOB_NAME)
        _, pods_state, _ = kubectl(
            ["get", "pods", "-n", PREFLIGHT_NAMESPACE,
             "-l", f"job-name={PROBE_JOB_NAME}", "-o", "wide"]
        )
        evidence = f"pods:\n{pods_state or '(なし)'}\nlogs:\n{logs or '(空)'}"
        if is_download_cap_error(logs):
            raise DrillError(
                "B2 の download cap 超過を検出した (403 download_cap_exceeded)。"
                "復元は 1 バイトも進まず全 unit がタイムアウトまでリトライするため中断する。"
                "cap は日次で回復する (無料枠の目安: 1GB/day)。--preflight-only で回復を"
                f"確認してから再実行すること。\n{evidence[:800]}"
            )
        raise DrillError(
            f"probe が {state} のまま判定できず、download 可否が不明 "
            "(cap 超過の痕跡も無い)。不明のまま本番 restore 群を起こすと盲走するため"
            f"中断する — 原因を確認して再実行すること。\n{evidence[:800]}"
        )
    finally:
        kubectl(["delete", "namespace", PREFLIGHT_NAMESPACE,
                 "--wait=true", "--ignore-not-found=true"])


def run_preflight_only() -> bool:
    """--preflight-only。phase 0 (B2 download 可否) だけを見て復元は起こさない。

    全体同時復元は B2 無料枠の日次 download 上限 (~1GB/day) を 1 回で超える規模
    (~4.2GiB、2026-08-22 実測) のため、「cap が回復したか」の確認を本命 run の起動と
    切り離せるようにする。確認ついでに誤って全 unit を起こし、中途半端に予算を溶かす
    事故を防ぐのが目的。戻り値は download 可能か。run_download_preflight が fail-closed
    のため、cap 超過でも判定不能でも False を返す (rc=2。report は書かない)。
    """
    preflight()
    try:
        run_download_preflight(TARGETS[-1])
    except DrillError as exc:
        log(f"preflight-only: {exc}")
        return False
    return True


def job_status(job: dict) -> str:
    conds = job.get("status", {}).get("conditions") or []
    for cond in conds:
        if cond.get("status") == "True":
            if cond.get("type") == "Complete":
                return "Complete"
            if cond.get("type") == "Failed":
                return "Failed"
    return "Running"


def fetch_logs_tail(namespace: str, job_name: str, tail_lines: int = 60) -> str:
    """Job の Pod ログ末尾。pod 名は job-name ラベルで探す (Job 名 ≠ Pod 名)。"""
    pods = kubectl_json(
        ["get", "pods", "-n", namespace, "-l", f"job-name={job_name}"]
    )
    chunks = []
    for item in pods.get("items", []):
        pod_name = item.get("metadata", {}).get("name")
        containers = [
            c.get("name") for c in item.get("spec", {}).get("initContainers", [])
        ] + [c.get("name") for c in item.get("spec", {}).get("containers", [])]
        for container in containers:
            rc, out, err = kubectl(
                ["logs", f"-n", namespace, pod_name, "-c", container,
                 f"--tail={tail_lines}"]
            )
            body = out or err
            if body:
                chunks.append(f"[{pod_name}/{container}]\n{body}")
    return "\n".join(chunks)


def run_drill(output_path: Path, overall_timeout: int, keep_namespaces: bool,
              dry_run: bool) -> dict:
    """本体。戻り値は report 辞書 (書き込みは呼び出し側で行う)。"""
    preflight()
    workspaces = list_workspace_homes()
    log(f"workspace homes: {len(workspaces)} 件 {[w[:8] for w in workspaces]}")
    units = expand_units(TARGETS, workspaces)

    if dry_run:
        manifests = []
        namespaces = sorted({u["namespace"] for u in units})
        for ns in namespaces:
            manifests.append(build_namespace_manifest(ns))
        for u in units:
            manifests.append(build_externalsecret_manifest(u["namespace"], u["secret_name"]))
            manifests.append(build_pvc_manifest(u["namespace"], u["pvc_name"], u["pvc_size"]))
            manifests.append(build_job_manifest(u))
        print(json.dumps(manifests, ensure_ascii=False, indent=2))
        return {"targets": [], "summary": {"dry_run": True, "units": len(units)}}

    executed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    created_namespaces = []

    # 最小リポジトリ (syncthing) で B2 からの download 可否を先に確かめる
    # (dry-run では cluster に書き込まないため実行しない)
    run_download_preflight(TARGETS[-1])

    try:
        # phase 1: 前回の残骸があれば先に消す (残骸は次の起動を「前回の中断」と誤認させる)。
        # その後 namespace + ExternalSecret を立てて、全 Secret 同期を待つ
        log("== phase 1: namespace + ExternalSecret ==")
        wanted_namespaces = sorted({u["namespace"] for u in units})
        for ns in wanted_namespaces:
            rc, _, _ = kubectl(["get", "namespace", ns])
            if rc == 0:
                log(f"  leftover {ns} を検出、先に削除する")
                rc_del, _, err = kubectl(["delete", "namespace", ns, "--wait=true"])
                if rc_del != 0:
                    raise DrillError(f"残骸 namespace {ns} の削除に失敗: {err}")
        for ns in wanted_namespaces:
            apply_manifest(build_namespace_manifest(ns))
            created_namespaces.append(ns)
        for u in units:
            apply_manifest(build_externalsecret_manifest(u["namespace"], u["secret_name"]))
        for u in units:
            wait_externalsecret_ready(u["namespace"], u["secret_name"])

        # phase 2: 全 unit をほぼ同時に起動 (apply を連打してから一括待ち)
        log("== phase 2: 全対象を同時起動 ==")
        start_times: dict[str, str] = {}
        for u in units:
            apply_manifest(build_pvc_manifest(u["namespace"], u["pvc_name"], u["pvc_size"]))
            apply_manifest(build_job_manifest(u))
        for u in units:
            pvc = kubectl_json(
                ["get", "pvc", u["pvc_name"], "-n", u["namespace"]]
            )
            # t0 は PVC 作成要求 (= API server が受理した時刻)。壁時計の起点
            start_times[u["job_name"]] = pvc["metadata"]["creationTimestamp"]

        # phase 3: 完了待ち
        log("== phase 3: 完了待ち ==")
        deadline = datetime.now(timezone.utc) + timedelta(seconds=overall_timeout)
        statuses: dict[str, dict] = {}
        while len(statuses) < len(units):
            if datetime.now(timezone.utc) > deadline:
                pending = [u["job_name"] for u in units if u["job_name"] not in statuses]
                raise DrillError(f"全体タイムアウト。未完了: {pending}")
            for u in units:
                if u["job_name"] in statuses:
                    continue
                job = kubectl_json(["get", "job", u["job_name"], "-n", u["namespace"]])
                state = job_status(job)
                if state != "Running":
                    statuses[u["job_name"]] = job
            if len(statuses) < len(units):
                log(f"  ... {len(statuses)}/{len(units)} 完了")
                time.sleep(POLL_INTERVAL_SECONDS)

        # phase 4: RTO 集計
        results_by_target: dict[str, dict] = {}
        for u in units:
            job = statuses[u["job_name"]]
            state = job_status(job)
            completion = (job.get("status") or {}).get("completionTime")
            entry = results_by_target.setdefault(
                u["target_name"],
                {"name": u["target_name"], "namespace": u["namespace"],
                 "units": [], "logs": []},
            )
            unit_result = {
                "job": u["job_name"],
                "pvc": u["pvc_name"],
                "status": state,
                "start_at": start_times[u["job_name"]],
                "completed_at": completion,
                "workspace_id": u.get("workspace_id"),
            }
            if state == "Complete" and completion:
                unit_result["rto_seconds"] = compute_rto_seconds(
                    start_times[u["job_name"]], completion
                )
                log(f"  {u['job_name']}: alive in {unit_result['rto_seconds']}s")
            else:
                log(f"  {u['job_name']}: {state}")
                unit_result["logs"] = fetch_logs_tail(u["namespace"], u["job_name"])
            entry["units"].append(unit_result)

        targets_report = []
        for target in TARGETS:
            entry = results_by_target[target["name"]]
            rtos = [x.get("rto_seconds") for x in entry["units"]]
            alive_units = sum(1 for x in entry["units"] if x["status"] == "Complete")
            all_alive = alive_units == len(entry["units"]) and all(r is not None for r in rtos)
            targets_report.append(
                {
                    "name": target["name"],
                    "namespace": entry["namespace"],
                    "kind": target["kind"],
                    "liveness_criteria": LIVENESS_CRITERIA[target["kind"]],
                    "status": "alive" if all_alive else "failed",
                    # 複数 unit (workspace-home) がある場合は最遅 unit を target の RTO とする
                    # (「全部生き返った」時刻が target の生き返りの時刻)
                    "rto_seconds": max(rtos) if all_alive else None,
                    "units": entry["units"],
                }
            )

        report = {
            "project": "P-0080",
            "executed_at_utc": executed_at,
            "method": "ops/drills/restore_drill.py — 全 backup 対象を drill-* namespace の新規 PVC へ "
                      "同時復元。RTO = PVC 作成要求 (PVC creationTimestamp) から liveness 合格 "
                      "(Job completionTime) までの壁時計。credential は append-only 鍵のみ",
            "targets": targets_report,
            "summary": {
                "total_targets": len(targets_report),
                "alive": sum(1 for t in targets_report if t["status"] == "alive"),
            },
        }
        problems = validate_report(report)
        report["summary"]["schema_problems"] = problems
        return report
    finally:
        if not keep_namespaces and created_namespaces:
            log("== cleanup: drill namespace を削除 ==")
            for ns in created_namespaces:
                rc, _, err = kubectl(["delete", "namespace", ns, "--wait=true"])
                log(f"  delete {ns}: {'ok' if rc == 0 else 'FAILED: ' + err}")


def write_report(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"report written: {path}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT_PATH,
                        help="report.json の出力先 (default: ops/projects/logs/P-0080/report.json)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_OVERALL_TIMEOUT_SECONDS,
                        help="全体タイムアウト秒 (default: 1800)")
    parser.add_argument("--keep-namespaces", action="store_true",
                        help="後片付け (namespace delete) を省略する (デバッグ用)")
    parser.add_argument("--dry-run", action="store_true",
                        help="cluster に触れず manifest を stdout に出して終了")
    parser.add_argument("--preflight-only", action="store_true",
                        help="phase 0 (B2 download 可否の確認) だけを行い、restore 群は起こさず終了。"
                             "cap 回復待ちの確認用。全体復元は無料枠の日次 download 上限を "
                             "1 回で超えるため、安易な再実行は予算を溶かす")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.preflight_only:
        try:
            ok = run_preflight_only()
        except DrillError as exc:
            print(f"PREFLIGHT FAILED: {exc}", file=sys.stderr)
            return 1
        if not ok:
            return 2
        log("PREFLIGHT OK: B2 からの download が今すぐ通る状態。"
            "ただし全体同時復元 (~4.2GiB) は無料枠の日次上限を超える — 再実行の判断は "
            "PROGRESS.md の引き継ぎを読むこと")
        return 0

    try:
        report = run_drill(args.output, args.timeout, args.keep_namespaces, args.dry_run)
    except DrillError as exc:
        print(f"DRILL FAILED: {exc}", file=sys.stderr)
        return 1

    failed = [
        t["name"] for t in report.get("targets", []) if t.get("status") != "alive"
    ]
    if report.get("summary", {}).get("dry_run"):
        return 0
    if failed:
        # 失敗対象を隠さない (PROJECT.md 方針 6)。report は書くが verify 条件は満たさない
        write_report(report, args.output)
        print(f"DRILL INCOMPLETE: 生き返らなかった対象 {failed}", file=sys.stderr)
        return 2
    write_report(report, args.output)
    log("DRILL COMPLETE: 全対象が生き返った")
    return 0


if __name__ == "__main__":
    sys.exit(main())
