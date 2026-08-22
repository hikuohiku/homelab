# 写真の預け方 (photo-intake)

スマホや PC の写真を immich に取り込む手順。**フォルダに放り込むだけ**で、あとは自動。
仕組みは `apps/syncthing/photo-intake-cronjob.yaml` (P-0085)。

## 1. 預け方

1. Tailscale ネットワークに接続した状態で syncthing の GUI を開く
   (`https://syncthing.<tailnet>.ts.net`)
2. **初回のみ**: GUI の「フォルダを追加」で `photo-intake`(`/var/syncthing/photo-intake`)
   を共有フォルダとして登録し、スマホ等のデバイスとペアリングする
3. `photo-intake` フォルダに写真・動画を置く。サブフォルダに入れてもよい(構造のまま取り込む)
4. 以上。置いたものが immich (`https://immich.<tailnet>.ts.net`) に現れる

### 置けるもの

jpg / jpeg / png / gif / webp / heic / heif / avif / jxl / tiff 系 / RAW(dng, cr2, cr3,
nef, arw, raf, orf, rw2, srw) / mp4 / mov / m4v / avi / mkv / webm / mts / m2ts / ts /
3gp / wmv / flv。

**置かないでよいもの**: 撮影途中の一時ファイル。`.` で始まるファイルと
`~syncthing~*`(sync 中の一時ファイル)は自動でスキップされる。

## 2. 反映までの時間

最大 **10 分**(10 分毎の CronJob が intake を見に行く)。直前の取り込みが終わっていない
場合はその分が終わってから。失敗したファイルは消されず元の場所に残り、次回自動で再試行
される。

## 3. 重複しても安心

同じファイルを何度置いても immich に二重登録はされない。

- サーバ側: immich が checksum で重複を検出し、再 upload はスキップされる
- ローカル側: upload 成功分は `photo-intake/done/` へ移動するため、次回の対象にならない

## 4. 元ファイルを消してよい条件

`photo-intake/done/` に移動したものは immich への取り込み済み。ただし削除してよいのは:

1. **done/ に移動済み**であること(元の場所に残っているものはまだ取り込みが済んでいない
   可能性がある)
2. **syncthing-data の restic バックアップが追いついた後**であること
   (毎日 03:55 JST 実行。`docs/backup.md` 参照)

両方揃う前の消去は非推奨。なお immich 側から削除するとゴミ箱経由になるので、取り違えの
復旧は immich の UI からも可能。

## トラブルシュート

- **いつまでも immich に現れない**: ファイルが `photo-intake/` 直下(またはサブフォルダ)に
  残っているか確認。`done/` に移っていたら取り込み済み。拡張子が対応表に無いと取り込まれない
- **GUI に photo-intake フォルダが無い**: フォルダは CronJob の初回実行時に作られる。
  ExternalSecret (`IMMICH_API_KEY`) が同期できるまで CronJob は起動しない
