"""recovery-canary の毎晩の復旧計測記録の集約 (P-0258)。

「node01 が消えたら / ArgoCD が止まったら」系の復旧演習を 1 回の勇気ではなく
毎晩の時計にする装置。産出側 (namespace recovery-canary の CronJob
recovery-canary-probe、apps/recovery-canary/cronjob.yaml に埋め込み) が
canary Deployment を削除し selfHeal 復帰までの壁時計秒を測って、専用 ConfigMap
`recovery-probe` の report.json キーへ書く。このモジュールはその記録を
latest.json / history jsonl の `recovery_probe` キーの形へ要約する純関数群で、
クラスタやネットワークに触れない。

- 産出側が書く成功レコード: {schema, tool, project, generated_at, ok: true,
  deleted_at, namespace, deployment, last_recovery_seconds: int, ready_at}
- 失敗レコードは ok: false で last_recovery_seconds を持たず、代わりに
  phase (delete / wait-deletion / wait-recreate / wait-ready) と error
  (人間向け文面) を持つ。秒数の捏造をしないため

download_budget.py と同じく report.py と同じく標準ライブラリのみで動く。
import 副作用を持たない (report.py と違い ServiceAccount token を読まないので、
cluster 外の unit test から importlib で直接ロードできる)。
"""

import datetime

# 産出側 CronJob recovery-canary-probe が書く専用 ConfigMap のある namespace。
# canary 一式は本体アプリから完全分離された専用 namespace (DoD 3)
RECOVERY_PROBE_NAMESPACE = "recovery-canary"

# 日次 CronJob (毎晩 03:43 JST) の 1 回分より長く沈黙していたら「装置が回って
# いない」(stale)。dashboard-smoke (P-0193) と同じ考え方: 24h + 2h マージン。
# reporter run は 30 分毎なので 1 日落ちで確実に拾う
STALE_AFTER_S = 26 * 3600

# 失敗記録の error (人間向け文面) の切り詰め上限。1 行 1 レポートの history jsonl
# 膨張止め (collect_externalsecrets の message / _dashboard_smoke_summary の
# detail と同じ上限)
ERROR_LIMIT = 200


def parse_utc(value):
    """産出側の iso() 書式 (%Y-%m-%dT%H:%M:%SZ) を aware datetime へ。

    厳格に: 他の書式・非文字列は None (例外を出さない。呼び出し側で
    帳簿の壊れとして扱う)。
    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc
        )
    except ValueError:
        return None


def coerce_seconds(value):
    """last_recovery_seconds の検査。0 以上の int のみ受け付ける。

    bool は int の派生なので明示的に弾く (download_budget.coerce_bytes と同じ
    倒し方)。負値は deleted_at/ready_at の時計矛盾なので不正。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def build_summary(payload, now):
    """ConfigMap の report.json (産出側ランナーの build_report() 戻り値) を
    latest.json / history jsonl に載せる要約へ変える (純関数)。

    status:
      ok       最新の夜間実行が復旧を計測できた — 記録のみで通知予算は消費しない。
               last_recovery_seconds (int) を載せる唯一の status
      fail     産出側が失敗記録を残した — 上限時間内に復旧しない等。
               秒数は載せず詰まった段階 (phase) を見せる
      stale    最終記録が STALE_AFTER_S より古い — 装置自身が沈黙

    形が契約通りでない場合 (ok が真偽値でない等) は ValueError — 呼び出し側
    (report.py collect_recovery_probe) が no_data エントリへ落とす。壊れた
    記録を黙って fail 扱いにすると「装置の故障」と「帳簿の壊れ」が区別できなく
    なるため (_dashboard_smoke_summary と同じ思想)。
    """
    if not isinstance(payload, dict):
        raise ValueError("report.json が dict でない")
    ok = payload.get("ok")
    if not isinstance(ok, bool):
        raise ValueError("report.json の ok が真偽値でない")
    generated_at = payload.get("generated_at")
    generated = parse_utc(generated_at)
    if generated is None:
        raise ValueError(
            "report.json の generated_at を解釈できない: {!r}".format(generated_at)
        )
    age_seconds = int((now - generated).total_seconds())

    # 鮮度を最優先で判定する: 古い記録が何を指していようまず「装置の沈黙」を
    # 報せる (_dashboard_smoke_summary と同じ倒し方)。ちょうど上限では鳴らさない
    if age_seconds > STALE_AFTER_S:
        return {
            "status": "stale",
            "reason": (
                "最終記録が {} 秒前 (> 上限 {} 秒) — "
                "CronJob recovery-canary-probe が沈黙している疑い"
            ).format(age_seconds, STALE_AFTER_S),
            "ok": ok,
            "generated_at": generated_at,
            "age_seconds": age_seconds,
        }

    if not ok:
        # 失敗記録から秒数は載せない。産出側が誤って持たせていても無視する —
        # 「失敗した夜」に秒数が並ぶと履歴が嘘をつく
        phase = payload.get("phase")
        if not isinstance(phase, str) or not phase.strip():
            phase = None
        error = payload.get("error")
        error = error.strip()[:ERROR_LIMIT] if isinstance(error, str) and error.strip() else None
        reason = "前夜の実行が失敗記録を残した: phase={}".format(
            phase or "unknown"
        )
        if error:
            reason += " — {}".format(error)
        out = {
            "status": "fail",
            "reason": reason,
            "ok": False,
            "generated_at": generated_at,
            "age_seconds": age_seconds,
        }
        if phase:
            out["phase"] = phase
        if error:
            out["error"] = error
        return out

    seconds = coerce_seconds(payload.get("last_recovery_seconds"))
    if seconds is None:
        raise ValueError(
            "ok=true のレコードなのに last_recovery_seconds が 0 以上の int でない: {!r}".format(
                payload.get("last_recovery_seconds")
            )
        )
    deleted_at = payload.get("deleted_at")
    ready_at = payload.get("ready_at")
    out = {
        "status": "ok",
        "reason": "{} 秒で Ready 復帰 ({} 秒前の実測)".format(seconds, age_seconds),
        "ok": True,
        "last_recovery_seconds": seconds,
        "generated_at": generated_at,
        "age_seconds": age_seconds,
        "deleted_at": deleted_at if isinstance(deleted_at, str) else None,
        "ready_at": ready_at if isinstance(ready_at, str) else None,
    }
    return out
