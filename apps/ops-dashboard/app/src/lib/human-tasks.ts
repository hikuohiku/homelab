// seeds.md の『人間の鍵作業』節の parse (P-0272)。
//
// 旧 build.py 世界が「あなたの手が要る」節として担っていた依頼面の復活。
// dashboard の runtime に python3 は無い (node:22-alpine) ため、
// ops/tools/human_tasks.py と同じルールを TypeScript に純関数として移植する。
// fixture は Python 側テストと共有 (ops/tests/fixtures/human_tasks_seeds.md)、
// 同入力 → 同出力を両側のテストで固定して drift を防ぐ。

import type { HumanTask } from "./types";

// 見出しレベル 1-2 のうち「人間の鍵作業」を含むものが対象の節
const SECTION_HEADING_RE = /^#{1,2}\s+.*人間の鍵作業.*/;
// 次の同レベル以上の見出しで節は終わる
const NEXT_HEADING_RE = /^#{1,2}\s+/;
// 項目は bullet + T-NNNN: だけ。番号付き行・インデント継続行は最初から一致しない
const ITEM_RE = /^- (T-\d+):\s*(\S.*)$/;
const STRIKETHROUGH_MARK = "~~";

export function extractSection(seedsText: string): string {
  const lines = seedsText.split("\n");
  const start = lines.findIndex((line) => SECTION_HEADING_RE.test(line));
  if (start < 0) return "";
  const body: string[] = [];
  for (let i = start + 1; i < lines.length; i++) {
    if (NEXT_HEADING_RE.test(lines[i])) break;
    body.push(lines[i]);
  }
  return body.join("\n");
}

export function ageDays(created: string | undefined, today: Date): number {
  if (!created) return 0;
  const ms = today.getTime() - Date.parse(`${created}T00:00:00Z`);
  if (!Number.isFinite(ms)) return 0;
  return Math.max(0, Math.floor(ms / 86_400_000));
}

export function parseHumanTasks(
  seedsText: string,
  createdById: Record<string, string>,
  today = new Date(),
): HumanTask[] {
  const tasks: HumanTask[] = [];
  for (const line of extractSection(seedsText).split("\n")) {
    if (line.includes(STRIKETHROUGH_MARK)) continue; // 取り消し線 = 解消済み・却下
    const match = ITEM_RE.exec(line);
    if (!match) continue;
    const created = createdById[match[1]];
    const task: HumanTask = { id: match[1], title: match[2].trim(), ageDays: ageDays(created, today) };
    // backlog に無い id では created を載せない (偽の情報源を作らない)
    if (created && Number.isFinite(Date.parse(`${created}T00:00:00Z`))) task.created = created;
    tasks.push(task);
  }
  // 古い順 (age_days 降順)、同点は id 昇順
  return tasks.sort((a, b) => b.ageDays - a.ageDays || a.id.localeCompare(b.id));
}

/** ops/backlog.json のテキスト → {id: created}。壊れていれば空。 */
export function backlogCreatedIndex(backlogText: string): Record<string, string> {
  let doc: unknown;
  try {
    doc = JSON.parse(backlogText);
  } catch {
    return {};
  }
  if (typeof doc !== "object" || doc === null || !Array.isArray((doc as { tasks?: unknown }).tasks)) return {};
  const index: Record<string, string> = {};
  for (const item of (doc as { tasks: unknown[] }).tasks) {
    if (typeof item !== "object" || item === null) continue;
    const id = (item as { id?: unknown }).id;
    const created = (item as { created?: unknown }).created;
    if (typeof id === "string" && typeof created === "string") index[id] = created;
  }
  return index;
}
