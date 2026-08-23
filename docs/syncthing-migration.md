# syncthing 移行手順 — 旧 LXC 101 からクラスタへ (T-0140 / P-0163)

クラスタで動く実サービスの中で唯一、実データが旧 Proxmox LXC **101** (syncthing) に
置き去りのままのものが syncthing。device ID の素になる `cert.pem` / `key.pem` を移さないと
既存ピアから「別デバイス」に見え、同期方向次第で削除が伝播しうる。この手順は identity を込めた
移行を、人間の作業を実質 **tar 1 コマンド + 検証コマンド 1 回**まで削り込んだ台本である。

すべての操作は**可逆**に設計してあり、失敗時は「ロールバック」節で移行前と同じ状態に戻せる。
**LXC 101 側は読み取りのみで、検証が合格するまで停止しない。**

## 全体像

| 手順 | やること | 打つ場所 |
|---|---|---|
| A | LXC 101 から `/var/lib/syncthing` 一式を tar で取り出す | Proxmox ホスト |
| B | アーカイブを PVC (`syncthing-data`) へ配置する | 作業マシン (kubectl) |
| C | 受け入れ検証 (`check --strict` + `exercise`) を 1 回回す | 作業マシン (kubectl) |
| D | 片付け | 作業マシン |

## 前提

- リポジトリの checkout (この手順は `ops/tools/syncthing_acceptance.py` と
  `apps/syncthing/restic-backup-cronjob.yaml` を読むので main 最新であること) と、
  管理者権限の kubeconfig
- Proxmox ホストへの到達 (`pct` コマンド)
- 移行先の器は既にある: PVC `syncthing-data` (20Gi, local-path)、Deployment `syncthing`
  (`/var/syncthing` に mount、PUID/PGID 1000)、GUI Service `syncthing.syncthing.svc:8384`、
  tailnet sync Service `syncthing-sync` (22000 TCP/UDP + 21027 UDP)
- 現在クラスタで稼働中の syncthing は**新規インストール** (別 device ID、データ無し)。
  移行とは「この新規インストールを旧 LXC 101 と同一の identity で置き換える」こと。
  元の状態は配置スクリプトが `.pristine` ディレクトリに待避し、いつでも戻せる
- node01 単ノードのため、検証 Job が本体と同時に同じ local-path PVC を mount しても成立する
  (RWO だが local-path は node ローカルの bind mount)
- データ量の目安: 同期データ合計が PVC 容量 (20Gi) を超える見込みなら、この手順の前に
  容量拡張 (`docs/node01-storage.md`) を済ませること

## 手順 A — 取り出し (人間のコア作業)

まずパスの事前確認 (想定パスは `/var/lib/syncthing` — Debian パッケージ既定だが**要確認**):

```bash
pct exec 101 -- sh -c 'ls -l /var/lib/syncthing/cert.pem && grep -o "path=\"[^\"]*\"" /var/lib/syncthing/config.xml'
```

- `cert.pem` が出ればパスは正しい。grep の出力 `<folder path=...>` が**同期フォルダの所在**
- 見つからなければ `pct exec 101 -- find / -maxdepth 5 -name cert.pem 2>/dev/null` で探し、
  下のコマンドの `-C` 引数を読み替える

取り出し本体 (**1 コマンド**。実行したディレクトリに `syncthing-101.tar.gz` ができる):

```bash
pct exec 101 -- tar -C /var/lib/syncthing -czf - . > syncthing-101.tar.gz
```

> 同期フォルダが旧 HOME (`/var/lib/syncthing`) の**外**にある場合 (上の grep で判別) は
> その分だけ tar に足す。例: フォルダが `/srv/sync` なら
> `pct exec 101 -- tar -C / -czf - var/lib/syncthing srv/sync > syncthing-101.tar.gz`
> (`-C /` に変わる点に注意)。HOME 配下ならこのままでよい

LXC から作業マシンへ運ぶ:

```bash
scp root@<proxmox-host>:~/syncthing-101.tar.gz .
```

LXC 101 側は何も変更していない。この時点でロールバック不要なのは自明。

## 手順 B — PVC への配置 (コピペブロック)

以降はリポジトリの checkout で `cd` しておくこと。アーカイブもカレントにあるものとする。

### 共通: 作業用 Pod

配置とロールバックで使い回す一時 Pod。PVC を読み書きできる位置で tar/chown/sed を行う。

```bash
kubectl -n syncthing delete pod syncthing-migrate --ignore-not-found
kubectl -n syncthing apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: syncthing-migrate
  namespace: syncthing
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  containers:
    - name: migrate
      # 新規 pull を避けるため、既に inventory 監視対象として node 上にあるイメージを流用
      image: python:3.12-alpine
      command: ["sh", "-c", "exec sleep 3600"]
      # chown/cp -a (属性維持) を行うため root 起動。権限昇格は禁止し、
      # 必要最小限の CHOWN/FOWNER/DAC_OVERRIDE のみ付与 (docs/backup.md 復元試験の教訓)
      securityContext:
        allowPrivilegeEscalation: false
        capabilities:
          drop: ["ALL"]
          add: ["CHOWN", "FOWNER", "DAC_OVERRIDE"]
      volumeMounts:
        - name: data
          mountPath: /var/syncthing
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: syncthing-data
EOF
kubectl -n syncthing wait --for=condition=ready pod/syncthing-migrate --timeout=180s
```

### 配置の実行

```bash
# 1) 本体を止める (identity 入れ替え中の二重起動・設定上書き合いを避ける)
kubectl -n syncthing scale deploy/syncthing --replicas=0
kubectl -n syncthing wait --for=delete pod -l app=syncthing --timeout=120s || true

# 2) 作業用 Pod を起こして (共通ブロック)、アーカイブを送り込む
kubectl -n syncthing cp syncthing-101.tar.gz syncthing-migrate:/tmp/staging.tgz

# 3) 配置スクリプトを実行
kubectl -n syncthing exec -i syncthing-migrate -- sh -s <<'SCRIPT'
set -eu
DST=/var/syncthing
UNPACK=/tmp/unpack
OLD_HOME=/var/lib/syncthing

mkdir -p "$UNPACK"
tar -xzf /tmp/staging.tgz -C "$UNPACK"

# 展開物が旧 HOME 直下であることを確認 (手順 A の tar -C と対応)
test -f "$UNPACK/cert.pem" \
  || { echo "FATAL: cert.pem が展開されていない。tar の -C パスを確認"; exit 1; }

# 置き場所: 推測で決めず、今の cert.pem の位置から検出する。
# flat = PVC 直下 (/var/syncthing/cert.pem)、nested = config/ 配下。
# P-0047 の実測では nested。どちらでも受け入れ検査ツールは自動判別する
if [ -f "$DST/config/cert.pem" ]; then CONF="$DST/config"; else CONF="$DST"; fi
echo "layout: $(test "$CONF" = "$DST/config" && echo nested || echo flat) -> $CONF"

# ロールバック点: 新規インストール側の identity/index を mv で待避 (所有権ごと保持)。
# 2 回目以降の実行では既にあるので触らない
if [ ! -d "$DST/.pristine" ]; then
  mkdir -p "$DST/.pristine"
  for f in cert.pem key.pem config.xml config.xml.v0 https-cert.pem https-key.pem syncthing.lock; do
    test -e "$CONF/$f" && mv "$CONF/$f" "$DST/.pristine/" || true
  done
  test -d "$CONF/index-v2" && mv "$CONF/index-v2" "$DST/.pristine/" || true
  echo "$(test "$CONF" = "$DST/config" && echo nested || echo flat)" > "$DST/.pristine/LAYOUT"
fi

# identity (+あれば https 証明書) を配置。cp -a で属性を維持
for f in cert.pem key.pem config.xml https-cert.pem https-key.pem; do
  test -f "$UNPACK/$f" && cp -a "$UNPACK/$f" "$CONF/$f" || true
done
test -f "$CONF/config.xml" || { echo "FATAL: 展開物に config.xml が無い"; exit 1; }

# folder path の張り替え: 旧 HOME を新 HOME へ (フォルダが旧 HOME 配下の場合)
sed -i "s|$OLD_HOME|$DST|g" "$CONF/config.xml"
# GUI 待ち受け: LXC では 127.0.0.1 バインドが既定。Service/liveness probe のために
# 全 interface へ張り替える (これを忘れると移行直後に probe 失敗で再起動を繰り返す)
sed -i "s|127\.0\.0\.1:8384|0.0.0.0:8384|g" "$CONF/config.xml"

# 同期フォルダの中身を新 HOME へ。フォルダ名は任意なので identity と
# 再生成物 (index* / lock) 以外の残り全部を持ってくる
find "$UNPACK" -mindepth 1 -maxdepth 1 \
  ! -name 'cert.pem' ! -name 'key.pem' ! -name 'config.xml' ! -name 'config.xml.v0' \
  ! -name 'https-cert.pem' ! -name 'https-key.pem' \
  ! -name 'index' ! -name 'index-v2' ! -name 'syncthing.lock' \
  -exec cp -a {} "$DST/" \;

# 所有権を PUID/PGID (1000:1000) に揃え、鍵のパーミッションを保証
chown -R 1000:1000 "$DST"
chmod 600 "$CONF/key.pem" 2>/dev/null || true
chmod 600 "$CONF/https-key.pem" 2>/dev/null || true

echo "--- 張り替え後の folder 定義 (旧 HOME 外のフォルダはデータ未到着なので要確認) ---"
grep -o 'path="[^"]*"' "$CONF/config.xml" | head -20
echo "--- 配置後の一覧 ---"
ls -la "$CONF"
SCRIPT

# 4) 作業用 Pod を片付け、本体を戻す
kubectl -n syncthing delete pod syncthing-migrate
kubectl -n syncthing scale deploy/syncthing --replicas=1
kubectl -n syncthing rollout status deploy/syncthing --timeout=300s
```

スクリプト末尾の出力を確認すること:

- `layout:` 行が flat / nested のどちらでも、以降の手順は同じでよい (ツールが追従する)
- 「張り替え後の folder 定義」に出た各 path が、tar に入れた同期データと一致しているか。
  旧 HOME 外のフォルダを tar に入れ忘れた場合ここでズレが分かる

## 手順 C — 受け入れ検証 (1 回)

検証は**クラスタ内の Job で実行する**。`check` の pvc-rw / `exercise` の書き込み演習は
PVC への実書き込みを要するため、port-forward では代替できない (REST しか通らない)。
Job 内なら Service DNS も解け、ツールの既定値 (`--gui-url syncthing.syncthing.svc:8384`,
`--sync-addr syncthing-sync.syncthing.svc:22000`) がそのまま正になる。

```bash
# 検査スクリプトと restic マニフェストを ConfigMap 経由でクラスタに持ち込む
kubectl -n syncthing delete configmap syncthing-acceptance --ignore-not-found
kubectl -n syncthing create configmap syncthing-acceptance \
  --from-file=syncthing_acceptance.py=ops/tools/syncthing_acceptance.py \
  --from-file=restic-backup-cronjob.yaml=apps/syncthing/restic-backup-cronjob.yaml

kubectl -n syncthing delete job syncthing-acceptance --ignore-not-found
kubectl -n syncthing apply -f - <<'EOF'
apiVersion: batch/v1
kind: Job
metadata:
  name: syncthing-acceptance
  namespace: syncthing
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 900
  template:
    metadata:
      labels:
        app: syncthing-acceptance
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      containers:
        - name: acceptance
          image: python:3.12-alpine
          command: ["sh", "-c"]
          args:
            - |
              set -eu
              # check (--strict) が通ったときだけ exercise を回す
              python3 /work/syncthing_acceptance.py check \
                --data-dir /var/syncthing --strict \
                --restic-manifest /work/restic-backup-cronjob.yaml \
              && python3 /work/syncthing_acceptance.py exercise \
                --data-dir /var/syncthing \
                --restic-manifest /work/restic-backup-cronjob.yaml
          # uid/gid 1000 (= PUID/PGID) で動かすことで、pvc-rw 検査は
          # 「本番 Pod と同じ権限で読み書きできるか」をそのまま試すことになる
          securityContext:
            runAsUser: 1000
            runAsGroup: 1000
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
          volumeMounts:
            - name: work
              mountPath: /work
            - name: data
              mountPath: /var/syncthing
      volumes:
        - name: work
          configMap:
            name: syncthing-acceptance
        - name: data
          persistentVolumeClaim:
            claimName: syncthing-data
EOF

kubectl -n syncthing wait --for=condition=complete job/syncthing-acceptance --timeout=15m \
  || kubectl -n syncthing logs job/syncthing-acceptance
kubectl -n syncthing logs job/syncthing-acceptance
```

**合格条件**: ログの最後が exercise の `判定: 合格` で終わること (check → exercise の順に
2 つの「判定: 合格」が出る。`set -eu` なので check が落ちた時点で exercise は回らない)。
`exercise` 中は稼働中インスタンスにダミーフォルダ `acceptance-dummy` が一時登録されるが、
終了時に必ず削除される (フォルダ登録後の初回 scan で syncthing が掘る `.stfolder` も
含めて丸ごと消す。本番データには触れない)。

## 手順 D — 片付け

検証が通ったら:

```bash
kubectl -n syncthing delete job syncthing-acceptance
kubectl -n syncthing delete configmap syncthing-acceptance
```

`.pristine` ディレクトリ (新規インストール時の identity 待避) は数日観察して同期が安定してから
消す (手順 D の最後でよい)。消し方は本体 Pod と同時でも安全だが、確実を取るなら:

```bash
kubectl -n syncthing scale deploy/syncthing --replicas=0
kubectl -n syncthing apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: syncthing-migrate
  namespace: syncthing
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  containers:
    - name: migrate
      image: python:3.12-alpine
      command: ["sh", "-c", "rm -rf /var/syncthing/.pristine && echo removed"]
      securityContext:
        allowPrivilegeEscalation: false
        capabilities: { drop: ["ALL"], add: ["DAC_OVERRIDE"] }
      volumeMounts:
        - { name: data, mountPath: /var/syncthing }
  volumes:
    - name: data
      persistentVolumeClaim: { claimName: syncthing-data }
EOF
kubectl -n syncthing wait --for=condition=ready pod/syncthing-migrate --timeout=180s \
  && kubectl -n syncthing logs syncthing-migrate
kubectl -n syncthing delete pod syncthing-migrate
kubectl -n syncthing scale deploy/syncthing --replicas=1
```

restic / Backblaze B2 側は何もしなくてよい (backup 対象パス `/mnt/syncthing-data` は不変)。

## 失敗したとき — ロールバック

巻き戻す対象はクラスタ側だけで、LXC 101 は最初から無傷である。手順 B が作った
`.pristine` 待避を戻せば、移行前 (新規インストール・別 device ID) と完全に同じ状態になる。

```bash
kubectl -n syncthing scale deploy/syncthing --replicas=0
kubectl -n syncthing delete pod syncthing-migrate --ignore-not-found
kubectl -n syncthing apply -f - <<'EOF'
(手順 B の「共通: 作業用 Pod」と同一の YAML をここに貼る)
EOF
kubectl -n syncthing wait --for=condition=ready pod/syncthing-migrate --timeout=180s

kubectl -n syncthing exec -i syncthing-migrate -- sh -s <<'SCRIPT'
set -eu
DST=/var/syncthing
PR="$DST/.pristine"
if [ ! -d "$PR" ]; then echo "待避が無い — 配置が行われていないので巻き戻すものも無い"; exit 0; fi
# 先に移行で持ち込んだものを全て消し (.pristine だけ残す)、その後待避を戻す。
# (配置先ディレクトリの作成は削除の後。逆順だと作ったものを消し直すことになる)
find "$DST" -mindepth 1 -maxdepth 1 ! -name '.pristine' -exec rm -rf {} +
CONF="$DST"; test "$(cat "$PR/LAYOUT")" = nested && CONF="$DST/config"
mkdir -p "$CONF"
for f in "$PR"/*; do
  test "$(basename "$f")" = LAYOUT && continue
  mv "$f" "$CONF/"
done
rm -f "$PR/LAYOUT"  # ループで読み飞ばしたレイアウトの目印を消してから空にする
rmdir "$PR"
chown -R 1000:1000 "$DST"
echo "--- rolled back ---"
ls -la "$CONF"
SCRIPT

kubectl -n syncthing delete pod syncthing-migrate
kubectl -n syncthing scale deploy/syncthing --replicas=1
kubectl -n syncthing rollout status deploy/syncthing --timeout=300s
```

ロールバック後に検証 Job や ConfigMap が残っていれば手順 D のコマンドで消してよい。

> **不合格発見のタイミングと安全性**: 移行直後からピアとの同期は始まりうる (device ID が
> 同じなので発見・接続は自動)。だから rollout 直後にすぐ検証する。仮に短時間だけ同期が走って
> いても、ロールバックすれば旧 LXC 101 が引き続き正であり、ピア側に残るデータが失われることはない。

## check の読み方 (項目ごとの対処)

| 項目 | 不合格/不明の典型原因 | 対処 |
|---|---|---|
| identity-files | 置き場所 (flat/nested) の不一致、所有権が 1000:1000 以外 | 手順 B の再実行 (`.pristine` があるので上書きで安全) |
| device-id-format | cert.pem が壊れている/別物 | 手順 A から取り直す |
| self-device-declared | cert.pem と config.xml の組み合わせ違い (取り違え) | 手順 A から取り直す |
| folder-paths | config.xml の path 張り替え漏れ (sed 対象外の旧パスが残っている) | 出力メッセージの path を見て手順 B の sed を確認 |
| pvc-rw | 所有権問題 | 手順 B 再実行 (uid 1000 で検証しているため本番同等の判定) |
| restic-coverage | 不明 = マニフェスト未指定 (Job 外で実行した場合のみ起こる) | `--restic-manifest` を渡す (Job 定義では渡済み) |
| gui-health / tailnet-sync | 本体が起動しきっていない (rollout 未完了)、または GUI バインド修正漏れ | rollout status を確認。起動を繰り返すなら config.xml の `<gui><address>` を確認 |
| exercise-* | フォルダ登録/rescan の不調。cleanup だけ UNKNOWN のときは detail の文言で切替 — 「folder 削除に失敗」なら GUI から `acceptance-dummy` を手動削除、「ダミーディレクトリ削除に失敗」なら残骸確認のみでよい (フォルダ登録自体は抜けている) | ログの detail を見る。恒常的な問題ならロールバックを検討 |

## 合格後

- **LXC 101 はまだ止めない。** 数日観察し (GUI でフォルダが Up to Date、エラー無し)、
  問題なければ停止 → 一定期間経過後に破棄、という順序 (Plans.md M1 vaultwarden と同じ)。
  停止・破棄は T-0140 の残作業であり、この台本の範囲外
- 移行後の GUI ログイン資格情報は**旧 LXC 101 のもの**に変わる (config.xml ごと移っているため)。
  Ingress 経由の URL は従来どおり
- 既存ピアからの接続は device ID が不変のため自動的に復帰する (tailnet 経由のピアは
  `syncthing-sync` Service のホスト名宛のままで可)

## 既知の死角・未確定事項

- **flat / nested の確定は配置時に実物から行う** (手順 B の layout 検出)。repo 内の記録だけでは
  片側しか固定できなかった (cronjob の exclude は `config/index-v2` 表記、backup コメントは裸の
  `config.xml`)。ツールは両方自動判別するため、この曖昧さが手順の分岐を生まない設計にしてある
- `folder-paths` 検査は「path が新 root 配下に収まること」しか見ない。**実データが届いているか**
  は見ない。届け忘れは手順 B 末尾の出力確認と、合格後の GUI 観察で拾う
- 旧 index データ (`index-v2`, v1 の `index`) は意図的に持ってこない。syncthing が再スキャンで
  作り直す派生キャッシュであり、バージョンの異なる旧 DB を持ち込む方が危険
  (docs/backup.md P-0047 の除外判断と同じ理屈)
- この手順の Job/Pod 定義は実機での空回しがまだない (P-0163 の exercise 実測は死んだポート宛て
  まで)。初回適用時に YAML が弾かれたり ImagePull で詰まる可能性が僅かにあるが、
  いずれも再試行可能な範疇
