// 書き置き 1 件の組み立てと、2 経路 (NATS / GitHub) の結果の判定。
// I/O を持たない純関数だけを置く — 経路を足すたびに「どう倒すか」が
// route.ts のフロー制御に埋もれると、fail-closed が崩れても気づけない。

import { randomBytes } from "node:crypto";

import type { FeedbackNote } from "./feedback-bus";

// 旧 server.py / build.py の textarea maxlength と揃える
export const MAX_BODY_CHARS = 20000;

/**
 * note の id。バスの sidecar がこれをそのままファイル名 (<id>.json) にし、
 * heart の既読 cursor が GitHub 経路のパスと突き合わせる鍵になる。
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

export type RouteResult = { ok: true } | { ok: false; reason: string };

/**
 * 2 経路の結果から「書き置きは届いたか」を決める。
 *
 * 片方でも届けば heart には届く (両経路の鍵が同じなので二重処理にならない)。
 * **両方落ちたときに成功と言わない**のがこの関数の全て — 所有者の「止めて」を
 * 黙って捨てる経路を作らないため、投稿者に issue #56 へ回ってもらう。
 */
export function decideOutcome(bus: RouteResult, github: RouteResult): RouteResult {
  if (bus.ok || github.ok) return { ok: true };
  return { ok: false, reason: `bus: ${bus.reason} / github: ${github.reason}` };
}
