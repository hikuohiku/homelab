// 常駐エージェント (core / heart) の transcript 解釈の契約を固定する (P-9004)。
//
// 常駐は Job と違い役割名 (runner|…) でなく Deployment 名を agent id に持つため、
// parseAgentName がそれを解釈し、findTranscriptFile が transcripts/resident/ を
// 見る。イベント行は既存ビューア (normalizeTranscriptEvent) で読める形式に限る。
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { after, test } from "node:test";
import { mergeTranscriptEvent } from "../src/lib/transcript-client";
import type { TranscriptEvent } from "../src/lib/types";

const fixtureDir = mkdtempSync(path.join(tmpdir(), "resident-transcript-"));
// transcript.ts はモジュール読み込み時に HEART_DATA_DIR を固定する。静的 import だと
// この代入より先にモジュールが評価されるため、動的 import で fixture を向かせる
process.env.HEART_DATA_DIR = fixtureDir;
const transcriptModule = import("../src/lib/transcript");

const coreFixture = readFileSync(new URL("./fixtures/resident-core.jsonl", import.meta.url), "utf8");
// heart の beat 行は opencode の text 形式。latestAction が「可視イベント」として
// 拾う (type=system だとカードの recentAction が「セッション開始」に留まる)
const heartFixture =
  '{"type":"text","part":{"type":"text","text":"beat 5001: actions=[] unhealthy=[]"},"timestamp":1787384168000}\n';

mkdirSync(path.join(fixtureDir, "transcripts", "resident"), { recursive: true });
writeFileSync(path.join(fixtureDir, "transcripts", "resident", "core.jsonl"), coreFixture);
writeFileSync(path.join(fixtureDir, "transcripts", "resident", "heart.jsonl"), heartFixture);

after(() => rmSync(fixtureDir, { recursive: true, force: true }));

test("parseAgentName は resident の Deployment 名を解釈する", async () => {
  const { parseAgentName } = await transcriptModule;
  assert.deepEqual(parseAgentName("autopilot-core"), { role: "core", projectId: "autopilot-core" });
  assert.deepEqual(parseAgentName("autopilot-heart"), { role: "heart", projectId: "autopilot-heart" });
  // Job 由来の解釈は現状維持
  assert.deepEqual(parseAgentName("runner-p-0001-a1"), { role: "worker", projectId: "p-0001" });
  assert.equal(parseAgentName("../../data/transcripts"), null);
});

test("transcriptMode は core/heart を resident ディレクトリへ向ける", async () => {
  const { transcriptMode } = await transcriptModule;
  assert.equal(transcriptMode("core"), "resident");
  assert.equal(transcriptMode("heart"), "resident");
  assert.equal(transcriptMode("reviewer"), "review");
  assert.equal(transcriptMode("worker"), "worker");
});

test("findTranscriptFile は resident/<role>.jsonl を選ぶ", async () => {
  const { findTranscriptFile } = await transcriptModule;
  const core = await findTranscriptFile("core", "autopilot-core");
  assert.ok(core);
  assert.equal(path.basename(core!), "core.jsonl");
  const heart = await findTranscriptFile("heart", "autopilot-heart");
  assert.ok(heart);
  assert.equal(path.basename(heart!), "heart.jsonl");
});

test("latestAction は resident の実ファイルから最新アクションを出す", async () => {
  const { latestAction } = await transcriptModule;
  const core = await latestAction("core", "autopilot-core");
  assert.equal(core.available, true);
  assert.equal(core.text, "bash を実行");
  const heart = await latestAction("heart", "autopilot-heart");
  assert.equal(heart.available, true);
  assert.match(heart.text, /beat 5001/);
});

test("resident の flat イベント行は既存ビューアで正規化される", async () => {
  const { normalizeTranscriptEvent } = await transcriptModule;
  const lines = coreFixture.trim().split("\n");
  const events = lines.flatMap((line, index) => normalizeTranscriptEvent(JSON.parse(line), `line-${index + 1}`));
  assert.deepEqual(events.map((event) => event.kind), ["system", "thinking", "message", "tool", "tool", "result"]);
  const merged = events.reduce(mergeTranscriptEvent, [] as TranscriptEvent[]);
  const tool = merged.find((event) => event.kind === "tool");
  assert.equal(tool?.toolName, "bash");
  assert.equal(tool?.status, "completed");
  const result = merged.at(-1);
  assert.equal(result?.kind, "result");
  assert.equal(result?.usage?.total, 3120);
});