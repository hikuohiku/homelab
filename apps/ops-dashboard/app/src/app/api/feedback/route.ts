// 書き置き (人間フィードバック) の投稿口。
//
// 保管先は NATS (events.raw.homelab.dashboard) だけ。そこから bus-sidecar が
// heart の inbox にファイルとして落とす。GitHub の ops-feedback ブランチへの
// 保存はやめた (設計 state-out-of-git Phase 6) — 自宅クラスタの内部イベントを
// 外部 SaaS に預けると、GitHub が落ちた日に所有者の「止めて」が heart に届かない。
//
// publish できなければ 502 を返して issue #56 へ誘導する。黙って捨てない。
// NKey seed はこのプロセスの中だけで使い、応答に出さない。

import { busConfigured, publishNote } from "@/lib/feedback-bus";
import { buildNote, MAX_BODY_CHARS, newNoteId, normalizeKind } from "@/lib/feedback-note";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const REPO = process.env.FEEDBACK_REPO ?? "hikuohiku/homelab";
const ISSUE = process.env.FEEDBACK_ISSUE ?? "56";

const ISSUE_URL = `https://github.com/${REPO}/issues/${ISSUE}`;

export async function POST(request: Request): Promise<Response> {
  if (!busConfigured()) {
    return Response.json(
      { error: `保存経路が未設定です。代わりに ${ISSUE_URL} へ直接コメントしてください`, issueUrl: ISSUE_URL },
      { status: 503 },
    );
  }
  let body = "";
  let kind: string | undefined;
  try {
    const payload = (await request.json()) as { body?: unknown; kind?: unknown };
    body = typeof payload.body === "string" ? payload.body.trim() : "";
    kind = normalizeKind(payload.kind);
  } catch {
    return Response.json({ error: "JSON body {body: string} が必要です" }, { status: 400 });
  }
  if (!body) return Response.json({ error: "本文が空です" }, { status: 400 });
  if (body.length > MAX_BODY_CHARS) {
    return Response.json({ error: `本文が長すぎます (上限 ${MAX_BODY_CHARS} 文字)` }, { status: 400 });
  }

  const now = new Date();
  const note = buildNote(newNoteId(now), body, kind, now);
  try {
    await publishNote(note);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return Response.json(
      { error: `保存に失敗 (${message})。代わりに ${ISSUE_URL} へ直接コメントしてください`, issueUrl: ISSUE_URL },
      { status: 502 },
    );
  }
  return Response.json({ id: note.id });
}
