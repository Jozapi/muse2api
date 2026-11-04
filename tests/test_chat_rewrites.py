"""muse.ai 在生成途中改寫已顯示的文字時，回覆不能變成「截斷的一份 + 完整的一份」。

可用 `python -m unittest tests.test_chat_rewrites` 或 pytest 執行。
"""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine as m  # noqa: E402


def fake_engine(snapshots):
    """每次輪詢回傳下一個快照；最後一個快照持續出現，且 Stop 按鈕已消失。"""
    clock = [0.0]
    m.time = SimpleNamespace(time=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s))
    frames = iter([''] + list(snapshots))  # 送出前的基準輪詢：還沒有回覆
    state = {'current': ''}

    def poll():
        try:
            state['current'] = next(frames)
            generating = True
        except StopIteration:
            generating = False
        return (1 if state['current'] else 0), state['current'], generating

    engine = object.__new__(m.MuseEngine)
    engine.cfg = SimpleNamespace(chat_timeout=60)
    engine.ensure_page = lambda *a, **k: None
    engine.reset_thread = lambda **k: None
    engine._send = lambda prompt: None
    engine._poll_chat = poll
    engine.page = SimpleNamespace(js=lambda script: '')
    return engine


# 實際抓到的形狀：串流途中 `[` 被藏起來，渲染完成後才出現。
JSON_FRAMES = ['{"terms":{"原文":"天城"', '{"terms":{"原文":"天城","譯名":"天城"},{"原文":"教会"',
               '{"terms":[{"原文":"天城","譯名":"天城"},{"原文":"教会","譯名":"教會"}]}']
PROSE_FINAL = '禮拜堂被騷動包圍了。\n\n「S＋？」\n\n「不愧是天城！」\n\n他抬起下巴。'
PROSE_FRAMES = ['禮拜堂被騷動', '禮拜堂被騷動包圍了。\n\n"S', '禮拜堂被騷動包圍了。\n\n「S＋？」\n\n「不愧',
                '禮拜堂被騷動包圍了。\n\n「S＋？」\n\n「不愧是天城！」\n\n他抬起', PROSE_FINAL]


class StreamTextTests(unittest.TestCase):
    def test_append_only_text_goes_out_line_by_line_and_adds_up(self):
        text = m.StreamText()
        pieces = [text.update('第一行\n第二'), text.update('第一行\n第二行\n第三'), text.finish()]
        self.assertEqual(pieces, ['第一行\n', '第二行\n', '第三'])
        self.assertEqual(''.join(pieces), '第一行\n第二行\n第三')
        self.assertEqual(text.rewrites, 0)

    def test_a_rewrite_of_sent_text_never_sends_the_whole_answer_again(self):
        text = m.StreamText()
        pieces = [text.update('a "b\n'), text.update('a 「b」\nc\n'), text.finish()]
        joined = ''.join(pieces)
        self.assertEqual(joined.count('a '), 1)
        self.assertGreater(text.rewrites, 0)


class ChatTests(unittest.TestCase):
    def test_non_streaming_returns_exactly_the_final_json(self):
        reply = fake_engine(JSON_FRAMES).chat({}, 'prompt')
        self.assertEqual(reply, JSON_FRAMES[-1])
        self.assertEqual(json.loads(reply)['terms'][1]['譯名'], '教會')

    def test_non_streaming_returns_exactly_the_final_prose(self):
        self.assertEqual(fake_engine(PROSE_FRAMES).chat({}, 'prompt'), PROSE_FINAL)

    def test_streaming_never_repeats_the_answer(self):
        for frames in (JSON_FRAMES, PROSE_FRAMES):
            with self.subTest(final=frames[-1][:10]):
                joined = ''.join(fake_engine(frames).chat_stream({}, 'prompt'))
                start = frames[-1][:6]
                self.assertEqual(joined.count(start), 1, joined)
                self.assertLessEqual(len(joined), len(frames[-1]) + 4)

    def test_streaming_matches_the_final_text_when_nothing_is_rewritten(self):
        frames = ['第一段\n', '第一段\n\n第二段', '第一段\n\n第二段寫完。']
        self.assertEqual(''.join(fake_engine(frames).chat_stream({}, 'prompt')), frames[-1])


if __name__ == '__main__':
    unittest.main()
