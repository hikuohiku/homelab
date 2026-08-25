# Doppler 障害ランブック

External Secrets Operator (ESO) にとって Doppler (`homelab/prd`) は唯一の上流であり、
クラスタ内の全 ExternalSecret (2026-08-23 時点で 21 件) が `ClusterSecretStore doppler`
経由でここに依存している。この文書は「Doppler が死んだ日」に人間が読むための手順で、
数値はすべて 2026-08-23 の遮断演習 (P-0175) での実測値である。
生データ: [`ops/projects/logs/P-0175/drill-report.json`](../ops/projects/logs/P-0175/drill-report.json)、
適用した NetworkPolicy の記録: [`ops/projects/logs/P-0175/netpol.yaml`](../ops/projects/logs/P-0175/netpol.yaml)。

## 症状 — 何が起こり、何が起こらないか

実測 (Doppler 行きのみを 35.5 分遮断):

- **最初は何も起きない。** 既存 Secret は resourceVersion 込みで一切変化せず、稼働中の
  Pod (vaultwarden / immich / coder / dex 等) は restart 0 で動き続ける。アプリのログに
  は何も出ない。ESO も黙ったまま
- エラーは各 ExternalSecret の `refreshInterval` (概ね 1h、telegram-adapter のみ 30m)
  を迎えた item から個別に顔を出す。遮断開始から最初の `SecretSyncedError` まで
  **539 秒**、全 17 件がエラー化しきるまで約 25 分。「失敗すら起こらない停止」が
  正しい描写
- エラーの message は一律 `could not get secret data from provider`
- **遮断中でも新規 Pod は立ち上がる。** telegram-adapter を遮断中に rollout restart
  したところ、既存 Secret を読んで 7 秒で Ready になった。既存 Secret が存在する限り
  アプリ側の再起動・再デプロイは壊れない
- 例外は「target Secret がまだ一度も作られていない」ケース。演習時点で
  `syncthing/syncthing-photo-intake-credentials` がまさにこの状態 (Doppler 側の別原因で
  エラー中、target Secret 未作成) で、これに依存する新しい Pod だけは Doppler 回復まで
  立ち上げできない

## 判定 — 障害かどうかの切り分け

1. 全景を見る:

   ```
   kubectl get externalsecrets -A
   ```

   複数 namespace の ExternalSecret が同時に `SecretSyncedError` になっていたら上流
   (Doppler / その経路) の疑いが強い。1 namespace だけならその item 固有の問題
   (リモートキー名の変更など)

2. エラーの中身を見る:

   ```
   kubectl get externalsecret <name> -n <namespace> -o jsonpath='{.status.conditions}'
   ```

   `dial tcp ...:443` 系の文言があればネットワーク層、401/403 系ならトークンの失効、
   「secret not found」系なら Doppler 側のキー削除

3. クラスタ内から Doppler への到達を確かめる (ESO と同じ HTTPS/443 での疎通テスト):

   ```
   kubectl run doppler-probe -n external-secrets --image=docker.io/library/busybox:1.36 \
     --restart=Never --command -- sleep 120
   kubectl exec -n external-secrets doppler-probe -- \
     wget -T 8 -O /dev/null https://api.doppler.com/
   kubectl delete pod doppler-probe -n external-secrets
   ```

   `Connection refused` / timeout なら経路が死んでいる (NetworkPolicy の取り残しを含む)。
   レスポンスが返れば経路は生きているので、Doppler サービス本体の障害かトークンの
   問題に絞られる。`wget: note: TLS certificate validation not implemented` という行は
   busybox wget の常動で異常ではない (2026-08-23 実機実測)。ESO は TLS (443) で話すため
   probe も https を使うこと — http (80) で試すと「80 番だけ生きていて 443 が死んでいる」
   状態を見誤る

4. ESO 本体のログ:

   ```
   kubectl logs -n external-secrets deploy/external-secrets --since=15m
   ```

5. 経時変化は health reporter の `externalsecrets` セクション
   (ConfigMap `autopilot/ops-health-report` の `latest.json` キー) にも出る。
   `last_sync_age_seconds` が `refresh_interval_seconds` を大きく超えて
   `Ready=True` のまま張り付いている場合は「まだエラーになっていない滞留」なので、
   このランブックの判定フローに入ってよい

## 応急 — 障害当日にやること / やってはいけないこと

やってよいこと:

- **観測と待機。** 既存の Secret と稼働中の Pod は少なくとも 35.5 分以上は持つ (実測。
  理論上限なし — ESO は既存 Secret を触りに行かない)。多くの場合、最善の応急は
  「Doppler の復旧を待つこと」
- アプリ単位の緊急対応が必要な場合は既存 Secret で再起動してよい (壊れないことは実測済み)

してはいけないこと:

- **既存 Secret を削除・編集しない。** ESO が owner のオブジェクトであり、消すと
  「遮断中でも Pod 再作成は可能」という唯一の耐性を自ら潰す
- ESO を再起動・スケール 0 にして直そうとしない。原因は ESO の外にあるので効果がなく、
  leader election や cert rotation の余計な揺れだけを生む
- egress をいじる変更 (NetworkPolicy の追加・修正) をその場で行わない。
  演習の教訓として、kube-router の NetworkPolicy は Service の ClusterIP ではなく
  DNAT 後の endpoint 側 IP で評価される ([netpol.yaml](../ops/projects/logs/P-0175/netpol.yaml)
  の v1/v2 欠陥参照)。場当たり的な egress 変更は DNS や K8s API を巻き込んで落とせる
- target Secret 未作成の新規ワークロードのデプロイを進めない (立ち上がらず Pending になる)

## 恒久 — 復旧と事後

1. Doppler 側を復旧させる。障害ページの確認、またはトークン失効が疑われる場合は
   Doppler コンソールで `homelab/prd` の service token を確認・再発行する
   (トークンを差し替えた場合は ClusterSecretStore `doppler` 参照先の
   Secret `doppler-token` (external-secrets namespace) を更新する)
2. 経路の復活を probe で確認 (「判定」の 3 を再実行)
3. **再同期は一斉には戻らない。** 実測では NetworkPolicy 削除後に、遮断後半でエラーに
   なった item (失敗回数が少ない) から順に解除後約 6.5 分、初期エラー群は約 10 分、
   最終 1 件は約 11.9 分かけて階段状に戻った。指数バックオフによるもので、
   「10 分たっても一部が Error のまま = まだ復旧していない」ではない。最長でも
   十数分は待つ
4. 全員戻ったことを確認:

   ```
   kubectl get externalsecrets -A | grep 'SecretSyncedError'
   ```

   エラーのまま残っている item の一覧が出る。これが空になれば復旧完了。
   逆引き (`grep -v SecretSynced`) では探さないこと — STATUS は正常時も
   `SecretSynced` という文字を含むため、正常・異常の両方とも消えてしまい
   何も判別できない。常設の既知エラーが今後増えた場合はその item を読み替える

5. 事後記録: 発生日時・影響 (どの ES がいつエラー化したか)・復旧所要時間を
   Maintenance.md か issue に残す。恒久的な冗長化 (代替プロバイダ併用など) が必要かの
   判断は別プロジェクトとして起票する (本ランブックの範囲は実測と手順まで)
