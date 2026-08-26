# -*- coding: utf-8 -*-
r"""
get_news 工具：结构化新闻摘要（国内/国际/健康/科技/全部）。
抓取 RSS（人民日报/央视/新华社/BBC中文等，按分类）；健康类返回里注明"仅供参考"。
"""
from ..tools import tool
from ._rss import _rss_items

NEWS_CATEGORY = {
    "国内": ["国内"],
    "国际": ["国际"],
    "健康": ["健康"],
    "财经": ["财经"],
    "科技": ["科技"],
    "全部": None,
}

# 健康类关键词（无专门健康源时从其他分类里过滤）
HEALTH_KEYWORDS = ["健康", "医院", "医生", "药", "疫苗", "睡眠", "血压", "血糖", "饮食",
                   "运动", "养生", "疾病", "治疗", "体检", "老年", "阿尔茨海默", "痴呆", "感冒"]


@tool(
    "get_news",
    "获取结构化新闻摘要（国内/国际/健康/科技/全部）。老人问'今天有什么新闻'时使用。",
    {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": ["国内", "国际", "健康", "财经", "科技", "全部"],
                "description": "新闻分类",
            },
        },
        "required": ["category"],
    },
)
def get_news(category: str = "全部") -> dict:
    category = category or "全部"
    cats = NEWS_CATEGORY.get(category, None)
    items = _rss_items(categories=cats, limit=8) if cats is not None else _rss_items(limit=8)
    if not items and category == "健康":
        # 健康源无内容 → 从全部源里按健康关键词过滤
        items = [it for it in _rss_items(limit=60)
                 if any(k in (it["title"] + it["summary"]) for k in HEALTH_KEYWORDS)][:8]
    if not items:
        return {"ok": False, "message": "暂时没有抓到新闻，请稍后再试"}
    lines = [f"【{category}新闻摘要】"]
    for it in items:
        lines.append(f"- {it['title']}（{it['pub'][:16] if it['pub'] else ''}）\n  {it['summary'][:120]}")
    if category == "健康":
        lines.append("（健康类信息仅供参考，具体请咨询医生）")
    return {"ok": True, "result": "\n".join(lines)}
