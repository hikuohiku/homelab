"""heart の生存を示す Lease の組み立て (設計 state-out-of-git Phase 7)。

**プロセスの生死ではなくビートの鮮度を示す。** renewTime を進めるのはビートが
最後まで通ったときだけで、liveness probe のように「返事ができる」ことでは進まない。
P-0027 の事故は「止まったまま死んだ」— プロセスは生きているのにループが回って
いなかった — なので、生きていることを示す面をプロセス側に持たせてはいけない。

読み手はコア (apps/autopilot-core の driver)。renewTime が
ops/rules.json の heartbeat.stale_seconds より古ければ Telegram で人間に言う。
git は一切経由しない。

ここは純関数だけ。k8s を叩くのは呼び出し側 (heart.beat)。
"""

API_VERSION = "coordination.k8s.io/v1"
KIND = "Lease"
PLURAL = "leases"

# Lease の名前。読み手 (core の CORE_HEART_LEASE_NAME) と揃えること
NAME = "autopilot-heart"

# beat 番号を人が見るための注記。判定には使わない (判定は renewTime だけ)
BEAT_ANNOTATION = "autopilot.homelab.hikuohiku.dev/beat"


def to_lease(namespace, holder, beat, now, stale_seconds, name=NAME):
    """1 ビート分の Lease を返す。

    leaseDurationSeconds に閾値をそのまま載せるのは、`kubectl get lease` を見た
    人間が「どれだけ空いたら異常か」を CR の上で読めるようにするため。判定の
    単一情報源は ops/rules.json のままで、ここはその写し。
    """
    return {
        "apiVersion": API_VERSION,
        "kind": KIND,
        "metadata": {
            "name": name,
            "namespace": namespace,
            "annotations": {BEAT_ANNOTATION: str(beat)},
        },
        "spec": {
            "holderIdentity": holder,
            "leaseDurationSeconds": int(stale_seconds),
            "renewTime": now,
        },
    }
