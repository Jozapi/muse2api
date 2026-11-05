"""对话可靠性修复的回归测试：

1. Stop 按钮还在（助手仍在计划/调用工具/吐字）时绝不提前把中间状态当成最终回覆，
   不管它看起来多"稳定"——旧逻辑在 has_stop 时允许等到 stable>=80（约 5s）就提前
   返回，模型先吐一句计划、再跑 >5s 工具调用时就会把计划句子当成答案返回。
2. Stop 按钮消失后，气泡数与文字都要稳定 MUSE_CHAT_STABLE_SECONDS 秒（默认 1.5s，
   可调）才算结束，这样"计划之后另开一个新气泡才出现最终答案"的情况也能等到。
3. muse.ai 把失败渲染成看起来正常的助手气泡文字时（而不是 HTTP 错误），要判定为
   生成失败并在吐出任何内容前抛出，好让 app.py 的重试/换号路径接管，不会 200。
4. 默认每个对话请求都另开新会话（MUSE_CHAT_FRESH_THREAD=1），避免多个请求堆叠在
   同一个 thread 里造成上下文串话；MUSE_CHAT_FRESH_THREAD=0 时恢复旧的复用行为。

可用 `python -m unittest tests.test_chat_reliability` 或 pytest 执行。
"""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine as m  # noqa: E402


def fake_engine_seq(frames, base=(0, "", False), chat_timeout=60):
    """按顺序把 frames 里的 (cnt, text, has_stop) 依次喂给每次 _poll_chat() 调用；
    耗尽后最后一帧永远重复（模拟轮询仍在继续但页面状态不再变化）。
    chat_stream 自己在每轮循环里调用 time.sleep(0.06) 推进模拟时钟，所以不需要真的
    等待就能验证"稳定/卡住 N 秒"之类的时间判定。"""
    clock = [0.0]
    m.time = SimpleNamespace(time=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s))
    seq = [base] + list(frames)
    state = {"i": 0}

    def poll():
        i = min(state["i"], len(seq) - 1)
        state["i"] += 1
        return seq[i]

    engine = object.__new__(m.MuseEngine)
    engine.cfg = SimpleNamespace(chat_timeout=chat_timeout)
    engine.ensure_page = lambda *a, **k: None
    engine.reset_thread = lambda **k: None
    engine._send = lambda prompt: None
    engine._poll_chat = poll
    engine.page = SimpleNamespace(js=lambda script: "")
    return engine


def _with_env(key, value):
    """临时设置/清除一个环境变量，返回恢复函数。"""
    old = os.environ.get(key)

    def restore():
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old

    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value
    return restore


PLANNING = "我將先確認翻譯規範，再開始動筆。"
FINAL = "翻譯完成：「你好，世界。」"


class CompletionTimingTests(unittest.TestCase):
    def setUp(self):
        self._restores = [_with_env("MUSE_CHAT_STABLE_SECONDS", None),
                           _with_env("MUSE_CHAT_ERROR_PATTERNS", None)]

    def tearDown(self):
        for restore in self._restores:
            restore()

    def test_does_not_return_the_planning_line_while_tool_steps_keep_stop_visible(self):
        """复现线上问题：助手先吐一句计划，然后长时间执行工具调用（Stop 按钮一直在，
        这里模拟 7.2s），旧逻辑在计划文字稳定 ~5s 后就把它当最终回覆返回；新逻辑必须
        一直等到 Stop 消失、新气泡里的最终答案也稳定下来才返回。"""
        frames = (
            [(1, PLANNING, True)] * 120     # 120*0.06s = 7.2s，Stop 一直在，远超旧的 5s 上限
            + [(2, FINAL, True)] * 5         # 新气泡（最终答案）出现，仍在生成
            + [(2, FINAL, False)]            # Stop 消失
        )
        reply = fake_engine_seq(frames).chat({}, "prompt")
        self.assertEqual(reply, FINAL)

    def test_stop_visible_long_pause_never_finalizes_early(self):
        """纯时间判定：文字稳定不变但 Stop 按钮一直在（这里模拟到 9s，远超旧的 5s 上限）
        都不该提前结束；一旦 Stop 消失并稳定住，才用那份文字结束。"""
        frames = (
            [(1, "草稿", True)] * 150        # 9s 模拟时间
            + [(1, "草稿", False)] * 30       # Stop 消失后稳定 30*0.06=1.8s（> 默认 1.5s）
        )
        reply = fake_engine_seq(frames).chat({}, "prompt")
        self.assertEqual(reply, "草稿")

    def test_multiple_new_boxes_after_stop_disappears_take_the_last_one(self):
        """Stop 消失的同一时刻又跳出一个新气泡（agent 气泡数继续变化）：
        不能用旧的那份文字结束，要继续等到气泡数和文字都稳定。"""
        frames = [
            (1, PLANNING, True),
            (2, FINAL, False),      # 气泡数变化了，还不能当作"稳定"
        ] + [(2, FINAL, False)] * 30
        reply = fake_engine_seq(frames).chat({}, "prompt")
        self.assertEqual(reply, FINAL)

    def test_stable_window_is_configurable(self):
        restore = _with_env("MUSE_CHAT_STABLE_SECONDS", "0.1")
        try:
            frames = [(1, FINAL, True)] + [(1, FINAL, False)] * 5
            reply = fake_engine_seq(frames).chat({}, "prompt")
            self.assertEqual(reply, FINAL)
        finally:
            restore()


class ErrorDetectionTests(unittest.TestCase):
    def setUp(self):
        self._restore = _with_env("MUSE_CHAT_ERROR_PATTERNS", None)

    def tearDown(self):
        self._restore()

    def test_muse_error_bubble_raises_generation_error_instead_of_200(self):
        frames = [(1, "Sorry, I ran into a problem while responding.", False)]
        with self.assertRaises(m.MuseGenerationError):
            fake_engine_seq(frames).chat({}, "prompt")

    def test_error_is_detected_before_any_chunk_is_yielded(self):
        """app.py 的切号重试要求：命中错误串时绝不能先吐出任何 chunk——
        safe_chat_stream 一旦看到 yielded=True 就不会再切号重试了。"""
        frames = [(1, "Sorry, I ran into a problem while responding.", False)]
        gen = fake_engine_seq(frames).chat_stream({}, "prompt")
        with self.assertRaises(m.MuseGenerationError):
            next(gen)

    def test_error_patterns_are_overridable_via_env(self):
        restore = _with_env("MUSE_CHAT_ERROR_PATTERNS", "快取已滿")
        try:
            frames = [(1, "快取已滿，請稍後再試。", False)]
            with self.assertRaises(m.MuseGenerationError):
                fake_engine_seq(frames).chat({}, "prompt")
        finally:
            restore()

    def test_default_patterns_do_not_misfire_on_an_ordinary_reply(self):
        frames = [(1, "這是一段正常的回覆，沒有任何問題。", False)] * 3
        reply = fake_engine_seq(frames).chat({}, "prompt")
        self.assertEqual(reply, "這是一段正常的回覆，沒有任何問題。")

    def test_quoted_error_line_in_the_middle_of_a_long_reply_is_not_a_false_positive(self):
        """旧逻辑只做子串匹配：翻译/转述里原样提到这句英文就会被误判为失败。
        新逻辑要求命中串在开头、或在结尾且全文短，这段引用既不在开头也不在结尾。"""
        text = (
            'The character said: "Sorry, I ran into a problem, but I will fix it." '
            "Then she continued the story for several more paragraphs explaining how "
            "the hero eventually succeeded against all odds, bringing the tale to a "
            "satisfying conclusion that spanned well beyond three hundred characters "
            "in total length, closing the chapter on a hopeful note for everyone."
        )
        self.assertGreaterEqual(len(text), 300)
        frames = [(1, text, False)] * 3
        reply = fake_engine_seq(frames).chat({}, "prompt")
        self.assertEqual(reply, text)

    def test_error_line_exactly_at_the_end_of_a_short_reply_still_matches(self):
        text = "抱歉，刚才网络有点问题。\nSorry, I ran into a problem"
        frames = [(1, text, False)] * 3
        with self.assertRaises(m.MuseGenerationError):
            fake_engine_seq(frames).chat({}, "prompt")

    def test_error_detected_on_the_final_text_after_partial_output_already_streamed(self):
        """流式已经吐出一段真实内容后，后续快照才变成错误气泡：之前吐出的内容无法撤回，
        但绝不能把错误文字当成正式回覆返回——必须在它出现的那一刻就抛出。"""
        good = "A" * 280 + "\n"
        broken = good + "Sorry, I ran into a problem"
        frames = [(1, good, False), (1, broken, False)]
        gen = fake_engine_seq(frames).chat_stream({}, "prompt")
        first = next(gen)
        self.assertEqual(first, good)
        with self.assertRaises(m.MuseGenerationError):
            next(gen)


class StreamBubbleResetTests(unittest.TestCase):
    """StreamText 必须按气泡（_poll_chat 返回的 cnt）重置，绝不能拿新气泡的文字去
    对旧气泡已发出的长度做切片，否则会拼出「计划句 + 被截断的正式回覆」。"""

    def setUp(self):
        self._restores = [_with_env("MUSE_CHAT_STABLE_SECONDS", None),
                           _with_env("MUSE_STREAM_HOLDBACK_CHARS", "0")]

    def tearDown(self):
        for restore in self._restores:
            restore()

    def test_new_bubble_is_not_sliced_against_the_previous_bubbles_sent_length(self):
        """复现报告里的 bug：旧写法在气泡切换时继续用同一个 StreamText，
        "PLANNING\\n" 发完后，更短的新气泡 "FINAL ANSWER" 被切成了 "WER"。"""
        frames = [(1, "PLANNING\n", True), (2, "FINAL ANSWER", True),
                  (2, "FINAL ANSWER", False)]
        gen = fake_engine_seq(frames).chat_stream({}, "prompt")
        chunks = list(gen)
        # 旧 bug 的结果是 "PLANNING\nWER"（第二个气泡被当成第一个气泡的改写切片）；
        # 正确结果必须完整保留两个气泡各自的文字。
        self.assertEqual("".join(chunks), "PLANNING\nFINAL ANSWER")

    def test_non_stream_chat_only_returns_the_last_bubble_not_the_planning_prefix(self):
        frames = [(1, "PLANNING\n", True), (2, "FINAL ANSWER", True),
                  (2, "FINAL ANSWER", False)]
        reply = fake_engine_seq(frames).chat({}, "prompt")
        self.assertEqual(reply, "FINAL ANSWER")


class StreamHoldbackTests(unittest.TestCase):
    """短文字（计划句、muse.ai 伪装的简短错误气泡）在完成分类前不该被吐给客户端——
    这样 app.py 看到还没 yield 过任何内容时仍可以安全地切号重试。"""

    def setUp(self):
        self._restores = [_with_env("MUSE_CHAT_STABLE_SECONDS", None),
                           _with_env("MUSE_STREAM_HOLDBACK_CHARS", None)]

    def tearDown(self):
        for restore in self._restores:
            restore()

    def test_short_multiline_text_is_not_streamed_piecemeal_before_completion(self):
        """默认 holdback=200：两行文字全程没超过阈值，不该先吐 "Line1\\n" 再补 "Line2"，
        该整段一次性在收尾时送出。"""
        frames = [(1, "Line1\n", True), (1, "Line1\nLine2", True)] + [
            (1, "Line1\nLine2", False)]
        gen = fake_engine_seq(frames).chat_stream({}, "prompt")
        chunks = list(gen)
        self.assertEqual(chunks, ["Line1\nLine2"])

    def test_holdback_threshold_is_configurable_and_allows_early_streaming(self):
        restore = _with_env("MUSE_STREAM_HOLDBACK_CHARS", "5")
        try:
            frames = [(1, "Hello\n", True), (1, "Hello\nWorld", False)]
            gen = fake_engine_seq(frames).chat_stream({}, "prompt")
            first = next(gen)
            self.assertEqual(first, "Hello\n")
        finally:
            restore()


class PlanningOnlyReplyTests(unittest.TestCase):
    """最终稳定下来的仍是送出后的第一个气泡、且像一句计划话术时，先多等
    MUSE_CHAT_PLANNING_GRACE 秒看是否还有正式回覆；真的没有就判失败，而不是把
    "我將..." 直接当成答案返回。"""

    def setUp(self):
        self._restores = [_with_env("MUSE_CHAT_STABLE_SECONDS", None),
                           _with_env("MUSE_CHAT_PLANNING_GRACE", None)]

    def tearDown(self):
        for restore in self._restores:
            restore()

    def test_planning_only_reply_with_nothing_further_raises(self):
        frames = [(1, PLANNING, False)]
        with self.assertRaises(m.MuseGenerationError):
            fake_engine_seq(frames).chat({}, "prompt")

    def test_real_answer_arriving_within_the_grace_window_is_still_returned(self):
        frames = [(1, PLANNING, False), (2, FINAL, False)]
        reply = fake_engine_seq(frames).chat({}, "prompt")
        self.assertEqual(reply, FINAL)

    def test_grace_window_is_configurable(self):
        restore = _with_env("MUSE_CHAT_PLANNING_GRACE", "0.01")
        try:
            frames = [(1, PLANNING, False)]
            with self.assertRaises(m.MuseGenerationError):
                fake_engine_seq(frames).chat({}, "prompt")
        finally:
            restore()

    def test_short_ordinary_english_reply_is_not_flagged_as_planning(self):
        """"I'll be there tomorrow." 以 I'll 开头且很短，但不含任何"在讲规范/文件"
        之类的元话题关键词，不该被当成计划句而多等一轮 grace。"""
        text = "I'll be there tomorrow."
        frames = [(1, text, False)]
        reply = fake_engine_seq(frames).chat({}, "prompt")
        self.assertEqual(reply, text)

    def test_short_chinese_translation_is_not_flagged_as_planning(self):
        text = "我會回來的。"
        frames = [(1, text, False)]
        reply = fake_engine_seq(frames).chat({}, "prompt")
        self.assertEqual(reply, text)

    def test_planning_sentence_with_meta_keyword_still_raises_when_nothing_follows(self):
        frames = [(1, "我將先確認翻譯規範，再進行全文翻譯。", False)]
        with self.assertRaises(m.MuseGenerationError):
            fake_engine_seq(frames).chat({}, "prompt")

    def test_english_planning_sentence_with_meta_keyword_still_raises_when_nothing_follows(self):
        frames = [(1, "I'll read the translation skill file first.", False)]
        with self.assertRaises(m.MuseGenerationError):
            fake_engine_seq(frames).chat({}, "prompt")


class StalePollFallbackTests(unittest.TestCase):
    """第一个新气泡出现后，若某次轮询抓拍到 cnt 退回 <= base_agent（最新气泡暂时空了
    或被旧气泡顶替），必须当成"还没就位"忽略掉，绝不能拿那次快照去结束对话。"""

    def setUp(self):
        self._restore = _with_env("MUSE_CHAT_STABLE_SECONDS", None)

    def tearDown(self):
        self._restore()

    def test_a_stale_poll_back_to_the_baseline_bubble_is_ignored(self):
        frames = [(1, "NEW ANSWER", False), (0, "", False)] + [
            (1, "NEW ANSWER", False)] * 30
        reply = fake_engine_seq(frames).chat({}, "prompt")
        self.assertEqual(reply, "NEW ANSWER")

    def test_bubble_count_drop_below_the_max_seen_is_never_used_to_finalize(self):
        """1->2->1：本轮已经见过气泡数到达 2（真正出现过的最终气泡），随后快照又退回
        到 1 并一直卡在那（不仅仅是退回到 baseline 以下）。绝不能把退回后、看起来
        "稳定"的旧气泡内容当成最终回覆返回——应该一直忽略直到整体超时。"""
        frames = [(1, "DRAFT", True), (2, "FINAL", True), (1, "DRAFT", False)]
        with self.assertRaises(m.MuseGenerationError):
            fake_engine_seq(frames, chat_timeout=1).chat({}, "prompt")


class FreshThreadTests(unittest.TestCase):
    """reset_thread(for_chat=True) 是否另开新会话，按 MUSE_CHAT_FRESH_THREAD 决定。"""

    def setUp(self):
        self._restore = _with_env("MUSE_CHAT_FRESH_THREAD", None)

    def tearDown(self):
        self._restore()

    def _run(self, bubble_count: int, env_value: str | None):
        _with_env("MUSE_CHAT_FRESH_THREAD", env_value)
        navigated = {"n": 0}
        seen = {}

        class Page:
            def __init__(self):
                self.ready_polls = 0

            def js(self, script):
                if "freshChat" in script:
                    seen["script"] = script
                    fresh_chat = script.rstrip().endswith("(true, true)")
                    return (bubble_count > 0) if fresh_chat else (bubble_count >= 24)
                self.ready_polls += 1
                return self.ready_polls >= 1

            def send(self, method, params=None):
                if method == "Page.navigate":
                    navigated["n"] += 1

        engine = object.__new__(m.MuseEngine)
        engine._wait_ws_ready = lambda *a, **k: True
        engine.page = Page()
        engine.reset_thread(for_chat=True)
        return navigated["n"], seen.get("script", "")

    def test_default_opens_a_new_thread_even_with_only_a_few_bubbles(self):
        n, script = self._run(bubble_count=3, env_value=None)
        self.assertTrue(script.rstrip().endswith("(true, true)"), script)
        self.assertEqual(n, 1)

    def test_env_1_is_equivalent_to_the_default(self):
        n, _ = self._run(bubble_count=3, env_value="1")
        self.assertEqual(n, 1)

    def test_env_0_restores_the_old_reuse_below_24_bubbles(self):
        n, script = self._run(bubble_count=3, env_value="0")
        self.assertTrue(script.rstrip().endswith("(true, false)"), script)
        self.assertEqual(n, 0)

    def test_env_0_still_navigates_once_the_old_threshold_is_hit(self):
        n, _ = self._run(bubble_count=24, env_value="0")
        self.assertEqual(n, 1)


class FreshThreadFailOpenTests(unittest.TestCase):
    """新会话导航发出后必须确认真的落到了 /thread/new（或气泡数清零）；
    确认不到就该报错让调用方重试，而不是悄悄在旧会话里继续发消息造成串话。"""

    def _fake_clock(self):
        clock = [0.0]
        old_time = m.time
        m.time = SimpleNamespace(time=lambda: clock[0],
                                 sleep=lambda s: clock.__setitem__(0, clock[0] + s))

        def restore():
            m.time = old_time
        return restore

    def test_navigation_that_never_lands_raises_so_the_caller_can_retry(self):
        restore_clock = self._fake_clock()

        class Page:
            def js(self, script):
                if "freshChat" in script:
                    return True
                if "readyState" in script:
                    return False  # 导航后一直没等到就位（气泡数仍 >0）
                if "location.pathname" in script:
                    return False  # 落地检查也确认仍停在旧会话
                return True

            def send(self, method, params=None):
                pass

        try:
            engine = object.__new__(m.MuseEngine)
            engine._wait_ws_ready = lambda *a, **k: True
            engine.page = Page()
            with self.assertRaises(m.MuseGenerationError):
                engine.reset_thread(for_chat=True)
        finally:
            restore_clock()

    def test_navigation_that_lands_despite_a_slow_ready_check_does_not_raise(self):
        restore_clock = self._fake_clock()

        class Page:
            def js(self, script):
                if "freshChat" in script:
                    return True
                if "readyState" in script:
                    return False  # 就位检查没赶上
                if "location.pathname" in script:
                    return True   # 但确实已经落在 /thread/new
                return True

            def send(self, method, params=None):
                pass

        try:
            engine = object.__new__(m.MuseEngine)
            engine._wait_ws_ready = lambda *a, **k: True
            engine.page = Page()
            engine.reset_thread(for_chat=True)  # 不应抛出
        finally:
            restore_clock()

    def test_for_chat_false_does_not_run_the_fail_open_check(self):
        """fail-open 检查只为对话场景加的；非对话调用保持旧行为，不因此报错。"""
        restore_clock = self._fake_clock()

        class Page:
            def js(self, script):
                if "readyState" in script:
                    return False
                if "location.pathname" in script:
                    return False
                return True

            def send(self, method, params=None):
                pass

        try:
            engine = object.__new__(m.MuseEngine)
            engine._wait_ws_ready = lambda *a, **k: True
            engine.page = Page()
            engine.reset_thread(for_chat=False)  # 不应抛出
        finally:
            restore_clock()

    def test_exception_from_page_navigate_raises_for_chat(self):
        """Page.navigate 本身抛异常时，旧代码会被 except Exception: pass 吞掉，
        继续在旧 thread 里发消息造成串话；对话场景下必须让异常变成
        MuseGenerationError 往上抛，让调用方重试，而不是悄悄放行。"""
        restore_clock = self._fake_clock()

        class Page:
            def js(self, script):
                if "freshChat" in script:
                    return True
                return True

            def send(self, method, params=None):
                if method == "Page.navigate":
                    raise RuntimeError("boom")

        try:
            engine = object.__new__(m.MuseEngine)
            engine._wait_ws_ready = lambda *a, **k: True
            engine.page = Page()
            with self.assertRaises(m.MuseGenerationError):
                engine.reset_thread(for_chat=True)
        finally:
            restore_clock()

    def test_exception_from_readiness_check_raises_for_chat(self):
        """导航后轮询就位状态的 js() 调用抛异常，同样不能被吞掉。"""
        restore_clock = self._fake_clock()

        class Page:
            def js(self, script):
                if "freshChat" in script:
                    return True
                if "readyState" in script:
                    raise RuntimeError("boom")
                return True

            def send(self, method, params=None):
                pass

        try:
            engine = object.__new__(m.MuseEngine)
            engine._wait_ws_ready = lambda *a, **k: True
            engine.page = Page()
            with self.assertRaises(m.MuseGenerationError):
                engine.reset_thread(for_chat=True)
        finally:
            restore_clock()

    def test_exception_during_navigation_is_still_swallowed_for_non_chat(self):
        """非对话（image/video）场景保持旧行为：导航相关异常被吞掉，不抛出。"""
        restore_clock = self._fake_clock()

        class Page:
            def js(self, script):
                if "readyState" in script:
                    raise RuntimeError("boom")
                return True

            def send(self, method, params=None):
                pass

        try:
            engine = object.__new__(m.MuseEngine)
            engine._wait_ws_ready = lambda *a, **k: True
            engine.page = Page()
            engine.reset_thread(for_chat=False)  # 不应抛出
        finally:
            restore_clock()


if __name__ == "__main__":
    unittest.main()
