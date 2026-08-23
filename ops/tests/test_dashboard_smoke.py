"""ops/tools/dashboard_smoke.py (P-0193, Mission Control 常設検眼) の判定層を固定する。

リポジトリルートから `python3 -m unittest ops.tests.test_dashboard_smoke`。
dashboard_smoke は ops/tools パッケージ配下なので素の import で読める
(モジュール top-level は定数のみで副作用無し。cluster 外 import 可)。

固定する契約:
- evaluate_dom: 正常ページは全検査 pass。矛盾形状・白画面・スピナ残置・
  古い心拍は対応する検査が鳴り ok が False に倒れる (両方向)
- 矛盾の形状 (critic 08-22 指摘): 正常チップと global-warning /
  HEART SIGNAL LOST の共存、正常チップと要確認チップの混在、
  正常チップなのに LAST HEART 観測なし、心拍表示の古さ
- parse_jst_stamp: 年なし MM/DD 表示の年推定。年末年始の前年巻き戻し、
  数分の clock skew を巻き戻さないこと、閏日の解析 (%m/%d 単独の
  非推奨パスを踏まないこと) を含む
- visible_text: Next.js flight data (__next_f.push) を可視テキストに数えない。
  flight data の中身が矛盾検査を誤発火させないこともここで担保
- find_heart_chips: 実測の React 出力 class="heart-chip " (末尾空白) と
  class="heart-chip heart-chip--bad" の両形を受ける
- check_freshness: max_age_s ちょうどは沈黙 (> でしか鳴らない)、超過で鳴る
"""

import datetime
import unittest
import warnings

from ops.tools import dashboard_smoke as ds

UTC = datetime.timezone.utc


def _jst(year, month, day, hour, minute=0, second=0):
    return datetime.datetime(year, month, day, hour, minute, second, tzinfo=ds.JST)


def _utc(year, month, day, hour, minute=0, second=0):
    return datetime.datetime(year, month, day, hour, minute, second, tzinfo=UTC)


# 実測に合わせた正常系の基準時刻: LAST HEART 表示 "08/23 21:51:08" の 60 秒後
STAMP = "08/23 21:51:08"
NOW = _utc(2026, 8, 23, 12, 52, 8)  # JST 2026-08-23 21:52:08


def chip(ok=True, beat="4213"):
    """page.tsx の heart-chip 出力。ok 側は実測形の末尾空白 class を再現する。"""
    cls = 'class="heart-chip "' if ok else 'class="heart-chip heart-chip--bad"'
    word = "正常" if ok else "要確認"
    return (f"<div {cls}><i></i><span><small>HEART / BEAT {beat}</small>"
            f"<strong>{word}</strong></span></div>")


def page(*, chips=None, last_heart=STAMP, warnings=(), signal_lost=False,
         loading=False):
    """page.tsx の描画結果に合わせた合成 DOM。

    既定値は 2026-08-23 の実サイト実行 (smoke-result.json 全検査合格) と
    同じ正常形。script/style は意図的に混ぜている (visible_text の篩を
    通る経路で断言させるため)。
    """
    if chips is None:
        chips = [chip()]
    chunks = [
        "<!doctype html><html><head>",
        '<script>self.__next_f.push([1,"4e:{\\"heart\\":{\\"stale\\":false}}"])</script>',
        "<style>.masthead{display:flex}.heart-chip{color:#0f0}</style>",
        "</head><body><main>",
        '<header class="masthead">',
        '<a class="identity"><span class="identity__mark">MC</span><span>'
        "<strong>MISSION CONTROL</strong>"
        "<small>HOMELAB AUTOPILOT / NODE01</small></span></a>",
        '<nav aria-label="画面切り替え"><button><span>01</span>エージェント・ライブ</button>'
        "<button><span>02</span>プロジェクト</button></nav>",
        "".join(chips),
        "</header>",
        '<div class="status-line" id="top">',
        "<span>JST 2026/08/23 21:51:30</span>",
        "<span>RUNNING <strong>6</strong></span>",
        "<span>TODAY <strong>$1.23</strong> / 9 sessions</span>",
        f"<span>LAST HEART {last_heart}</span>",
        '<span class="status-line__readonly">READ ONLY</span>',
        "</div>",
    ]
    for w in warnings:
        chunks.append(f'<div class="global-warning">{w}</div>')
    if signal_lost:
        chunks.append('<div class="global-warning">HEART SIGNAL LOST — core 応答なし</div>')
    if loading:
        chunks.append('<div class="loading"><span></span>管制信号を同期中</div>')
    else:
        chunks.append(
            '<section class="agents-strip"><div class="agents-strip__heading">'
            "<span>ACTIVE CHANNELS</span><strong>6</strong></div>"
            '<div class="agent-card"><strong>autopilot</strong>'
            "<small>走行時間 3時間 12分</small></div></section>"
            '<section class="transcript-panel"><h2>LIVE TRANSCRIPT</h2>'
            '<pre class="transcript__body">2026-08-23 21:50:01 セッション開始 '
            "backlog から P-0193 を取得し project/p-0193 へ切り替えた</pre></section>")
    chunks.append("</main></body></html>")
    return "".join(chunks)


def named(result, name):
    return next(c for c in result["checks"] if c["name"] == name)


def statuses(result):
    return {c["name"]: c["status"] for c in result["checks"]}


class VisibleTextTest(unittest.TestCase):
    def test_flight_data_in_script_is_excluded(self):
        # script 内に「HEART SIGNAL LOST」があっても可視テキストではない。
        # これを数えると矛盾検査が flight data で誤発火する
        html = ('<html><body><script>self.__next_f.push([1,"HEART SIGNAL LOST"])'
                "</script><p>管制信号は正常です</p></body></html>")
        text = ds.visible_text(html)
        self.assertNotIn("__next_f", text)
        self.assertNotIn("HEART SIGNAL LOST", text)
        self.assertIn("管制信号は正常です", text)

    def test_style_content_is_excluded(self):
        text = ds.visible_text("<style>.loading{color:red}</style><p>本体</p>")
        self.assertNotIn(".loading", text)
        self.assertIn("本体", text)

    def test_charrefs_are_resolved_and_parts_joined(self):
        text = ds.visible_text("<p>a &amp; b &lt;c&gt;</p><p>次の行</p>")
        self.assertEqual(text, "a & b <c> 次の行")

    def test_broken_html_does_not_raise(self):
        # 断言の材料集めで死なない (parser.feed を握りつぶして短く落ちる)
        self.assertIn("一部", ds.visible_text("<p>一部<script>未閉鎖"))


class FindHeartChipsTest(unittest.TestCase):
    MEASURED_OK = ('<div class="heart-chip "><i></i><span>'
                   "<small>HEART / BEAT 4213</small><strong>正常</strong></span></div>")
    MEASURED_BAD = ('<div class="heart-chip heart-chip--bad"><i></i><span>'
                    "<small>HEART / BEAT 4213</small><strong>要確認</strong></span></div>")

    def test_measured_trailing_space_class_matches(self):
        found = ds.find_heart_chips(self.MEASURED_OK)
        self.assertEqual(len(found), 1)
        self.assertTrue(ds.chip_is_ok(found[0]))
        self.assertFalse(ds.chip_is_bad(found[0]))

    def test_bad_modifier_class_matches(self):
        found = ds.find_heart_chips(self.MEASURED_BAD)
        self.assertEqual(len(found), 1)
        self.assertTrue(ds.chip_is_bad(found[0]))
        self.assertFalse(ds.chip_is_ok(found[0]))

    def test_multiline_chip_matches(self):
        # re.S で受けるので属性や子要素の改行を跨げる
        multiline = '<div class="heart-chip ">\n  <i></i>\n  <small>x</small>\n</div>'
        self.assertEqual(len(ds.find_heart_chips(multiline)), 1)

    def test_unrelated_divs_are_ignored(self):
        html = '<div class="agent-card">x</div><div class="global-warning">y</div>'
        self.assertEqual(ds.find_heart_chips(html), [])


class LastHeartLabelTest(unittest.TestCase):
    def test_extracts_stamp_after_mark(self):
        self.assertEqual(ds.last_heart_label(f"<span>LAST HEART {STAMP}</span>"), STAMP)

    def test_extracts_no_observation_label(self):
        self.assertEqual(ds.last_heart_label("<span>LAST HEART 観測なし</span>"), "観測なし")

    def test_absent_mark_returns_none(self):
        self.assertIsNone(ds.last_heart_label("<span>RUNNING 6</span>"))


class ParseJstStampTest(unittest.TestCase):
    def test_basic_parse_returns_aware_jst_datetime(self):
        parsed = ds.parse_jst_stamp(STAMP, NOW)
        self.assertEqual(parsed, _jst(2026, 8, 23, 21, 51, 8))
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed.utcoffset(), datetime.timedelta(hours=9))

    def test_new_year_stamp_is_rolled_back_to_previous_year(self):
        # 大晦日の心拍を元日に見たら前年として解釈する (年は画面に出ない)
        now = _utc(2027, 1, 1, 0, 0, 30)  # JST 09:00:30
        parsed = ds.parse_jst_stamp("12/31 23:59:59", now)
        self.assertEqual(parsed, _jst(2026, 12, 31, 23, 59, 59))

    def test_small_clock_skew_is_not_rolled_back(self):
        # 数分の未来は巻き戻さない (2 日超のときだけ前年に直す契約)
        now = _utc(2026, 8, 23, 12, 47, 8)  # JST 21:47:08
        parsed = ds.parse_jst_stamp("08/23 21:52:00", now)  # 5 分未来
        self.assertEqual(parsed, _jst(2026, 8, 23, 21, 52, 0))

    def test_leap_day_label_parses_without_deprecated_path(self):
        # 閏日の 02/29 を、%m/%d 単独解釈 (Python 3.14 で非推奨) を踏まずに
        # 年を結合した %Y/%m/%d で解くことを固定する。非推奨パスに触れたら
        # simplefilter("error") が例外として落とす
        now = _utc(2028, 2, 29, 15, 1, 0)  # JST 2028-03-01 00:01
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            parsed = ds.parse_jst_stamp("02/29 23:59:26", now)
        self.assertEqual(parsed, _jst(2028, 2, 29, 23, 59, 26))

    def test_unparseable_labels_return_none(self):
        for label in ("観測なし", "", "   ", None, "not-a-time", "25/99 99:99:99"):
            with self.subTest(label=label):
                self.assertIsNone(ds.parse_jst_stamp(label, NOW))


class CheckFreshnessTest(unittest.TestCase):
    HTML = f"<span>LAST HEART {STAMP}</span>"  # 21:51:08 JST

    def _now_after(self, seconds):
        return (_jst(2026, 8, 23, 21, 51, 8)
                + datetime.timedelta(seconds=seconds)).astimezone(UTC)

    def test_exactly_max_age_stays_silent(self):
        # 境界は沈黙側。> のときだけ鳴る (計器の手遅れ型沈黙を避ける既存流儀とは逆で、
        # 「古すぎ判定」は明確な超過でのみ鳴らす)
        result = ds.check_freshness(self.HTML, self._now_after(900))
        self.assertEqual(result[0]["status"], ds.PASS)

    def test_one_second_over_max_age_rings(self):
        result = ds.check_freshness(self.HTML, self._now_after(901))
        self.assertEqual(result[0]["status"], ds.FAIL)
        self.assertIn("古い", result[0]["detail"])

    def test_custom_threshold_is_honored(self):
        result = ds.check_freshness(self.HTML, self._now_after(61),
                                    max_age_s=60.0)
        self.assertEqual(result[0]["status"], ds.FAIL)

    def test_missing_mark_fails_loudly(self):
        result = ds.check_freshness("<span>RUNNING 6</span>", NOW)
        self.assertEqual(result[0]["status"], ds.FAIL)
        self.assertIn("見つからない", result[0]["detail"])

    def test_unparseable_stamp_fails_loudly(self):
        result = ds.check_freshness("<span>LAST HEART 観測なし</span>", NOW)
        self.assertEqual(result[0]["status"], ds.FAIL)
        self.assertIn("解釈できない", result[0]["detail"])


class EvaluateDomHealthyTest(unittest.TestCase):
    def test_healthy_page_is_all_green_with_pinned_check_list(self):
        result = ds.evaluate_dom(page(), now_utc=NOW)
        self.assertTrue(result["ok"])
        # 検査の並び (集約順序) も契約。reporter 側の畳み込みがこの名前に依存する
        self.assertEqual([c["name"] for c in result["checks"]],
                         ["rendered-masthead", "render-complete", "non-blank",
                          "section-heartbeat", "section-live-area",
                          "no-lie-coexistence", "no-mixed-heart-signals",
                          "no-unobserved-pulse", "heartbeat-fresh"])
        by_name = statuses(result)
        failed = {n for n, s in by_name.items() if s == ds.FAIL}
        self.assertEqual(failed, set(), f"失敗した検査: "
                         f"{ {n: named(result, n)['detail'] for n in failed} }")


class EvaluateDomContradictionTest(unittest.TestCase):
    """critic 08-22 指摘の形状。どれか一つでも鳴ったら ok が倒れること。"""

    def test_ok_chip_with_global_warning_coexistence(self):
        result = ds.evaluate_dom(page(warnings=("autopilot pod が再起動ループ",)),
                                 now_utc=NOW)
        check = named(result, "no-lie-coexistence")
        self.assertEqual(check["status"], ds.FAIL)
        self.assertIn("autopilot pod が再起動ループ", check["detail"])
        self.assertIn("共存", check["detail"])
        self.assertFalse(result["ok"])

    def test_ok_chip_with_signal_lost_coexistence(self):
        result = ds.evaluate_dom(page(signal_lost=True), now_utc=NOW)
        check = named(result, "no-lie-coexistence")
        self.assertEqual(check["status"], ds.FAIL)
        self.assertIn(ds.HEART_SIGNAL_LOST_MARK, check["detail"])
        self.assertFalse(result["ok"])

    def test_mixed_ok_and_bad_chips(self):
        result = ds.evaluate_dom(page(chips=[chip(ok=True), chip(ok=False)]),
                                 now_utc=NOW)
        check = named(result, "no-mixed-heart-signals")
        self.assertEqual(check["status"], ds.FAIL)
        self.assertIn("同時に存在", check["detail"])
        # 混在は coexistence 検査も兼ねる (bad チップは reds に載らないが
        # no-mixed-heart-signals が担当)。no-lie-coexistence 自体は鳴らない
        self.assertEqual(named(result, "no-lie-coexistence")["status"], ds.PASS)
        self.assertFalse(result["ok"])

    def test_only_bad_chips_do_not_ring_mixed_check(self):
        # 全チップが要確認なら一貫した状態 (嘘は UI ではなく環境側にある可能性)
        result = ds.evaluate_dom(page(chips=[chip(ok=False)]), now_utc=NOW)
        self.assertEqual(named(result, "no-mixed-heart-signals")["status"], ds.PASS)

    def test_ok_chip_with_unobserved_last_heart(self):
        result = ds.evaluate_dom(page(last_heart="観測なし"), now_utc=NOW)
        check = named(result, "no-unobserved-pulse")
        self.assertEqual(check["status"], ds.FAIL)
        self.assertIn("観測なし", check["detail"])
        self.assertFalse(result["ok"])

    def test_warning_alone_without_chips_is_not_a_contradiction(self):
        # 正常チップが無いなら共存検査の対象外 (chip 有無は section-heartbeat の担当)
        result = ds.evaluate_dom(page(chips=[], warnings=("維持作業中",)), now_utc=NOW)
        check = named(result, "no-lie-coexistence")
        self.assertEqual(check["status"], ds.PASS)
        self.assertIn("対象外", check["detail"])


class EvaluateDomDegradedTest(unittest.TestCase):
    def test_white_screen_fails_rendering_and_sections(self):
        result = ds.evaluate_dom("<!doctype html><html><body></body></html>",
                                 now_utc=NOW)
        by_name = statuses(result)
        for name in ("rendered-masthead", "non-blank",
                     "section-heartbeat", "section-live-area"):
            self.assertEqual(by_name[name], ds.FAIL, name)
        # loading マーク自体は無いので render-complete は鳴らさない (役割分担)
        self.assertEqual(by_name["render-complete"], ds.PASS)
        self.assertFalse(result["ok"])

    def test_loading_spinner_remaining_fails_render_complete(self):
        result = ds.evaluate_dom(page(loading=True), now_utc=NOW)
        check = named(result, "render-complete")
        self.assertEqual(check["status"], ds.FAIL)
        self.assertIn(ds.LOADING_MARK, check["detail"])
        self.assertFalse(result["ok"])

    def test_stale_heartbeat_fails_only_freshness(self):
        result = ds.evaluate_dom(page(last_heart="08/23 18:00:00"), now_utc=NOW)
        by_name = statuses(result)
        self.assertEqual(by_name["heartbeat-fresh"], ds.FAIL)
        self.assertIn("古い", named(result, "heartbeat-fresh")["detail"])
        # 鮮度の失敗が矛盾検査へ漏れない (検査は独立)
        self.assertEqual(by_name["no-lie-coexistence"], ds.PASS)
        self.assertEqual(by_name["no-mixed-heart-signals"], ds.PASS)
        self.assertFalse(result["ok"])


class CheckProjectBoardTest(unittest.TestCase):
    def test_board_title_present_passes(self):
        dom = ("<main><h2>プロジェクトボード</h2>"
               '<div class="board-row">P-0193</div></main>')
        result = ds.check_project_board(dom)
        self.assertEqual(result[0]["status"], ds.PASS)

    def test_board_title_absent_fails(self):
        result = ds.check_project_board("<main><p>エラー</p></main>")
        self.assertEqual(result[0]["status"], ds.FAIL)
        self.assertIn("描画されない", result[0]["detail"])


if __name__ == "__main__":
    unittest.main()
