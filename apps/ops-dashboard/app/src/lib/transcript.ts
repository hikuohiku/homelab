import { open, readdir, stat } from "node:fs/promises";
import path from "node:path";
import { watch } from "node:fs";
import { StringDecoder } from "node:string_decoder";
import { mergeTranscriptEvent } from "./transcript-client";
import type { AgentRole, TokenUsage, TranscriptEvent } from "./types";

const DATA_DIR = process.env.HEART_DATA_DIR ?? "/data";

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

function asText(value: unknown): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    return value
      .map((item) => {
        if (typeof item === "string") return item;
        const part = asRecord(item);
        return typeof part.text === "string" ? part.text : JSON.stringify(item);
      })
      .join("\n");
  }
  if (value == null) return "";
  return JSON.stringify(value, null, 2);
}

function usage(input: unknown, output: unknown, cost: unknown): TokenUsage {
  const inputTokens = Number(input ?? 0);
  const outputTokens = Number(output ?? 0);
  return {
    input: Number.isFinite(inputTokens) ? inputTokens : 0,
    output: Number.isFinite(outputTokens) ? outputTokens : 0,
    total: (Number.isFinite(inputTokens) ? inputTokens : 0) +
      (Number.isFinite(outputTokens) ? outputTokens : 0),
    costUsd: Number(cost ?? 0) || 0,
  };
}

function claudeEvents(raw: Record<string, unknown>, lineId: string): TranscriptEvent[] {
  const type = raw.type;
  const at = toIso(raw.timestamp);
  if (type === "assistant" || type === "user") {
    const message = asRecord(raw.message);
    const content = Array.isArray(message.content) ? message.content : [message.content];
    const events: TranscriptEvent[] = [];
    content.forEach((item, index) => {
      if (typeof item === "string") {
        events.push({ id: `${lineId}-${index}`, kind: "message", at, text: item });
        return;
      }
      const part = asRecord(item);
      const partType = part.type;
      if (partType === "thinking") {
        events.push({ id: `${lineId}-${index}`, kind: "thinking", at, text: asText(part.thinking) });
        return;
      }
      if (partType === "text") {
        events.push({ id: `${lineId}-${index}`, kind: "message", at, text: asText(part.text) });
        return;
      }
      if (partType === "tool_use") {
        events.push({
          id: String(part.id ?? `${lineId}-${index}`), kind: "tool", at,
          toolName: String(part.name ?? "tool"), input: part.input, status: "running",
        });
        return;
      }
      if (partType === "tool_result") {
        events.push({
          id: String(part.tool_use_id ?? `${lineId}-${index}`), kind: "tool", at,
          output: part.content, status: part.is_error ? "failed" : "completed",
        });
      }
    });
    return events;
  }
  if (type === "result") {
    const rawUsage = asRecord(raw.usage);
    return [{
      id: lineId,
      kind: raw.is_error ? "error" : "result",
      at,
      text: asText(raw.result || raw.error || raw.subtype),
      usage: usage(rawUsage.input_tokens, rawUsage.output_tokens, raw.total_cost_usd),
    }];
  }
  if (type === "system") {
    return [{ id: lineId, kind: "system", at, text: asText(raw.subtype ?? raw.message ?? "session start") }];
  }
  return [];
}

function toIso(value: unknown): string | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return new Date(value).toISOString();
  if (typeof value === "string" && value) return value;
  return undefined;
}

function opencodeEvents(raw: Record<string, unknown>, lineId: string): TranscriptEvent[] {
  const part = asRecord(raw.part);
  const type = String(raw.type ?? part.type ?? "");
  // 実測: raw.timestamp は epoch ミリ秒の数値、part.time は {start,end} (ミリ秒)。
  // 旧実装は string しか見ておらず at が常に undefined だった (JST 表示のため修正)
  const time = asRecord(part.time);
  const at = toIso(time.start) ?? toIso(part.time) ?? toIso(raw.timestamp);
  if (type === "text") {
    return [{ id: String(part.id ?? lineId), kind: "message", at, text: asText(part.text ?? raw.text) }];
  }
  if (type === "reasoning" || part.type === "reasoning") {
    return [{ id: String(part.id ?? lineId), kind: "thinking", at, text: asText(part.text ?? raw.text) }];
  }
  // 実測 (2026-08-22, opencode 1.18.21): tool イベントの top-level type は
  // "tool_use" で、part.type が "tool"。type === "tool" だけだと全ツール実行が
  // 落ちて、ライブビューが step 境界 (ステップ開始/完了) だらけになる
  if (type === "tool" || type === "tool_use" || part.type === "tool") {
    const state = asRecord(part.state);
    const rawStatus = String(state.status ?? part.status ?? "running");
    const status = rawStatus === "completed" ? "completed" :
      rawStatus === "error" || rawStatus === "failed" ? "failed" : "running";
    return [{
      id: String(part.callID ?? part.callId ?? part.id ?? lineId),
      kind: "tool",
      at,
      toolName: String(part.tool ?? part.name ?? state.title ?? "tool"),
      input: state.input ?? part.input,
      output: state.output ?? state.error ?? part.output,
      status,
    }];
  }
  if (type === "step_finish") {
    const tokens = asRecord(part.tokens);
    return [{
      id: String(part.id ?? lineId), kind: "result", at,
      text: "ステップ完了",
      usage: usage(tokens.input, tokens.output, part.cost),
    }];
  }
  if (type === "step_start") {
    return [{ id: String(part.id ?? lineId), kind: "system", at, text: "ステップ開始" }];
  }
  if (type === "error") {
    const error = asRecord(raw.error);
    const data = asRecord(error.data);
    return [{ id: lineId, kind: "error", at, text: asText(data.message ?? error.name ?? "unknown error") }];
  }
  return [];
}

export function normalizeTranscriptEvent(raw: unknown, lineId: string): TranscriptEvent[] {
  const event = asRecord(raw);
  const type = event.type;
  if (type === "assistant" || type === "user" || type === "result" || type === "system") {
    return claudeEvents(event, lineId);
  }
  return opencodeEvents(event, lineId);
}

export function roleFromKind(kind: string): AgentRole {
  const roles: Record<string, AgentRole> = {
    runner: "worker", worker: "worker", reviewer: "reviewer", review: "reviewer",
    curriculum: "curriculum", critic: "critic", consolidation: "consolidation", chore: "chore",
  };
  return roles[kind] ?? "unknown";
}

// 常駐エージェント (autopilot-core / autopilot-heart) は Deployment 名を agent id に
// 持ち (kubernetes.ts の id: metadata.name)、Job 由来の役割名を持たない (P-9004)。
// 解釈できれば transcript が transcripts/resident/<role>.jsonl にある
const RESIDENT_IDS: Record<string, AgentRole> = {
  "autopilot-core": "core",
  "autopilot-heart": "heart",
};

function isResidentRole(role: AgentRole): boolean {
  return role === "core" || role === "heart";
}

export function transcriptMode(role: AgentRole): string {
  if (isResidentRole(role)) return "resident";
  return role === "reviewer" ? "review" : role;
}

export function parseAgentName(agentId: string): { role: AgentRole; projectId: string } | null {
  if (!/^[a-z0-9-]{1,63}$/.test(agentId)) return null;
  if (RESIDENT_IDS[agentId]) return { role: RESIDENT_IDS[agentId], projectId: agentId };
  const match = agentId.match(/^(runner|reviewer|curriculum|critic|consolidation|chore)-(.+)-a\d+$/);
  if (!match) return null;
  return { role: roleFromKind(match[1]), projectId: match[2] };
}

export async function findTranscriptFile(role: AgentRole, projectId: string): Promise<string | null> {
  const directory = path.join(DATA_DIR, "transcripts", transcriptMode(role));
  let names: string[];
  try {
    names = await readdir(directory);
  } catch {
    return null;
  }
  const resident = isResidentRole(role);
  const prefix = `${projectId.toLowerCase()}-`;
  const candidates = await Promise.all(names
    .filter((name) => {
      if (!name.endsWith(".jsonl")) return false;
      if (resident) {
        // 常駐は <role>.jsonl の単一追記ファイル。ローテーションは削除のみで
        // 改名しないが、将来の変種 (分割など) に備えて role 始まりも許容する
        const base = name.toLowerCase();
        return base === `${role}.jsonl` || base.startsWith(`${role}-`);
      }
      return name.toLowerCase().includes(`-${prefix}`);
    })
    .map(async (name) => {
      const file = path.join(directory, name);
      const info = await stat(file);
      return { file, modified: info.mtimeMs, size: info.size };
    }));
  candidates.sort((a, b) => b.modified - a.modified);
  // セッション開始直後の transcript は「作られたが 0 バイト」の時間が数十秒ある。
  // 最新が空なら中身のある直近ファイルを返す (開いた瞬間に空画面を見せない)。
  // 空ファイルに書き込みが始まれば mtime が進んで自然に乗り換わる
  const nonEmpty = candidates.find((c) => c.size > 0);
  return (nonEmpty ?? candidates[0])?.file ?? null;
}

export async function latestAction(role: AgentRole, projectId: string): Promise<{ text: string; available: boolean }> {
  const file = await findTranscriptFile(role, projectId);
  if (!file) return { text: "transcript を待機中", available: false };
  try {
    const handle = await open(/* turbopackIgnore: true */ file, "r");
    const info = await handle.stat();
    const length = Math.min(info.size, 64 * 1024);
    const buffer = Buffer.alloc(length);
    await handle.read(buffer, 0, length, info.size - length);
    await handle.close();
    const normalized: TranscriptEvent[] = [];
    const lines = buffer.toString("utf8").split("\n");
    for (const [index, line] of lines.entries()) {
      if (!line.trim()) continue;
      try {
        for (const event of normalizeTranscriptEvent(JSON.parse(line), `preview-${index}`)) {
          const merged = mergeTranscriptEvent(normalized, event);
          normalized.splice(0, normalized.length, ...merged);
        }
      } catch {
        // A partial first line is expected when reading a tail slice.
      }
    }
    const visible = normalized.reverse().find((event) => event.kind === "tool" || event.kind === "message" || event.kind === "error");
    if (visible) {
      const text = visible.kind === "tool" ? `${visible.toolName ?? "tool"} を実行` : visible.text ?? "処理中";
      return { text: text.replace(/\s+/g, " ").slice(0, 110), available: true };
    }
  } catch {
    return { text: "transcript を読み取れません", available: true };
  }
  return { text: "セッション開始", available: true };
}

export async function streamTranscript(
  agentId: string,
  emit: (event: string, data: unknown) => void,
  signal: AbortSignal,
): Promise<void> {
  const parsed = parseAgentName(agentId);
  if (!parsed) throw new Error("invalid agent id");
  let activeFile: string | null = null;
  let offset = 0;
  let carry = "";
  let lineNumber = 0;
  let decoder = new StringDecoder("utf8");

  while (!signal.aborted) {
    const file = await findTranscriptFile(parsed.role, parsed.projectId);
    if (!file) {
      emit("status", { state: "waiting", message: "transcript を待機中" });
    } else {
      if (file !== activeFile) {
        activeFile = file;
        offset = 0;
        carry = "";
        lineNumber = 0;
        decoder = new StringDecoder("utf8");
        emit("reset", { file: path.basename(file) });
      }
      const info = await stat(/* turbopackIgnore: true */ file);
      if (info.size < offset) {
        offset = 0;
        carry = "";
        decoder = new StringDecoder("utf8");
        emit("reset", { file: path.basename(file) });
      }
      if (info.size > offset) {
        const handle = await open(/* turbopackIgnore: true */ file, "r");
        while (offset < info.size) {
          const buffer = Buffer.alloc(Math.min(64 * 1024, info.size - offset));
          const { bytesRead } = await handle.read(buffer, 0, buffer.length, offset);
          if (!bytesRead) break;
          offset += bytesRead;
          const chunks = (carry + decoder.write(buffer.subarray(0, bytesRead))).split("\n");
          carry = chunks.pop() ?? "";
          for (const line of chunks) {
            if (!line.trim()) continue;
            lineNumber += 1;
            try {
              for (const event of normalizeTranscriptEvent(JSON.parse(line), `line-${lineNumber}`)) {
                emit("transcript", event);
              }
            } catch {
              emit("parse-error", { line: lineNumber });
            }
          }
        }
        await handle.close();
      }
    }
    // push 型 tail: transcripts ディレクトリを fs.watch (inotify) し、書き込みが
    // あった瞬間に起きる。watch が張れない (ディレクトリ未作成等) 場合や取りこぼし
    // の保険として 5 秒のタイムアウトを併用する (2026-08-22、固定 1 秒ポーリングから変更)
    await new Promise<void>((resolve) => {
      const directory = path.join(DATA_DIR, "transcripts", transcriptMode(parsed.role));
      let watcher: ReturnType<typeof watch> | undefined;
      const done = () => { try { watcher?.close(); } catch { /* noop */ } clearTimeout(timer); resolve(); };
      const timer = setTimeout(done, 5000);
      try {
        watcher = watch(/* turbopackIgnore: true */ directory, done);
        watcher.on("error", () => { /* タイムアウトに任せる */ });
      } catch { /* ディレクトリ未作成: タイムアウトで再試行 */ }
      signal.addEventListener("abort", done, { once: true });
    });
  }
}
