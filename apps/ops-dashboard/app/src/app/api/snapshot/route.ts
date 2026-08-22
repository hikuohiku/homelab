import { getSnapshot } from "@/lib/snapshot";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  const snapshot = await getSnapshot();
  return Response.json(snapshot, {
    headers: { "Cache-Control": "no-store" },
  });
}

