"""Discord 通知 (webhook 一方向) + outbox fallback + 通知予算。

型は 4 つ:
  announce — プロジェクト予告 (拒否権窓と veto 手順を必ず載せる)
  deliver  — 納品報告 (何が変わったか 3 行)
  question — 人間にしか決められないことを聞く
  incident — 障害・停滞
review 型 (レビュー停滞) は incident と同じ扱いで送る。

Discord が死んでいても仕事は止めない: outbox.jsonl に積み、次のビートで
再送する。連続失敗時は issue #56 にフォールバックする (人間への到達経路を
一本に依存しない)。即時送信は rules.notify.daily_budget 件/日まで。
超過分は outbox に "digest" マークで積み、daily briefing に畳む。
"""

import json
import urllib.error
import urllib.request

from .statefiles import now_iso

IMMEDIATE_TYPES = ("announce", "deliver", "question", "incident", "review")


def veto_footer(project_id, deadline):
    lines = [
        f"拒否するには issue #56 に `veto {project_id}` とコメント"
        "（ダッシュボードの書き置きでも可）。走行中でも veto は有効です。",
    ]
    if deadline:
        lines.insert(0, f"着手予定: {deadline} (拒否権窓)")
    return "\n".join(lines)


class Notifier:
    def __init__(self, webhook_url, statefiles, rules, gh=None, feedback_issue=56):
        self.webhook = webhook_url
        self.state = statefiles
        self.rules = rules
        self.gh = gh
        self.issue = feedback_issue

    def _sent_today(self, today):
        return sum(
            1
            for r in self.state.read_jsonl("sent.jsonl")
            if r.get("at", "").startswith(today)
        )

    def _post_discord(self, text):
        if not self.webhook:
            raise RuntimeError("DISCORD_WEBHOOK_URL 未設定")
        req = urllib.request.Request(
            self.webhook,
            data=json.dumps({"content": text[:1900]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15):
            pass

    def send(self, ntype, text, now=None):
        """即時送信を試みる。予算超過・失敗は outbox 行き。戻り値は実送信の有無。"""
        at = now_iso(now)
        today = at[:10]
        if ntype not in IMMEDIATE_TYPES or self._sent_today(today) >= self.rules[
            "notify"
        ]["daily_budget"]:
            self.state.append_jsonl(
                "outbox.jsonl", {"at": at, "type": ntype, "text": text, "digest": True}
            )
            return False
        try:
            self._post_discord(f"[{ntype}] {text}")
            self.state.append_jsonl("sent.jsonl", {"at": at, "type": ntype, "text": text})
            return True
        except (urllib.error.URLError, OSError, RuntimeError) as e:
            self.state.append_jsonl(
                "outbox.jsonl",
                {"at": at, "type": ntype, "text": text, "error": str(e)[:200]},
            )
            return False

    def flush_outbox(self, now=None):
        """未送信の即時分を再送する。2 回連続で送れないものは issue #56 へ落とす。

        digest 分 (予算超過で畳んだもの) は daily briefing が回収する予定だが、
        briefing の実装 (Phase 3) まで放置すると永久に届かない (レビュー指摘 [12])。
        24h を超えた digest 分はまとめて issue #56 に代送して掃く。"""
        pending = self.state.read_jsonl("outbox.jsonl")
        if not pending:
            return
        from .statefiles import parse_iso
        from datetime import timedelta

        now_dt = parse_iso(now_iso(now))
        stale_digest = [
            r for r in pending
            if r.get("digest") and now_dt - parse_iso(r["at"]) > timedelta(hours=24)
        ]
        if stale_digest and self.gh is not None:
            try:
                body = "\n".join(
                    f"- [{r['type']}] {r['at']} {r['text'][:300]}" for r in stale_digest
                )
                self.gh.comment_issue(
                    self.issue,
                    f"(通知予算超過分のまとめ、{len(stale_digest)} 件)\n{body}"[:60000],
                )
                pending = [r for r in pending if r not in stale_digest]
            except Exception:
                pass
        remaining = []
        for rec in pending:
            if rec.get("digest"):
                remaining.append(rec)  # 24h 以内の digest 分は briefing 待ち
                continue
            try:
                self._post_discord(f"[{rec['type']}] {rec['text']}")
                self.state.append_jsonl(
                    "sent.jsonl",
                    {"at": now_iso(now), "type": rec["type"], "text": rec["text"]},
                )
            except (urllib.error.URLError, OSError, RuntimeError) as e:
                rec["retries"] = rec.get("retries", 0) + 1
                rec["error"] = str(e)[:200]
                if rec["retries"] >= 2 and self.gh is not None:
                    try:
                        self.gh.comment_issue(
                            self.issue,
                            f"(Discord 不達のため代送) [{rec['type']}] {rec['text']}",
                        )
                        continue
                    except Exception:
                        pass
                remaining.append(rec)
        self.rewrite_outbox(remaining)

    def rewrite_outbox(self, records):
        self.state.rewrite_jsonl("outbox.jsonl", records)


def main():
    """疎通試験用 CLI (プラン検証 #3)。

    使い方: DISCORD_WEBHOOK_URL=... python3 -m ops.heart.notify "テスト本文"
    """
    import os
    import sys

    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    text = sys.argv[1] if len(sys.argv) > 1 else "heart notify 疎通試験"
    req = urllib.request.Request(
        url,
        data=json.dumps({"content": f"[test] {text}"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        print(f"sent: HTTP {resp.status}")


if __name__ == "__main__":
    main()
