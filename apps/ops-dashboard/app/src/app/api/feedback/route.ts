// 書き置き (人間フィードバック) の投稿口。
//
// 保管先は 2 つ。NATS (events.raw.homelab.dashboard → bus-sidecar → heart) と、
// 従来からの GitHub ops-feedback ブランチ (ops/feedback/inbox/<id>.json)。
// 両方に同じ id で書く。heart の既読 cursor は両経路で同じ鍵を持つので、
// 二重に処理されることはない。
//
// なぜ両書きか: 一次保管を GitHub からクラスタ内へ移す途中 (設計
// state-out-of-git Phase 6)。先に GitHub を落とすと、バスが届いていない間の
// 「止めて」が宙に浮く。バスが実際に通ったことを確かめてから GitHub 側を落とす。
//
// 片方でも保存できれば受理する。両方落ちたときだけ失敗を返して issue #56 へ誘導する
// (黙って捨てない)。トークンと NKey seed はこのプロセスの中だけで使い、応答に出さない。

import { busConfigured, publishNote } from "@/lib/feedback-bus";
import {
  buildNote,
  decideOutcome,
  MAX_BODY_CHARS,
  newNoteId,
  normalizeKind,
  type RouteResult,
} from "@/lib/feedback-note";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const REPO = process.env.FEEDBACK_REPO ?? "hikuohiku/homelab";
const BRANCH = process.env.FEEDBACK_BRANCH ?? "ops-feedback";
const BASE_BRANCH = process.env.FEEDBACK_BASE_BRANCH ?? "main";
const INBOX_DIR = (process.env.FEEDBACK_DIR ?? "ops/feedback/inbox").replace(/^\/+|\/+$/g, "");
const ISSUE = process.env.FEEDBACK_ISSUE ?? "56";
const API = (process.env.GITHUB_API ?? "https://api.github.com").replace(/\/+$/, "");

const ISSUE_URL = `https://github.com/${REPO}/issues/${ISSUE}`;

function token(): string {
  return (process.env.GITHUB_TOKEN ?? "").trim();
}

async function gh(method: string, path: string, body?: unknown): Promise<{ status: number; json: any }> {
  const response = await fetch(`${API}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${token()}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "mission-control/1",
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });
  const json = await response.json().catch(() => ({}));
  return { status: response.status, json };
}

async function ensureBranch(): Promise<void> {
  const head = await gh("GET", `/repos/${REPO}/git/ref/heads/${BRANCH}`);
  if (head.status === 200) return;
  const base = await gh("GET", `/repos/${REPO}/git/ref/heads/${BASE_BRANCH}`);
  if (base.status !== 200) throw new Error(`base branch ${BASE_BRANCH} が読めない (${base.status})`);
  const created = await gh("POST", `/repos/${REPO}/git/refs`, {
    ref: `refs/heads/${BRANCH}`,
    sha: base.json.object?.sha,
  });
  // 422 = 競合して誰かが先に作った。それでよい
  if (created.status !== 201 && created.status !== 422) {
    throw new Error(`branch ${BRANCH} を作れない (${created.status})`);
  }
}

/**
 * GitHub 側へ保存する。id は呼び出し元が決めた 1 つだけを使う (バスと鍵を揃えるため。
 * ここで振り直すと、同じ書き置きが 2 つの鍵で heart に届いて 2 回処理される)。
 * 422 = 同じパスが既にある = 自分の再送なので成功として扱う。
 */
async function saveToGithub(note: object, id: string): Promise<void> {
  await ensureBranch();
  const path = `${INBOX_DIR}/${id}.json`;
  const put = await gh("PUT", `/repos/${REPO}/contents/${path}`, {
    message: `feedback ${id} (ops-dashboard)`,
    content: Buffer.from(`${JSON.stringify(note, null, 1)}\n`, "utf8").toString("base64"),
    branch: BRANCH,
  });
  if (put.status === 200 || put.status === 201 || put.status === 422) return;
  throw new Error(`保存に失敗 (${put.status})`);
}

async function attempt(run: () => Promise<void>): Promise<RouteResult> {
  try {
    await run();
    return { ok: true };
  } catch (error) {
    return { ok: false, reason: error instanceof Error ? error.message : String(error) };
  }
}

export async function POST(request: Request): Promise<Response> {
  if (!busConfigured() && !token()) {
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
  const id = newNoteId(now);
  const note = buildNote(id, body, kind, now);

  // バスを先に試す。heart に一番速く届く経路で、移行後もこれだけが残る
  const bus = busConfigured()
    ? await attempt(() => publishNote(note))
    : ({ ok: false, reason: "未設定" } as RouteResult);
  const github = token()
    ? await attempt(() => saveToGithub(note, id))
    : ({ ok: false, reason: "GITHUB_TOKEN 未設定" } as RouteResult);

  const outcome = decideOutcome(bus, github);
  if (outcome.ok) return Response.json({ id });
  return Response.json(
    { error: `保存に失敗 (${outcome.reason})。代わりに ${ISSUE_URL} へ直接コメントしてください`, issueUrl: ISSUE_URL },
    { status: 502 },
  );
}
