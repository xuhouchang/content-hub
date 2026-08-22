#!/usr/bin/env python3
"""LLM 语义质量评估（quality_judge）offline 验证。

纯 stdlib unittest + mock，不联网、不调真实模型、不触碰发布。

覆盖：
  1. run_llm_judge：DeepSeek 直连（经 lib.llm.call_model）返回严格 JSON -> 解析成功
  2. 容错：code-fence、前后杂文 -> 仍能解析
  3. fail-safe：LLM 返回空 / 非法 / 抛错 / verdict 非法 -> 一律 unavailable 且不伪造通过
  4. evaluate_quality：规则不过 => 不浪费 LLM，直接不可通过
  5. evaluate_quality：规则过 + LLM pass => 通过（status=rules+llm）
  6. evaluate_quality：规则过 + LLM fail => 不可通过
  7. evaluate_quality：规则过 + LLM unavailable => 不可通过 + degraded warning
  8. write_article.evaluate_article_quality：默认 degraded（仅规则）不伪造通过；
     开启 QUALITY_LLM_JUDGE 时叠加 LLM。
"""
import unittest
from unittest import mock
from pathlib import Path
import sys
import json

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import lib.quality_judge as qj
import write_article as wa

GOOD_ARTICLE = """# 组织跟不上才是AI落地的真正瓶颈

摘要: 真正卡住AI落地的不是技术。

过去一年，73%的企业在部署AI后仍面临决策瓶颈。数据分散导致判断滞后，进而让试点项目无法复制。因为决策链没有重构，所以即使工具再先进，一线也用不起来。这因此形成了"有工具没结果"的死循环。

不过，这套判断并不适用于单独开发者——没有组织层变量时，个人工具的ROI依然直接。研究来源：https://example.com/a；另一篇：https://example.com/b。

另一个代价是：重构决策链需要跨越部门利益，这往往是最大的隐性成本。
"""

_MUST_FIX = "主张缺乏可追溯来源、存在素材堆砌"


def _pass_json():
    return json.dumps({
        "verdict": "pass", "overall_score": 4, "overall_reason": "结构完整、判断清晰",
        "dimensions": {d: {"score": 4, "reason": "ok", "suggestion": "可再压缩"} for d in qj.DIMENSIONS},
        "must_fix": [],
    }, ensure_ascii=False)


def _fail_json():
    return json.dumps({
        "verdict": "fail", "overall_score": 2, "overall_reason": "机制缺失",
        "dimensions": {d: {"score": 2, "reason": "弱", "suggestion": "补足"} for d in qj.DIMENSIONS},
        "must_fix": [_MUST_FIX],
    }, ensure_ascii=False)


class TestRunLLMJudge(unittest.TestCase):
    def _call(self, retval):
        return mock.patch("lib.quality_judge.call_model", return_value=retval)

    def test_strict_json_parsed(self):
        with self._call(_pass_json()):
            r = qj.run_llm_judge(GOOD_ARTICLE, title="组织跟不上才是瓶颈")
        self.assertEqual(r["verdict"], "pass")
        self.assertEqual(r["overall_score"], 4)
        self.assertEqual(set(r["dimensions"].keys()), set(qj.DIMENSIONS))
        self.assertEqual(list(r["dimensions"]["thesis"].keys()), ["score", "reason", "suggestion"])

    def test_code_fence_tolerated(self):
        raw = "```json\n" + _pass_json() + "\n```"
        with self._call(raw):
            r = qj.run_llm_judge(GOOD_ARTICLE)
        self.assertEqual(r["verdict"], "pass")

    def test_stray_text_tolerated(self):
        raw = "以下是评估结果。\n" + _fail_json() + "\n希望对你有帮助。"
        with self._call(raw):
            r = qj.run_llm_judge(GOOD_ARTICLE)
        self.assertEqual(r["verdict"], "fail")
        self.assertIn(_MUST_FIX, r["must_fix"])

    def test_none_response_unavailable(self):
        with self._call(None):
            r = qj.run_llm_judge(GOOD_ARTICLE)
        self.assertEqual(r["verdict"], "unavailable")
        self.assertEqual(r["overall_score"], 0)

    def test_empty_response_unavailable(self):
        with self._call("   "):
            r = qj.run_llm_judge(GOOD_ARTICLE)
        self.assertEqual(r["verdict"], "unavailable")

    def test_invalid_json_unavailable(self):
        with self._call("这不是JSON"):
            r = qj.run_llm_judge(GOOD_ARTICLE)
        self.assertEqual(r["verdict"], "unavailable")

    def test_exception_not_fake_pass(self):
        with mock.patch("lib.quality_judge.call_model", side_effect=RuntimeError("boom")):
            r = qj.run_llm_judge(GOOD_ARTICLE)
        self.assertEqual(r["verdict"], "unavailable")
        self.assertIn("调用失败", r["overall_reason"])

    def test_invalid_verdict_unavailable(self):
        bad = json.dumps({"verdict": "maybe", "dimensions": {}, "must_fix": []})
        with self._call(bad):
            r = qj.run_llm_judge(GOOD_ARTICLE)
        self.assertEqual(r["verdict"], "unavailable")

    def test_scores_clamped_1_5(self):
        obj = json.loads(_pass_json())
        obj["dimensions"]["evidence"]["score"] = 99
        obj["dimensions"]["style"]["score"] = -3
        with self._call(json.dumps(obj)):
            r = qj.run_llm_judge(GOOD_ARTICLE)
        self.assertEqual(r["dimensions"]["evidence"]["score"], 5)
        self.assertEqual(r["dimensions"]["style"]["score"], 1)


class TestEvaluateQuality(unittest.TestCase):
    def _rules_pass(self):
        return {
            "passed": True, "score": 8,
            "checks": {k: True for k in
                       ("has_thesis","has_mechanism","has_evidence","has_counterpoint_or_boundary",
                        "has_source_traceability","not_material_dump","title_is_judgment","no_empty_content")},
            "warnings": [], "reasons": [],
        }

    def test_rules_fail_no_llm_call(self):
        rules = dict(self._rules_pass()); rules["passed"] = False; rules["score"] = 5
        with mock.patch.object(qj, "run_llm_judge") as mj:
            r = qj.evaluate_quality(GOOD_ARTICLE, rule_gate=lambda t, **k: rules)
        self.assertFalse(r["passed"])
        self.assertEqual(r["status"], "rules")
        mj.assert_not_called()

    def test_rules_pass_llm_pass(self):
        with mock.patch.object(qj, "run_llm_judge",
                               return_value={"verdict": "pass", "overall_score": 4,
                                             "overall_reason": "ok", "dimensions": {}, "must_fix": []}):
            r = qj.evaluate_quality(GOOD_ARTICLE, rule_gate=lambda t, **k: self._rules_pass())
        self.assertTrue(r["passed"])
        self.assertEqual(r["status"], "rules+llm")

    def test_rules_pass_llm_fail(self):
        with mock.patch.object(qj, "run_llm_judge",
                               return_value={"verdict": "fail", "overall_score": 2,
                                             "overall_reason": "机制缺失", "dimensions": {}, "must_fix": [_MUST_FIX]}):
            r = qj.evaluate_quality(GOOD_ARTICLE, rule_gate=lambda t, **k: self._rules_pass())
        self.assertFalse(r["passed"])
        self.assertEqual(r["status"], "rules+llm")
        self.assertTrue(any(_MUST_FIX in x for x in r["reasons"]))

    def test_rules_pass_llm_unavailable_no_fake_pass(self):
        with mock.patch.object(qj, "run_llm_judge",
                               return_value={"verdict": "unavailable", "overall_score": 0,
                                             "overall_reason": "无凭证", "dimensions": {}, "must_fix": ["无凭证"]}):
            r = qj.evaluate_quality(GOOD_ARTICLE, rule_gate=lambda t, **k: self._rules_pass())
        self.assertFalse(r["passed"])  # 绝不伪造通过
        self.assertEqual(r["status"], "unavailable")
        self.assertTrue(r["warnings"], "应有 degraded 提示")


class TestWriteArticleIntegration(unittest.TestCase):
    def test_default_degraded_rules_only(self):
        """未开启 QUALITY_LLM_JUDGE 时，走纯规则，不触发 LLM。"""
        with mock.patch("lib.quality_judge.call_model", side_effect=AssertionError("不应调用LLM")):
            with mock.patch.dict("os.environ", {}, clear=False):
                r = wa.evaluate_article_quality(GOOD_ARTICLE, title="组织跟不上才是瓶颈")
        self.assertEqual(r["status"], "rules")
        self.assertTrue(r["passed"])  # 好文章规则层即可过，但不做语义复核

    def test_enabled_integration_with_llm_pass(self):
        with mock.patch("lib.quality_judge.call_model", return_value=_pass_json()) as m:
            with mock.patch.dict("os.environ", {"QUALITY_LLM_JUDGE": "1"}, clear=False):
                r = wa.evaluate_article_quality(GOOD_ARTICLE, title="组织跟不上才是瓶颈")
        m.assert_called_once()
        self.assertEqual(r["status"], "rules+llm")
        self.assertTrue(r["passed"])
        self.assertEqual(r["llm"]["verdict"], "pass")

    def test_check_article_quality_backcompat(self):
        """老入口行为不变。"""
        q = wa.check_article_quality(GOOD_ARTICLE, title="组织跟不上才是瓶颈")
        self.assertTrue(q["passed"])
        self.assertNotIn("status", q)


if __name__ == "__main__":
    unittest.main(verbosity=2)
