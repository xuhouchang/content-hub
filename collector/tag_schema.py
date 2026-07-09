"""
Material tagging schema: multi-dimensional labels for every collected article.

Each material gets tags from multiple dimensions simultaneously.
The LLM reads the full article content and assigns tags.

## Version 2 — 2026-06-01

Key improvements over V1:
- evidence_type split into evidence_source + evidence_depth (better signal)
- content_form extended with "高管访谈/对话" (unique perspective format)
- tone: "报道/客观" split into "新闻式报道" vs "分析式报道" (passive vs active)
- entity_scope: "跨行业通用" split into "跨行业通用(理论)" vs "跨行业通用(实践)"
- perspective: added "行业专家/从业者视角" for practitioner voices
- topic_focus: added "AI人机交互/体验" (the UX of AI products)
- topic_focus: "AI产品/工具评测" → merged with "AI产品/工具评测&实战记录"
- Content preview in tagging increased from 2000→3000 chars for better evidence_depth detection

Usage:
    from tag_schema import TAG_SCHEMA, TAG_SYSTEM_PROMPT
"""

# ── Multi-dimensional tag schema ──
# 7 active dimensions, 69 total tags (V1 had 6 active + 1 deprecated = 59 tags)
#
# Dimensions: content_form + topic_focus + perspective +
#               evidence_source + evidence_depth + tone + entity_scope

TAG_SCHEMA = {
    "content_form": {
        "description": "文章的内容形式 —— 它是一篇什么东西？",
        "tags": [
            "研究报告/调查报告",     # 有方法、样本量、数据图表的研究
            "深度分析/评论",         # 有观点、论证链的分析文章
            "新闻/短讯",             # 纯事实通报，无分析深度
            "产品发布/更新公告",     # 新产品/新功能/新版本
            "高管访谈/对话",         # CEO/CIO/VP 一对一采访或圆桌对话
            "技术论文/学术研究",     # 论文、实验、benchmark报告
            "播客/视频转录",         # 对话形式的转录内容
            "案例研究/实战复盘",     # 具体某公司/某项目的经验
            "个人观点/专栏",         # 作者个人意见，非系统性分析
            "趋势综述/盘点",         # 行业动态汇总，多个事件
            "指南/教程/最佳实践",    # How-to、实操指南
            "政策/监管文件",         # 法规、政策解读
            "周报/聚合",             # Newsletter、每周汇总、link salad
        ],
    },
    "topic_focus": {
        "description": "文章的核心主题 —— 它在讲什么？",
        "tags": [
            "AI Agent/智能体",          # Agent架构、多Agent编排、自主Agent
            "大语言模型(LLM)",            # 模型能力、训练、推理、prompt
            "AI治理/安全/对齐",           # 可信AI、偏见、幻觉、合规
            "企业AI落地/采纳",            # 部署、ROI、组织变革、选型
            "AI与生产力/效率",            # 人效、自动化、工作流
            "AI与组织/团队/人才",         # 团队结构、技能、岗位变化
            "AI人机交互/体验",            # AI UX设计、人机交互模式、使用体验
            "商业模式/竞争/战略",         # 创业、融资、市场格局
            "AI与社会/经济/政策",         # 就业、不平等、公共政策
            "开发工具/基础设施",          # MLOps、RAG、向量数据库、工程实践
            "垂直行业应用",               # 医疗、法律、金融、教育等
            "AI产品/工具评测",            # 具体产品的使用体验和对比
            "AI研究与反常识发现",         # 意想不到的实验结果、模型行为
            "数据/内容/知识管理",         # 数据治理、知识图谱、搜索
            "自动化与机器人",             # RPA、机器人、自动驾驶
            "AI与创意/媒体",              # 内容生成、设计、影视、音乐
            "安全/风险/对抗",             # AI安全漏洞、对抗攻击、数据泄露
        ],
    },
    "perspective": {
        "description": "文章的主要叙事视角 —— 从谁的角度在说？",
        "tags": [
            "企业决策者/高管视角",   # CEO/CIO如何思考
            "技术团队/工程师视角",   # 开发者、技术负责人的角度
            "员工/用户视角",         # 普通员工、终端用户的体验
            "监管/政策制定者视角",   # 监管机构、立法者
            "投资者/分析师视角",     # VC、投行、市场分析师
            "学术/研究者视角",       # 大学、研究院
            "咨询/顾问视角",         # 管理咨询、行业顾问
            "行业专家/从业者视角",   # 垂直行业的专家或一线从业者
            "供应商/产品方视角",     # AI公司、SaaS厂商
            "消费者/公众视角",       # 社会大众、消费者权益
        ],
    },
    "evidence_source": {
        "description": "论据的来源性质 —— 文章的核心论据来自哪？",
        "tags": [
            "一手数据/实验",         # 作者自己做的调查、实验、A/B测试
            "一手案例/采访",         # 作者亲自采访、深度跟踪的项目
            "公开数据集/指标",       # 引用公开benchmark、排行榜、行业指标
            "引用学术论文",          # 引用大学/研究院的学术论文（非行业报告）
            "引用行业报告",          # 引用Gartner/McKinsey等咨询或市场报告
            "逻辑推演/框架",         # 基于逻辑论证构建框架，无原始数据
            "个人经验/观察",         # 作者自己的行业观察、工作经验
            "法规/政策原文",         # 以法律条文、政策文件为论据基础
        ],
    },
    "evidence_depth": {
        "description": "论证的深度和质量 —— 文章论述到哪个层次？",
        "tags": [
            "有数据/有因果",         # 有量化数据 + 建立了因果关系或排除替代解释
            "有数据/仅描述",         # 有量化数据，但未建立因果，仅展示趋势/分布
            "有案例/有细节",         # 有真实案例，包含决策逻辑、执行细节、结果
            "有案例/仅概述",         # 有案例但缺细节，停留在"某公司做了某事"
            "纯观点/无支撑",         # 没有数据或案例，全是观点和论断
            "引用综述/合成",         # 基于二手资料的综述、整合、对比
        ],
    },
    "tone": {
        "description": "文章的基调 —— 它的表达姿态？",
        "tags": [
            "分析/洞察",             # 冷静分析、提供新视角
            "预警/风险提示",         # 指出问题、潜在风险
            "实用/指导",             # 务实、可操作的建议
            "批判/质疑",             # 挑战主流观点、质疑
            "展望/预测",             # 面向未来的判断
            "新闻式报道",            # 中立报道，传递信息而非立场
            "分析式报道",            # 在报道基础上提供自己的分析和判断
            "故事/叙事",             # 讲故事为主，有情节
            "辩论/争议",             # 提出有争议的观点
        ],
    },
    "entity_scope": {
        "description": "文章涉及的组织/实体范围 —— 覆盖谁？",
        "tags": [
            "科技巨头",              # Google/Meta/OpenAI/Apple/Amazon/Microsoft
            "大型企业(非科技)",      # 银行、制造、零售等传统大企业
            "创业公司/SME",          # 中小型、初创公司
            "学术机构",              # 大学、研究机构
            "政府/监管机构",         # 政府、议会、监管
            "非营利/民间组织",       # NGO、行业协会
            "个人/个体",             # 个人开发者、专家
            "跨行业(理论/宏观)",     # 不特定企业，讨论通用概念或理论
            "跨行业(实践/标杆)",     # 跨行业但有具体企业案例
        ],
    },
}

# ── LLM system prompt ──
TAG_SYSTEM_PROMPT = """你是一个专业的内容标签系统。对每一篇文章从7个维度打标，每个维度选择最匹配的一个标签。

## 维度定义

1. content_form —— 内容的形式：研究报告（有数据方法）、深度分析（有观点链）、新闻（纯事实）、产品公告、高管访谈/对话（一对一的CEO/CIO采访）、案例复盘、周报（多篇汇总）等。

2. topic_focus —— 核心主题：Agent、LLM、企业AI落地、治理安全、人机交互体验、反常识发现等。

3. perspective —— 叙事视角：从谁的角度写的？高管、工程师、员工、学术研究者、行业从业者、供应商？

4. evidence_source —— 论据来源：核心证据的来源性质。关键区分：一手数据/实验 vs 引用学术论文 vs 引用行业报告 vs 逻辑推演（无原始数据）。

5. evidence_depth —— 论证的深度和粒度：有数据+因果分析？有数据趋势描述？有案例+细节？还是纯观点/无支撑？

6. tone —— 基调：分析洞察、预警风险、实用指导、批判质疑、新闻式报道（被动传递信息）、分析式报道（报道+自己的判断）。

7. entity_scope —— 涉及范围：科技巨头、传统企业、创业公司、学术机构、政府、跨行业（理论/宏观：不涉及具体企业）、跨行业（实践/标杆：有具体案例）。

## 打分细则

**evidence_source 的关键区分：**
- "一手数据/实验"：作者亲自做的survey、A/B测试、用户研究
- "引用学术论文"：引用arXiv/大学研究/学术会议论文
- "引用行业报告"：引用Gartner/Forrester/McKinsey/Deloitte等
- "逻辑推演/框架"：没有引用任何外部数据，纯粹的逻辑论证

如果文章同时引用了多种来源，选最主要的那个。

**evidence_depth 的关键区分：**
- "有数据/有因果"：不仅展示数据，还解释了因果关系（"因为A所以B，数据排除C的可能性"）
- "有数据/仅描述"："X%的企业正在做Y"——有数字但不能证实归因
- "有案例/有细节"：具体公司名+问题描述+方案细节+效果数据+时间线
- "有案例/仅概述"："某公司通过AI实现了效率提升"——没细节
- "纯观点/无支撑"：整篇文章没有提到任何一个数据点或具体案例
- "引用综述/合成"：综合多位研究者/多份报告的观点，但不包含原始案例或数据

**tone 的关键区分：**
- "新闻式报道"：陈述事实——"OpenAI发布了o3，评测显示比o1提升20%"
- "分析式报道"：同样写一件事但给出判断——"OpenAI发布的o3暴露了一个信号：推理成本还在涨"

**entity_scope 的关键区分：**
- "跨行业(理论/宏观)"：讨论AI对"企业管理"的通用影响，不举具体公司例子
- "跨行业(实践/标杆)"：讨论AI如何在多行业落地，但给出了具体企业案例

## 输出格式

输出JSON数组，每个元素：
- url: 原文URL
- title: 原文标题
- tags: 7个维度标签的对象
- key_signal: 1-2句最令人意外/最有价值的发现（如果没有就空字符串）

标签必须严格从列表中选，不要自创。输出示例：
[
  {{
    "url": "https://example.com/article",
    "title": "文章的标题",
    "tags": {{
      "content_form": "深度分析/评论",
      "topic_focus": "企业AI落地/采纳",
      "perspective": "企业决策者/高管视角",
      "evidence_source": "引用行业报告",
      "evidence_depth": "有数据/有因果",
      "tone": "分析式报道",
      "entity_scope": "跨行业(实践/标杆)"
    }},
    "key_signal": "员工使用AI后效率提升40%，但管理者满意度下降15%——AI没有让团队更和谐"
  }}
]

只输出JSON数组，不要额外内容。
"""

if __name__ == "__main__":
    import json
    print("Tag Schema Dimensions (V2):")
    for dim, info in TAG_SCHEMA.items():
        desc = info.get("description", dim)
        note = ""
        if "deprecated" in desc:
            note = " [已弃用]"
            desc = desc.replace("[已弃用] ", "")
        print(f"\n{desc} ({dim}){note}:")
        for tag in info["tags"]:
            print(f"  • {tag}")
    active_dims = [k for k in TAG_SCHEMA if "deprecated" not in TAG_SCHEMA[k].get("description", "")]
    print(f"\nActive dimensions: {len(active_dims)}")
    print(f"Total tags: {sum(len(v['tags']) for v in TAG_SCHEMA.values())}")
    print()

    # Show V1 → V2 changes
    print("Changes from V1:")
    v1_evidence_types = ["定量数据/调研数据", "定性案例/深度访谈", "实验/基准测试", "推理/逻辑论证", "引用/综述", "个人经验/感悟", "政策/法规原文"]
    print(f'  • evidence_type → evidence_source + evidence_depth (old evidence_type tags: {v1_evidence_types})')
    print('  • content_form: + "高管访谈/对话"')
    print('  • tone: "报道/客观" → "新闻式报道" / "分析式报道"')
    print('  • entity_scope: "跨行业通用" → "跨行业(理论/宏观)" / "跨行业(实践/标杆)"')
    print('  • perspective: + "行业专家/从业者视角"')
    print('  • topic_focus: + "AI人机交互/体验"')
    print('  • evidence_source: "引用他人研究" → "引用学术论文" / "引用行业报告"')
