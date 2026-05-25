"""
Reflect 节点 + _normalize_error 单元测试（Day 7-8 引入）。

背景：
  Day 9 baseline 上 D9-11 连续 3 次 attempt 全报 incomplete input。即使
  _strip_code_fence bug 修了，未来 LLM 在新题上仍可能陷入"反复犯同样错"。
  Reflect 节点 Day 7-8 升级：检测连续相同 SQLite error，触发"换思路"强化
  提示，让 LLM 跳出循环。

  本测试集锚定：
  1. _normalize_error 把同类错误归一到一致签名（列名不同但错误类别相同也算同类）
  2. _reflect 在连续相同错误上正确 bump repeat_count
  3. 错误类别变化时 repeat_count 归零（说明 LLM 真的换了思路）
"""
import unittest

from src.agent import _normalize_error, _reflect


class TestNormalizeError(unittest.TestCase):

    def test_no_such_column_with_target(self):
        sig = _normalize_error("no such column: EKKO.NETWR")
        self.assertEqual(sig, "no_such_column:EKKO.NETWR")

    def test_no_such_column_different_columns_distinct_signature(self):
        a = _normalize_error("no such column: EKKO.NETWR")
        b = _normalize_error("no such column: EKKO.WAERS")
        self.assertNotEqual(a, b, "列名不同应得到不同签名（LLM 在改）")

    def test_no_such_table(self):
        sig = _normalize_error("no such table: VBRK")
        self.assertEqual(sig, "no_such_table:VBRK")

    def test_incomplete_input(self):
        sig = _normalize_error("incomplete input")
        self.assertEqual(sig, "incomplete_input")

    def test_multiple_statements(self):
        sig = _normalize_error("You can only execute one statement at a time.")
        self.assertEqual(sig, "multiple_statements")

    def test_syntax_error(self):
        sig = _normalize_error('near "FROM": syntax error')
        self.assertEqual(sig, "syntax_error")

    def test_empty_error(self):
        self.assertEqual(_normalize_error(""), "")

    def test_unknown_error_truncated(self):
        msg = "totally unknown sqlite drama " * 10
        sig = _normalize_error(msg)
        self.assertEqual(len(sig), 60)


class TestReflectNode(unittest.TestCase):

    def _state(self, history, prev_repeat=0):
        return {
            "history": history,
            "attempt": history[-1]["attempt"] if history else 0,
            "repeat_count": prev_repeat,
        }

    def test_first_failure_no_repeat(self):
        state = self._state([
            {"attempt": 0, "ok": False, "error": "incomplete input", "row_count": 0},
        ])
        out = _reflect(state)
        self.assertEqual(out["repeat_count"], 0, "只有 1 次失败不应触发 repeat_count")
        self.assertEqual(out["last_error_signature"], "incomplete_input")
        self.assertEqual(out["attempt"], 1)

    def test_two_consecutive_same_errors_repeat_1(self):
        state = self._state([
            {"attempt": 0, "ok": False, "error": "incomplete input", "row_count": 0},
            {"attempt": 1, "ok": False, "error": "incomplete input", "row_count": 0},
        ])
        out = _reflect(state)
        self.assertEqual(out["repeat_count"], 1, "连续两次同错误应 repeat_count=1")
        self.assertEqual(out["last_error_signature"], "incomplete_input")

    def test_three_consecutive_same_errors_accumulate(self):
        """D9-11 修复前的场景：连续 3 次同 error，应累积到 2。"""
        state = self._state([
            {"attempt": 0, "ok": False, "error": "incomplete input", "row_count": 0},
            {"attempt": 1, "ok": False, "error": "incomplete input", "row_count": 0},
            {"attempt": 2, "ok": False, "error": "incomplete input", "row_count": 0},
        ], prev_repeat=1)
        out = _reflect(state)
        self.assertEqual(out["repeat_count"], 2)

    def test_different_columns_same_kind_distinct(self):
        """LLM 在尝试改列名但仍幻觉到另一列 —— 签名不同，repeat 归零。"""
        state = self._state([
            {"attempt": 0, "ok": False, "error": "no such column: EKKO.NETWR",
             "row_count": 0},
            {"attempt": 1, "ok": False, "error": "no such column: EKKO.WAERS",
             "row_count": 0},
        ])
        out = _reflect(state)
        self.assertEqual(out["repeat_count"], 0,
                         "列名变化说明 LLM 在尝试不同方向，不算重复")

    def test_recovery_clears_repeat(self):
        """前次失败 + 当次成功 → 签名空，repeat 归零（虽然 reflect 不会被路由到）。"""
        state = self._state([
            {"attempt": 0, "ok": False, "error": "incomplete input", "row_count": 0},
            {"attempt": 1, "ok": True, "error": "", "row_count": 5},
        ])
        out = _reflect(state)
        self.assertEqual(out["repeat_count"], 0)
        self.assertEqual(out["last_error_signature"], "")


if __name__ == "__main__":
    unittest.main()
