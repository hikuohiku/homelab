import assert from "node:assert/strict";
import test from "node:test";
import { buildNote, noteId, normalizeKind } from "../src/lib/feedback-note";

// 書き置きは所有者の「止めて」を運ぶ経路。ここが守るのは、id が bus-sidecar の
// safeID に収まること — 外れると sidecar がファイルを置けず、書き置きが heart に
// 届かないまま捨てられる
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

test("noteId は sidecar がファイル名に使える形", () => {
  const id = noteId(new Date("2026-08-25T12:34:56.789Z"), "a1b2c3");
  assert.equal(id, "20260825-123456-a1b2c3");
  assert.match(id, SAFE_ID);
});

test("buildNote は sidecar が読む形の note を作る", () => {
  const now = new Date("2026-08-25T12:34:56.789Z");
  assert.deepEqual(buildNote("20260825-123456-a1b2c3", "止めて", undefined, now), {
    id: "20260825-123456-a1b2c3",
    source: "ops-dashboard",
    received: "2026-08-25T12:34:56Z",
    body: "止めて",
  });
});

test("kind は許可リスト外を落とす", () => {
  assert.equal(normalizeKind("task-request"), "task-request");
  assert.equal(normalizeKind("something-else"), undefined);
  assert.equal(normalizeKind(42), undefined);
});

test("buildNote は kind を持つときだけ載せる", () => {
  const now = new Date("2026-08-25T12:34:56Z");
  assert.equal(buildNote("x", "掃除して", "task-request", now).kind, "task-request");
  assert.equal("kind" in buildNote("x", "掃除して", undefined, now), false);
});
