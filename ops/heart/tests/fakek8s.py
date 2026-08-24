"""ビート結合テスト用の、記憶だけの k8s (設計 state-out-of-git 4b-2)。

プロジェクトの正が Project CR に移ったので、beat() は k8s が居ないと 1 行目で
止まる。ここは CR / HeartState を dict に溜めるだけの器で、server-side apply を
「名前で置き換える」に単純化してある。
"""

from ops.heart import heartstate, projectcr


class FakeK8s:
    def __init__(self, projects=(), scalars=None):
        self.custom = {}
        self.leases = {}
        for p in projects:
            self.apply_custom(
                projectcr.API_VERSION, "autopilot", projectcr.PLURAL,
                projectcr.cr_name(p["id"]),
                projectcr.to_cr(p, "autopilot", ("delivered", "stalled", "vetoed",
                                                 "rejected")),
            )
        if scalars:
            self.apply_custom(
                projectcr.API_VERSION, "autopilot", heartstate.PLURAL,
                heartstate.NAME, heartstate.to_cr("autopilot", scalars),
            )
        self.applied = []

    # --- Project / HeartState ---
    def list_custom(self, api_version, namespace, plural, label_selector=None):
        items = [v for (p, _), v in sorted(self.custom.items()) if p == plural]

        def label(item, key):
            return ((item.get("metadata") or {}).get("labels") or {}).get(key)

        # 本物の API と同じく **サーバ側**で絞る。手元で捨てる実装にすると、
        # selector を渡し忘れた読み手をテストが見逃す
        if label_selector == projectcr.NOT_REJECTED_SELECTOR:
            items = [i for i in items if label(i, "state") != "rejected"]
        elif label_selector == projectcr.REJECTED_SELECTOR:
            items = [i for i in items if label(i, "state") == "rejected"]
        elif label_selector == projectcr.LIVE_SELECTOR:
            items = [i for i in items if label(i, "lifecycle") == projectcr.LIVE]
        return items

    def get_custom(self, api_version, namespace, plural, name):
        try:
            return self.custom[(plural, name)]
        except KeyError:
            raise RuntimeError(f"k8s API 404: {plural}/{name}") from None

    def apply_custom(self, api_version, namespace, plural, name, body):
        self.custom[(plural, name)] = body
        if hasattr(self, "applied"):
            self.applied.append(name)
        return body

    def projects(self):
        return projectcr.working_set(
            self.list_custom(projectcr.API_VERSION, "autopilot", projectcr.PLURAL)
        )

    def scalars(self):
        return heartstate.from_cr(self.custom.get((heartstate.PLURAL, heartstate.NAME)))

    def apply_lease(self, namespace, name, body):
        self.leases[name] = body
        return body

    # --- ビートが触る残り (どれもこのテストの本題ではない) ---
    def list_jobs(self, *a, **k):
        return []

    def create_job(self, *a, **k):
        return {}

    def delete_job(self, *a, **k):
        return {}

    def get_configmap(self, *a, **k):
        return {}

    def list_pods(self, *a, **k):
        return []
