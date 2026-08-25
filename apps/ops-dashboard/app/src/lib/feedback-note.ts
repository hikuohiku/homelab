// 書き置き 1 件の組み立て。I/O を持たない純関数だけを置く。

import { randomBytes } from "node:crypto";

import type { FeedbackNote } from "./feedback-bus";

// 旧 server.py / build.py の textarea maxlength と揃える
export const MAX_BODY_CHARS = 20000;

/**
 * note の id。バスの sidecar がこれをそのままファイル名 (<id>.json) にし、
 * heart の既読 cursor がその名前で重複を落とす。
 * sidecar の safeID (^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$) に収まる形だけを作る。
 */
export function noteId(now: Date, suffix: string): string {
  const stamp = now.toISOString().replace(/[-:]/g, "").slice(0, 15).replace("T", "-");
  return `${stamp}-${suffix}`;
}

export function newNoteId(now: Date = new Date()): string {
  return noteId(now, randomBytes(3).toString("hex"));
}

export function buildNote(id: string, body: string, kind: string | undefined, now: Date): FeedbackNote {
  return {
    id,
    source: "ops-dashboard",
    received: now.toISOString().replace(/\.\d+Z$/, "Z"),
    ...(kind ? { kind } : {}),
    body,
  };
}

/** 受け取った payload の kind を許可リストで潰す。勝手な種別を発明させない。 */
export function normalizeKind(raw: unknown): string | undefined {
  return raw === "task-request" ? "task-request" : undefined;
}
