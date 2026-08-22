import { parseAgentName, streamTranscript } from "@/lib/transcript";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const encoder = new TextEncoder();

export async function GET(request: Request, context: { params: Promise<{ agentId: string }> }) {
  const { agentId } = await context.params;
  if (!parseAgentName(agentId)) return new Response("invalid agent id", { status: 400 });

  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      let closed = false;
      const emit = (event: string, data: unknown) => {
        if (closed) return;
        controller.enqueue(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));
      };
      const keepAlive = setInterval(() => {
        if (!closed) controller.enqueue(encoder.encode(": keep-alive\n\n"));
      }, 15_000);
      emit("connected", { agentId });
      void streamTranscript(agentId, emit, request.signal)
        .catch((error) => emit("stream-error", { message: error instanceof Error ? error.message : String(error) }))
        .finally(() => {
          closed = true;
          clearInterval(keepAlive);
          try { controller.close(); } catch { /* client disconnected */ }
        });
    },
  });
  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
    },
  });
}

