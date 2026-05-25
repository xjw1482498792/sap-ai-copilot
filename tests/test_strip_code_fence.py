"""
_strip_code_fence 单元测试（Day 7-8 引入）。

背景：
  Day 9 baseline 暴露 D9-11「今年每月销售额环比增长率」上 Agent 模式 3 次
  attempt 全报 incomplete input，根因是 _strip_code_fence 把"以中文别名开头
  的字段列表行"（如 `  月份,`）误判为话术段落起点，整段主 SELECT 后内容
  被砍掉。

  本测试集锚定修复后行为：
  1. 中文别名 / WITH CTE 字段列表不应被截断（D9-11 复现 case）
  2. 真正的中文话术段落（含明显 trigger 词）应被截断（Day 4 经验里的
     "等等，总金额需要..." 这种 self-correction 噪声）
  3. markdown 围栏 + SQL 关键字定位等 Day 1-4 原有行为不能回归
"""
import unittest

from src.main import _strip_code_fence


class TestStripCodeFence(unittest.TestCase):

    # ===== Day 7 新增：D9-11 中文别名场景不应被截断 =====

    def test_d9_11_chinese_alias_in_select_list_not_truncated(self):
        """D9-11 复现 case：主 SELECT 后是中文别名字段列表，必须完整保留。"""
        llm_output = (
            "WITH monthly_sales AS (\n"
            "  SELECT\n"
            "    STRFTIME('%Y-%m', ERDAT) AS 月份,\n"
            "    SUM(NETWR) AS 月销售额\n"
            "  FROM VBAK\n"
            "  WHERE ERDAT >= '2026-01-01' AND ERDAT < '2026-06-01'\n"
            "  GROUP BY STRFTIME('%Y-%m', ERDAT)\n"
            ")\n"
            "SELECT\n"
            "  月份,\n"
            "  月销售额,\n"
            "  LAG(月销售额) OVER (ORDER BY 月份) AS 上月销售额,\n"
            "  ROUND((月销售额 - LAG(月销售额) OVER (ORDER BY 月份)) * 100.0 / "
            "LAG(月销售额) OVER (ORDER BY 月份), 2) AS 环比增长率\n"
            "FROM monthly_sales\n"
            "ORDER BY 月份"
        )
        out = _strip_code_fence(llm_output)
        self.assertIn("FROM monthly_sales", out, "字段列表不应被截掉，FROM 子句应保留")
        self.assertIn("ORDER BY 月份", out, "ORDER BY 子句应保留")
        self.assertIn("环比增长率", out, "中文别名应保留")

    def test_chinese_alias_no_indent_not_truncated(self):
        """无缩进的中文别名行也不应被截断（LLM 偶尔写无缩进 SQL）。"""
        llm_output = (
            "SELECT\n"
            "月份,\n"
            "月销售额\n"
            "FROM VBAK"
        )
        out = _strip_code_fence(llm_output)
        self.assertIn("FROM VBAK", out)
        self.assertIn("月销售额", out)

    # ===== Day 4 经验：真正的中文话术应被截断 =====

    def test_trailing_prose_truncated(self):
        """LLM 给完 SQL 后自言自语的中文段落必须被截。"""
        llm_output = (
            "SELECT COUNT(*) FROM KNA1\n"
            "这条 SQL 统计了客户总数。"
        )
        out = _strip_code_fence(llm_output)
        self.assertEqual(out.strip(), "SELECT COUNT(*) FROM KNA1")
        self.assertNotIn("这条", out)

    def test_self_correction_prose_truncated(self):
        """Day 4 经验里 "等等，总金额需要..." 这类自我修正噪声必须被截。"""
        llm_output = (
            "SELECT SUM(NETWR) FROM VBAK\n"
            "等等，需要 JOIN VBAP 再 SUM(NETPR * KWMENG)\n"
            "SELECT SUM(VBAP.NETPR * VBAP.KWMENG) FROM VBAK JOIN VBAP ON VBAK.VBELN = VBAP.VBELN"
        )
        out = _strip_code_fence(llm_output)
        self.assertEqual(out.strip(), "SELECT SUM(NETWR) FROM VBAK")
        self.assertNotIn("等等", out)
        self.assertNotIn("JOIN VBAP", out, "二次 SQL 也应被截掉")

    def test_chinese_punctuation_triggers_truncation(self):
        """以中文句号 / 问号 / 感叹号结尾的中文行视为话术。"""
        llm_output = (
            "SELECT * FROM MARA\n"
            "上面的查询返回所有物料。"
        )
        out = _strip_code_fence(llm_output)
        self.assertEqual(out.strip(), "SELECT * FROM MARA")

    # ===== Day 1-4 原有行为不能回归 =====

    def test_markdown_fence_stripped(self):
        out = _strip_code_fence("```sql\nSELECT 1\n```")
        self.assertEqual(out, "SELECT 1")

    def test_markdown_fence_no_lang(self):
        out = _strip_code_fence("```\nSELECT 1\n```")
        self.assertEqual(out, "SELECT 1")

    def test_leading_prose_before_sql(self):
        """tool calling 后续轮 LLM 偶尔加开场白 → 用 SELECT 关键字定位起点。"""
        out = _strip_code_fence("现在可以生成 SQL 了。\nSELECT * FROM KNA1")
        self.assertEqual(out, "SELECT * FROM KNA1")

    def test_with_cte_starts_with_with(self):
        """WITH 也是 SQL 起点关键字。"""
        out = _strip_code_fence("WITH t AS (SELECT 1) SELECT * FROM t")
        self.assertEqual(out, "WITH t AS (SELECT 1) SELECT * FROM t")

    def test_trailing_semicolon_stripped(self):
        out = _strip_code_fence("SELECT 1;")
        self.assertEqual(out, "SELECT 1")

    def test_fence_after_sql_truncates(self):
        """SQL 后跟 ``` 围栏（再 + 二次胡话）的场景。"""
        out = _strip_code_fence(
            "SELECT * FROM KNA1\n```\n等等，重新写：\nSELECT COUNT(*) FROM KNA1"
        )
        self.assertEqual(out.strip(), "SELECT * FROM KNA1")

    def test_plain_sql_unchanged(self):
        out = _strip_code_fence("SELECT * FROM VBAK WHERE BUKRS = '1000'")
        self.assertEqual(out, "SELECT * FROM VBAK WHERE BUKRS = '1000'")


if __name__ == "__main__":
    unittest.main()
