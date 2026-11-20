"""聊天轮询不能每 60ms 都对整页所有元素做 getComputedStyle：滚动容器要缓存复用。

用 node 跑真正的 _POLL_CHAT_JS / _scroll_bottom 脚本，配一个假的 document 计算
querySelectorAll('*') 被调用了几次。
"""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine as m  # noqa: E402

FAKE_DOM = r"""
let now = 0, fullScans = 0, styleReads = 0;
Date.now = () => now;
function el(sh, ch, overflow) {
  return {scrollHeight: sh, clientHeight: ch, scrollTop: 0, isConnected: true,
          _overflow: overflow, className: '', innerText: ''};
}
const scroller = el(5000, 600, 'auto');
const plain = Array.from({length: 50}, () => el(10, 10, 'visible'));
let all = [scroller, ...plain];
const bubble = {className: 'hatch-chat-groupable-bubble hatch-agent-bubble-bg', innerText: '你好'};
global.window = {};
global.getComputedStyle = e => { styleReads++; return {overflowY: e._overflow}; };
global.document = {
  body: {scrollTop: 0, scrollHeight: 0},
  scrollingElement: null,
  querySelectorAll: sel => {
    if (sel === '*') { fullScans++; return all; }
    if (sel.indexOf('hatch-chat-groupable-bubble') !== -1) return [bubble];
    return [];
  },
  querySelector: () => null,
};
document.body.querySelectorAll = document.querySelectorAll;
function run(script) { return (0, eval)(script); }
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class PollScrollCacheTests(unittest.TestCase):
    def _node(self, body):
        script = FAKE_DOM + "\nconst POLL = %s;\n%s" % (json.dumps(m.MuseEngine._POLL_CHAT_JS), body)
        out = subprocess.check_output(["node", "-e", script], text=True).strip()
        return json.loads(out)

    def test_repeated_polls_reuse_the_scroller(self):
        res = self._node("""
          let last;
          for (let i = 0; i < 30; i++) { now += 60; last = JSON.parse(run(POLL)); }
          console.log(JSON.stringify({fullScans, scrolled: scroller.scrollTop, last}));
        """)
        # 30 次轮询共 1.8 秒，只该扫一次整页；结果与原脚本一致。
        self.assertEqual(res["fullScans"], 1)
        self.assertEqual(res["scrolled"], 5000)
        self.assertEqual(res["last"]["txt"], "你好")
        self.assertEqual(res["last"]["cnt"], 1)

    def test_rescans_after_two_seconds_and_when_scroller_detaches(self):
        res = self._node("""
          run(POLL); now += 2000; run(POLL);          // 2 秒后重扫一次
          const afterAge = fullScans;
          scroller.isConnected = false; now += 60; run(POLL);   // 容器被移出 DOM
          console.log(JSON.stringify({afterAge, fullScans}));
        """)
        self.assertEqual(res["afterAge"], 2)
        self.assertEqual(res["fullScans"], 3)

    def test_missing_scroller_is_rescanned_soon(self):
        res = self._node("""
          all = plain;                               // 还没有可滚动的内容
          run(POLL); now += 60; run(POLL);           // 250ms 内不重复扫
          const early = fullScans;
          all = [scroller, ...plain]; now += 250; run(POLL);
          console.log(JSON.stringify({early, fullScans, scrolled: scroller.scrollTop}));
        """)
        self.assertEqual(res["early"], 1)
        self.assertEqual(res["fullScans"], 2)
        self.assertEqual(res["scrolled"], 5000)


class StuckCheckThrottleTests(unittest.TestCase):
    def test_body_text_is_read_about_once_a_second_while_waiting(self):
        clock = [0.0]
        old_time = m.time
        m.time = type("T", (), {"time": staticmethod(lambda: clock[0]),
                                "sleep": staticmethod(lambda s: clock.__setitem__(0, clock[0] + s))})
        reads = {"n": 0}

        def js(script):
            if "innerText.slice" in script:
                reads["n"] += 1
            return ""

        try:
            engine = object.__new__(m.MuseEngine)
            engine.cfg = type("C", (), {"chat_timeout": 60})()
            engine.ensure_page = lambda *a, **k: None
            engine.reset_thread = lambda **k: None
            engine._send = lambda prompt: None
            engine._poll_chat = lambda: (0, "", False)
            engine.page = type("P", (), {"js": staticmethod(js)})()
            with self.assertRaises(m.MuseGenerationError):
                list(engine.chat_stream({}, "hi", timeout=40))
        finally:
            m.time = old_time
        # 首字等待 40 秒：14 秒后约 26 秒，每秒一次 ≈ 26 次，而不是每 60ms 一次（≈430 次）。
        self.assertLess(reads["n"], 30)
        self.assertGreater(reads["n"], 20)


if __name__ == "__main__":
    unittest.main()
