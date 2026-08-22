// 書き置き (人間フィードバック) の投稿口。旧 server.py の POST /feedback の移植。
//
// 一次保管場所はリポジトリ内の構造化データ (ops-feedback ブランチの
// ops/feedback/inbox/<id>.json、1 件 1 ファイル)。heart がそれを読んで台帳へ
// 取り込む。main へは直 push できない (ruleset) ため専用ブランチへ Contents API で
// 書く。1 件 1 ファイルなので read-modify-write が無く、同時投稿でも衝突しない。
// この経路は autopilot に依存しない — heart が死んでいても書き置きは残る。
// トークンはこのプロセスの中だけで使い、応答に一切出さない。

import { randomBytes } from "node:crypto";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const REPO = process.env.FEEDBACK_REPO ?? "hikuohiku/homelab";
const BRANCH = process.env.FEEDBACK_BRANCH ?? "ops-feedback";
const BASE_BRANCH = process.env.FEEDBACK_BASE_BRANCH ?? "main";
const INBOX_DIR = (process.env.FEEDBACK_DIR ?? "ops/feedback/inbox").replace(/^\/+|\/+$/g, "");
const ISSUE = process.env.FEEDBACK_ISSUE ?? "56";
const API = (process.env.GITHUB_API ?? "https://api.github.com").replace(/\/+$/, "");
// 旧 server.py / build.py の textarea maxlength と揃える
const MAX_BODY_CHARS = 20000;

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

function newNoteId(): string {
  const now = new Date();
  const stamp = now.toISOString().replace(/[-:]/g, "").slice(0, 15).replace("T", "-");
  return `${stamp}-${randomBytes(3).toString("hex")}`;
}

export async function POST(request: Request): Promise<Response> {
  if (!token()) {
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
    // kind は heart の tasks.KIND_TASK_REQUEST と揃える。許可リスト外は無視して
    // ただの書き置き扱い (勝手な種別を発明させない)
    kind = payload.kind === "task-request" ? "task-request" : undefined;
  } catch {
    return Response.json({ error: "JSON body {body: string} が必要です" }, { status: 400 });
  }
  if (!body) return Response.json({ error: "本文が空です" }, { status: 400 });
  if (body.length > MAX_BODY_CHARS) {
    return Response.json({ error: `本文が長すぎます (上限 ${MAX_BODY_CHARS} 文字)` }, { status: 400 });
  }
  try {
    await ensureBranch();
    // 422 = 同じパスが既にある (乱数衝突)。id を振り直して 1 度だけやり直す
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const note = {
        id: newNoteId(),
        source: "ops-dashboard",
        received: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
        ...(kind ? { kind } : {}),
        body,
      };
      const path = `${INBOX_DIR}/${note.id}.json`;
      const put = await gh("PUT", `/repos/${REPO}/contents/${path}`, {
        message: `feedback ${note.id} (${note.source})`,
        content: Buffer.from(`${JSON.stringify(note, null, 1)}\n`, "utf8").toString("base64"),
        branch: BRANCH,
      });
      if (put.status === 200 || put.status === 201) {
        return Response.json({ id: note.id });
      }
      if (put.status !== 422 || attempt > 0) {
        throw new Error(`保存に失敗 (${put.status})`);
      }
    }
    throw new Error("保存に失敗 (id 再試行も 422)");
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return Response.json(
      { error: `${message}。代わりに ${ISSUE_URL} へ直接コメントしてください`, issueUrl: ISSUE_URL },
      { status: 502 },
    );
  }
}
