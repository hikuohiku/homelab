"""人間フィードバックの一次分類。

初期実装はキーワードルールのみ (LLM ゼロ)。弱いモデルにエスカレーション判断を
させない、という設計判断 (プラン決定 #9 の背景) をここで守る:
「止めて」系と veto はモデルを経ずに決定論で拾う。
分類できなかったものは classify() が "review_needed" を返し、heart が
daily briefing に載せて人間と consolidation に見せる。取りこぼしても
「無視される」ではなく「翌日の briefing に出る」に倒す。

将来 LLM 分類に切り替えるときも、stop/veto の決定論パスは残すこと
(models.json の triage を "keyword" 以外にしたときの実装条件)。
"""

import re

VETO_RE = re.compile(r"veto\s+(P-\d{4})", re.IGNORECASE)


def _matches_stop(text, kw):
    """停止キーワードの一致判定。素朴な部分文字列一致だと、長文フィードバック中の
    『〜で止めてしまう』のような叙述から偽の全停止を拾う (レビュー指摘 [26])。
    命令として書かれた形だけを拾う:
      - いずれかの行がキーワードで始まる (「止めてください」「stop everything」)
      - または全体が短い (50 文字以下) メッセージにキーワードが含まれる
    ASCII キーワードは単語境界も要求する ("stop" が "backstop" に当たらない)。"""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if kw.isascii():
        head = re.compile(rf"{re.escape(kw)}\b", re.IGNORECASE)
        word = re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE)
        if any(head.match(line) for line in lines):
            return True
        return len(text) <= 50 and bool(word.search(text))
    if any(line.startswith(kw) for line in lines):
        return True
    return len(text) <= 50 and kw in text


def classify(text, rules):
    """1 コメント/書き置き -> {"kind": ..., "projects": [...]}

    kind: "stop_all" | "veto" | "resume_all" | "review_needed"
    """
    text = text or ""
    vetoes = VETO_RE.findall(text)
    if vetoes:
        return {"kind": "veto", "projects": [v.upper() for v in vetoes]}
    for kw in rules["veto"]["stop_keywords"]:
        if _matches_stop(text, kw):
            return {"kind": "stop_all", "projects": []}
    # 全停止 (stop_engaged) の解除。stop の後に評価する = 両方に読める文は停止に倒す
    for kw in rules["veto"].get("resume_keywords", []):
        if _matches_stop(text, kw):
            return {"kind": "resume_all", "projects": []}
    return {"kind": "review_needed", "projects": []}
