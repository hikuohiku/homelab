import type { TranscriptEvent } from "./types";

export function mergeTranscriptEvent(events: TranscriptEvent[], incoming: TranscriptEvent): TranscriptEvent[] {
  if (incoming.kind !== "tool") return [...events, incoming];
  const index = events.findIndex((event) => event.kind === "tool" && event.id === incoming.id);
  if (index < 0) return [...events, incoming];
  const next = [...events];
  next[index] = {
    ...next[index],
    ...incoming,
    input: incoming.input ?? next[index].input,
    output: incoming.output ?? next[index].output,
    toolName: incoming.toolName === "tool" ? next[index].toolName : incoming.toolName,
  };
  return next;
}
