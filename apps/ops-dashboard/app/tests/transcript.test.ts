import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { mergeTranscriptEvent } from "../src/lib/transcript-client";
import { normalizeTranscriptEvent, parseAgentName } from "../src/lib/transcript";
import type { TranscriptEvent } from "../src/lib/types";

async function fixture(name: string): Promise<TranscriptEvent[]> {
  const text = await readFile(new URL(`./fixtures/${name}`, import.meta.url), "utf8");
  return text.trim().split("\n").flatMap((line, index) => normalizeTranscriptEvent(JSON.parse(line), `line-${index + 1}`));
}

test("Claude stream-json の思考・発話・tool・usage を正規化する", async () => {
  const events = await fixture("claude.jsonl");
  assert.deepEqual(events.map((event) => event.kind), ["system", "thinking", "message", "tool", "tool", "result"]);
  assert.equal(events[3].toolName, "Bash");
  assert.equal(events[4].status, "completed");
  assert.deepEqual(events.at(-1)?.usage, { input: 1000, output: 250, total: 1250, costUsd: 0.125 });
});

test("OpenCode JSON の reasoning・tool 更新・step usage を正規化する", async () => {
  const events = await fixture("opencode.jsonl");
  // 末尾の 1 行は実測 (2026-08-22, v1.18.21) の top-level type "tool_use" 形。
  // これを落とすとライブビューが step 境界だらけになる (実際に起きた)
  assert.deepEqual(events.map((event) => event.kind), ["system", "thinking", "message", "tool", "tool", "result", "tool"]);
  const merged = events.reduce(mergeTranscriptEvent, [] as TranscriptEvent[]);
  const tool = merged.find((event) => event.kind === "tool");
  assert.equal(tool?.status, "completed");
  assert.equal(tool?.output, "4 tests passed");
  const realTool = events.at(-1);
  assert.equal(realTool?.kind, "tool");
  assert.equal(realTool?.toolName, "bash");
  assert.equal(realTool?.status, "completed");
  assert.equal(events.filter((e) => e.kind === "result").at(-1)?.usage?.total, 8737);
});

test("agent id は既知 Job 名だけを受け入れる", () => {
  assert.deepEqual(parseAgentName("reviewer-p-0045-a2"), { role: "reviewer", projectId: "p-0045" });
  assert.deepEqual(parseAgentName("critic-critic-a109"), { role: "critic", projectId: "critic" });
  assert.equal(parseAgentName("../../data/transcripts"), null);
});

