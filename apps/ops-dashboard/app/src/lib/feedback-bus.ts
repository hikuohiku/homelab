// 書き置きを NATS (JetStream) へ流す出口。設計 state-out-of-git Phase 6。
//
// 何を解いているか: 書き置きの一次保管が GitHub の ops-feedback ブランチだった。
// 所有者の「止めて」が外部 SaaS の可用性に乗っている。ここをクラスタ内で閉じる。
// heart 側は既にバスを読んでいる (NATS → bus-sidecar → ファイル → collect_feedback)
// ので、publish 先を足せば経路が通る。
//
// note の形は telegram-adapter の busEvent と同じにしてある。sidecar が
// <id>.json で落とし、heart の cursor が GitHub 経路と同じ鍵で重複を落とす。
//
// 接続は 1 リクエスト 1 本。書き置きは人間が手で書く頻度なので張りっぱなしにする
// 価値がなく、常時接続だと Next.js のワーカー入れ替えごとに切断の後始末が要る。
//
// seed は環境変数から読んで、この関数の中だけで使う。応答にもログにも出さない。

import { jetstream } from "@nats-io/jetstream";
import { connect, nkeyAuthenticator } from "@nats-io/transport-node";

export interface FeedbackNote {
  id: string;
  source: string;
  received: string;
  kind?: string;
  body: string;
}

const SUBJECT = process.env.NATS_SUBJECT ?? "events.raw.homelab.dashboard";
// 人間が送信ボタンを押して待っている時間。届かないなら早く「届かなかった」と言う
const TIMEOUT_MS = Number(process.env.NATS_TIMEOUT_MS ?? 5_000);

/** バスの設定があるか。無ければ publish は試みない (切り戻し構成)。 */
export function busConfigured(): boolean {
  return Boolean(process.env.NATS_URL?.trim() && process.env.NATS_NKEY_SEED?.trim());
}

/**
 * note 1 件を events.raw.homelab.dashboard へ publish する。
 *
 * JetStream の publish を使うのは、core NATS の publish が fire-and-forget で
 * 権限違反もストリーム不在もエラーにならないため (telegram-adapter で実測済み)。
 * ack を待って初めて「届いた」と言える。
 */
export async function publishNote(note: FeedbackNote): Promise<void> {
  const url = (process.env.NATS_URL ?? "").trim();
  const seed = (process.env.NATS_NKEY_SEED ?? "").trim();
  if (!url || !seed) throw new Error("NATS_URL / NATS_NKEY_SEED が未設定");

  const nc = await connect({
    servers: url,
    name: "ops-dashboard",
    authenticator: nkeyAuthenticator(new TextEncoder().encode(seed)),
    timeout: TIMEOUT_MS,
    // 1 リクエスト限りの接続なので再接続しない。失敗はその場で呼び出し元へ返す
    maxReconnectAttempts: 0,
  });
  try {
    // msgID は JetStream 側の重複排除キー。同じ note を再送しても 1 通にしかならない
    await jetstream(nc).publish(SUBJECT, new TextEncoder().encode(JSON.stringify(note)), {
      msgID: `dashboard-${note.id}`,
      timeout: TIMEOUT_MS,
    });
  } finally {
    await nc.drain().catch(() => {});
  }
}
