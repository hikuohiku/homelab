# 状態ブランチを消してよいか — 判定チェックリスト

対象: `ops-state` / `ops-health-report` / `ops-feedback` / `ops-dashboard`

`git push --delete` は不可逆で、所有者の手番。このファイルは**押す前に何を実測すれば
「消しても何も壊れない」と言えるか**を、コマンドまで含めて書いたもの。
リポジトリ側の参照は別に消してあるので、ここで見るのは**実機**だけ。

判定の原則: **読めない・分からないは「消してよい」に倒さない。** 1 つでも落ちたら押さない。

---

## 0. 前提 — リポジトリ側の参照が閉じているか

CI が緑であることに加えて、機械が枝を触る口が残っていないことを見る。

```bash
git fetch origin --prune
# 書き手・読み手のコードに枝の名前が残っていないこと (歴史を語るコメントは除く)
grep -rn "ops-state\|ops-feedback\|ops-health-report" \
  --include='*.py' --include='*.go' --include='*.ts' --include='*.tsx' \
  --include='*.yaml' --include='*.yml' --include='*.sh' . \
  | grep -v 'ops-health-reporter' | grep -v 'configmaps/ops-health-report'
```

残っていてよいのは「以前は〜だった」という記録だけ。`envOr(...)` の既定値・
`git fetch origin <branch>`・`ensure_branch` が出てきたら、そこが生きた口。

未 merge の段があるうちは押さない。段ごとの担当は
[`architecture.md`](architecture.md) の「段階」表を見る。

---

## 1. `ops-state` — 心臓が git を触っていないこと

```bash
# (a) heart が最後に枝へ書いた時刻。4b-2b の merge より後の commit があってはいけない
git log -1 --format='%cI %s' origin/ops-state

# (b) heart が生きていて、Lease が進んでいる (= ビートが最後まで通っている)
kubectl -n autopilot get lease autopilot-heart -o jsonpath='{.spec.renewTime}{"\n"}'
sleep 300
kubectl -n autopilot get lease autopilot-heart -o jsonpath='{.spec.renewTime}{"\n"}'
#   → 2 回目が進んでいること。進まなければ heart が止まっているので、
#     枝の削除どころではない

# (c) プロジェクトの正が CR にあり、非終端が居る
kubectl -n autopilot get projects -l lifecycle=live
kubectl -n autopilot get projects --no-headers | wc -l
#   → 件数が枝の projects.json + 台帳の id 数以上あること。下の (d) で厳密に見る

# (d) 取りこぼしゼロ。heart が移行を見送っていないこと
kubectl -n autopilot logs deploy/autopilot-heart --since=24h \
  | grep -E 'Project CR に .* 取りこぼし|projects.json を PVC へ移した'
#   → 「取りこぼしがある」が出ていないこと。
#     「PVC へ移した」が 1 回出ていれば移行は完了している

# (e) CR の書き込みが失敗し続けていないこと
kubectl -n autopilot logs deploy/autopilot-heart --since=24h \
  | grep -c 'project CR apply failed'
#   → 0。非 0 なら CRD のスキーマに穴がある (heart.note_cr_failures)

# (f) バックアップに乗っていること。**枝を消す前にここだけは必ず見る**
kubectl -n autopilot-projects-backup get cronjob
kubectl -n autopilot-projects-backup get jobs \
  -o custom-columns=NAME:.metadata.name,SUCCEEDED:.status.succeeded,END:.status.completionTime
#   → 直近 24h に succeeded=1 の Job があること。
#     復元手順は docs/backup.md「Project CR の restic バックアップ」
```

**(f) が緑でないうちは押さない。** 枝を消した時点で、プロジェクトの記録の写しは
クラスタと B2 の 2 箇所だけになる。

---

## 2. `ops-health-report` — 健全性レポートが ConfigMap 経路で回っていること

```bash
# (a) 書き手 (CronJob) が ConfigMap を更新している
kubectl -n autopilot get configmap ops-health-report \
  -o jsonpath='{.data.latest\.json}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["generated_at"])'
#   → 60 分以内。CronJob は 30 分ごと

# (b) version_drift が枝ではなく ConfigMap 経由で載っている
kubectl -n version-watcher get configmap version-drift \
  -o jsonpath='{.data.report\.json}' | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["generated_at"], d["summary"])'
kubectl -n autopilot get configmap ops-health-report \
  -o jsonpath='{.data.latest\.json}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["version_drift"]["status"])'
#   → ok。stale / no_data なら夜間 CronJob (37 2 * * *) がまだ 1 度も
#     新しい形で走っていない。1 晩待って見直す

# (c) 読み手が両方とも ConfigMap を見ている
kubectl -n autopilot logs deploy/autopilot-heart --since=1h | grep -i 'health'
kubectl -n autopilot get deploy autopilot-core \
  -o jsonpath='{.spec.template.spec.containers[*].env[?(@.name=="CORE_HEALTH_CONFIGMAP")].value}{"\n"}'
#   → ops-health-report (ConfigMap 名。枝ではない)

# (d) 枝への push が止まっている
git log -1 --format='%cI %s' origin/ops-health-report
#   → version-watcher の ConfigMap 化以降、新しい commit が無いこと
```

---

## 3. `ops-feedback` — 書き置きがバス経路だけで届いていること

**Phase 6b / 7b が merge されるまで、この枝は生きている。** 下は両方が入った後の判定。

```bash
# (a) 書き手が枝を触っていない
git log -1 --format='%cI %s' origin/ops-feedback

# (b) ダッシュボードのフォームから 1 件書いて、heart の台帳に着くこと (実地試験)
#     tailnet のブラウザで https://ops-dashboard.tailae6c2.ts.net/ を開き、
#     書き置きに "deletion-drill <日付>" と入れて送信する。その後:
kubectl -n autopilot logs deploy/autopilot-heart --since=10m | grep -i feedback
kubectl -n autopilot exec deploy/autopilot-heart -- \
  sh -c 'cat /data/feedback-bus/inbox/*.json' | grep deletion-drill
#   → 数分以内に届いていること。届かなければバス経路が繋がっていない

# (c) Telegram からの発話も同じ経路に乗ること
#     所有者の端末から DM を 1 通送り、(b) と同じ場所に現れることを見る
kubectl -n telegram-adapter logs deploy/telegram-adapter --since=10m
#   → GitHub Contents API への PUT が出ていないこと

# (d) NATS が受けている
kubectl -n nats get pvc nats-jetstream
kubectl -n autopilot logs deploy/autopilot-heart -c bus-sidecar --since=1h | tail
```

**原本の扱い**: 枝の `ops/feedback/inbox/` は人間が書いた原本で、CHARTER §5 が
「消さない・書き換えない」と縛っている。枝ごと消すなら、その前に inbox を
手元へ 1 度落として保管すること。

```bash
git fetch origin ops-feedback
git archive origin/ops-feedback ops/feedback/inbox/ | tar -x -C ~/feedback-inbox-archive/
```

---

## 4. `ops-dashboard` — 遺物であること

書き手だった `ops/dashboard/build.py` は退役済みで、リポジトリに存在しない。

```bash
git log -1 --format='%cI %s' origin/ops-dashboard
#   → 2026-08-22 で止まっていること

# 人間が見ている画面が Mission Control であること
kubectl -n autopilot get deploy ops-dashboard
curl -sI https://ops-dashboard.tailae6c2.ts.net/ | head -1   # tailnet のノードから
```

`ops/state.json` の `dashboard.ops_dashboard_url` がこの Deployment を指していれば、
枝は誰も見ていない。**4 本のうち、これが一番安全に消せる。**

---

## 5. 押す

```bash
git push origin --delete ops-dashboard
git push origin --delete ops-health-report
git push origin --delete ops-feedback
git push origin --delete ops-state
```

消した後に必ず見るもの:

```bash
# heart が枝の不在で落ちていないこと (5 ビート = 約 10 分)
kubectl -n autopilot logs deploy/autopilot-heart --since=15m | grep -iE 'error|traceback'
kubectl -n autopilot get lease autopilot-heart -o jsonpath='{.spec.renewTime}{"\n"}'

# 採択ゲートの使い捨て clone が通ること (blobless clone は ref を全部生やす)
kubectl -n autopilot logs deploy/autopilot-heart --since=1h | grep -i 'gate'
```

**復旧**: 消した枝は 90 日以内なら GitHub の API から SHA で復元できる。
削除直前の SHA を控えておくこと。

```bash
git rev-parse origin/ops-state origin/ops-health-report origin/ops-feedback origin/ops-dashboard
```
