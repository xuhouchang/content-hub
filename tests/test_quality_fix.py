#!/usr/bin/env python3
"""公众号文章质量修复方案 — offline verification gates.

Runnable with stdlib unittest only. No network, no model calls, no WeChat sends.

Run from repo root:
    python tests/test_quality_fix.py

Covers 方案 §五-B:
  1. 正常素材能构造结构化上下文
  2. 缺少 key_signal / 标签 / 来源字段时仍能运行
  3. 跨主题素材不会被默认借入（signal-borrow disabled）
  4. 蓝图缺少机制/边界时被拒绝
  5. 空模型响应不会触发 .strip() 异常
  6. 文章质量闸门能识别"素材复述型"低质量文章
  7. 合格文章能通过检查
"""
import os
import sys
import json
import io
import contextlib
from pathlib import Path
from unittest import TestCase, mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import write_article as wa
from content_platform.business.daily_article import topic_planner as tp


class TestStructuredContext(TestCase):
    def test_normal_material_constructs_structured_context(self):
        m = [{
            "url": "https://example.com/a",
            "title": "物料A",
            "content": "这篇报告显示73%的企业在部署AI后面临决策瓶颈。机制是数据分散导致判断滞后。",
            "key_signal": "AI落地后真正的瓶颈是决策判断而非技术",
        }]
        ctx = wa.build_material_context(m)
        self.assertIn("素材 1", ctx)
        self.assertIn("73%", ctx)  # data heuristic extracted
        self.assertIn("独特信号", ctx)
        self.assertIn("https://example.com/a", ctx)

    def test_missing_fields_still_run(self):
        # 没有 key_signal / title / tags / source 也绝不能崩
        m = [{"url": "", "content": "只有正文内容，没有任何结构化字段，也不应该崩溃。"}]
        ctx = wa.build_material_context(m)
        self.assertIsInstance(ctx, str)
        self.assertIn("素材 1", ctx)


class TestSignalBorrowDisabled(TestCase):
    def test_no_cross_cluster_borrow(self):
        """Signal-borrow removed：best cluster < 2 signals 时不得从其他聚类借入。"""
        clusters = {
            "主题A": [{"url": "https://a.com/1", "score": 90, "content": "A素材"}],
            "主题B": [{"url": "https://b.com/1", "score": 80, "content": "B素材"}],
        }
        # Stub signals DB so best cluster (A) has only 1 signal -> old code would borrow from B
        from lib import materials as mat
        with mock.patch.object(mat, "_load_key_signals", return_value={"https://a.com/1": "sig"}):
            selected = mat._pick_best_cluster(clusters, max_count=5)
        urls = {s.get("url", "") for s in selected}
        # Must ONLY contain cluster A materials; B must not be borrowed in.
        self.assertIn("https://a.com/1", urls)
        self.assertNotIn("https://b.com/1", urls)


class TestBlueprintValidation(TestCase):
    def _bp(self, **over):
        base = {
            "topic": "t",
            "thesis": "AI落地后真正的瓶颈是企业决策判断链，而非技术本身",
            "why_now": "now",
            "evidence": ["证据1", "证据2"],
            "mechanism_chain": ["现象", "中间机制", "结果"],
            "counterargument": "有人认为只要换工具就够",
            "boundary": "仅适用于中大型企业",
            "outline": ["a", "b", "c"],
            "title_candidates": ["t1", "t2", "t3"],
        }
        base.update(over)
        return base

    def test_valid_blueprint_passes(self):
        r = wa.validate_blueprint(self._bp())
        self.assertTrue(r["ok"], r)

    def test_missing_mechanism_rejected(self):
        r = wa.validate_blueprint(self._bp(mechanism_chain=["现象"]))
        self.assertFalse(r["ok"])
        self.assertTrue(any("机制链" in x or "mechanism_chain" in x for x in r["reasons"]))

    def test_missing_counter_and_boundary_rejected(self):
        r = wa.validate_blueprint(self._bp(counterargument="", boundary=""))
        self.assertFalse(r["ok"])
        self.assertTrue(any("反方观点或边界" in x for x in r["reasons"]))

    def test_generic_thesis_rejected(self):
        r = wa.validate_blueprint(self._bp(thesis="AI正在快速发展"))
        self.assertFalse(r["ok"])
        self.assertTrue(any("泛判断" in x for x in r["reasons"]))

    def test_insufficient_rejected(self):
        r = wa.validate_blueprint({"insufficient": True, "reason": "素材不足"})
        self.assertFalse(r["ok"])

    def test_extract_blueprint_tolerates_fence(self):
        bp = self._bp()
        raw = "```json\n" + json.dumps(bp, ensure_ascii=False) + "\n```"
        out = wa.extract_blueprint(raw)
        self.assertEqual(out["thesis"], bp["thesis"])

    def test_extract_blueprint_none_safe(self):
        self.assertEqual(wa.extract_blueprint(None), {})
        self.assertEqual(wa.extract_blueprint(""), {})


class TestEmptyModelResponse(TestCase):
    def test_topic_planner_none_strip_safe(self):
        """call_model 返回 None 时不得 NoneType.strip，应返回保守 DISTINCT=False。"""
        with mock.patch("lib.llm.call_model", return_value=None):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                result = tp._llm_judge_topic_similar("标题", "摘要", [{"title": "旧"}], model="m")
        self.assertFalse(result)

    def test_topic_planner_empty_strip_safe(self):
        with mock.patch("lib.llm.call_model", return_value="   "):
            result = tp._llm_judge_topic_similar("标题", "摘要", [{"title": "旧"}], model="m")
        self.assertFalse(result)

    def test_call_model_never_returns_empty_whitespace(self):
        """顶层 call_model 必须把空/纯空白结果压成 None。"""
        import lib.llm as llm
        with mock.patch.object(llm, "call_deepseek_direct", return_value="   "):
            self.assertIsNone(llm.call_model([{"role": "user", "content": "x"}], max_tokens=10))


class TestQualityGate(TestCase):
    def test_material_dump_detected(self):
        """素材复述型低质量文章应被判为未通过或明显低于门槛。"""
        dump = """# 素材报告解读

摘要: 这是一篇素材复述型文章，展示多个独立素材段落的直接堆叠。

=== 素材 1 ===
原文摘录: 这是第一段原文。外部报告的结论是A。

原文摘录: 这是第二段原文。外部报告的结论是A的补充数据。

原文摘录: 这是第三段原文。外部报告又说了一遍A。三段的本质都是同一素材的复述。

原文摘录: 这是第四段原文。外部报告在第三段基础上再展开A。
"""
        q = wa.check_article_quality(dump, title="素材报告解读")
        # 素材复述型：要么被识别出复述，要么整体未通过闸门
        self.assertTrue((not q["checks"]["not_material_dump"]) or (not q["passed"]), q)

    def test_good_article_passes(self):
        good = """# 判断：组织跟不上才是AI落地的真正瓶颈

摘要: 真正卡住AI落地的不是技术。

过去一年，73%的企业在部署AI后仍面临决策瓶颈。数据分散导致判断滞后，进而让试点项目无法复制。因为决策链没有重构，所以即使工具再先进，一线也用不起来。这因此形成了"有工具没结果"的死循环。

不过，这套判断并不适用于单独开发者——没有组织层变量时，个人工具的ROI依然直接。研究来源：https://example.com/a；另一篇：https://example.com/b。

另一个代价是：重构决策链需要跨越部门利益，这往往是最大的隐性成本。
"""
        q = wa.check_article_quality(good, title="组织跟不上才是AI落地的真正瓶颈")
        self.assertTrue(q["passed"], q)
        self.assertGreaterEqual(q["score"], 6)


if __name__ == "__main__":
    import unittest
    unittest.main(verbosity=2)
