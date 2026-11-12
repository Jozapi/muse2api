"""回归测试：任务列表里别的任务的「Stop task」按钮不能被当成「本轮还在生成」。

muse.ai 的任务列表会给每个还在跑的任务放一个 aria-label="Stop task" 的按钮。账号里只要
有一个挂住不结束的旧任务，旧的选择器 button[aria-label*="Stop" i] 就永远命中，
回覆早已写完也要等满 chat_timeout（300s）才以 MuseGenerationError 收场。

这里只做静态检查（没有浏览器）；真实 DOM 上的行为在部署时用 CDP 验证。
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine as m  # noqa: E402


class StaleStopTaskTests(unittest.TestCase):
    def test_chat_poll_does_not_treat_any_stop_labelled_button_as_generating(self):
        js = m.MuseEngine._POLL_CHAT_JS
        # 不能再用「存在任何含 Stop 的按钮」当判定。
        self.assertNotIn("document.querySelector('button[aria-label*=\"Stop\" i]')", js)
        # 候选按钮必须逐个过滤掉任务列表的 Stop task。
        self.assertIn(".some(function(b)", js)
        self.assertRegex(js, r"!/task\|[^/]*/i\.test\(b\.getAttribute\('aria-label'\)")

    def test_chat_poll_still_recognises_the_composer_stop_button(self):
        js = m.MuseEngine._POLL_CHAT_JS
        self.assertIn("b.getAttribute('data-testid')==='hatch-composer-stop-button'", js)
        self.assertTrue(re.search(r"stop:stop", js))


if __name__ == "__main__":
    unittest.main()
