# -*- coding: utf-8 -*-
r"""
联网工具集（模块 9）：
  - web_search(query)  通用搜索：有 BOCHA_API_KEY / SERPAPI_KEY 时走官方 API；
                        否则走 DuckDuckGo Instant Answer + 本地新闻语料关键词检索兜底。
  - get_news(category) 结构化新闻：抓取 RSS（人民日报/央视/新华社/BBC中文等，按分类）。
安全约束：结果只做摘要；健康类信息在返回里注明"仅供参考"；过滤惊悚关键词。

工具以 OpenAI Function Calling 格式暴露给大模型，由 chat 引擎调用。

【新增工具三步走】schema 与实现写在一起，装饰器自动注册：
    @tool("工具名", "何时用/怎么用的描述（模型靠它决定调用）", {参数 JSON Schema}, enabled=True)
    def 工具名(参数: str = "默认值") -> dict:
        ...
        return {"ok": True, "result": "..."}   # 或 {"ok": False, "message": "..."}
注册后 run_tool 自动分发、TOOLS 自动收录、per-tool 开关 <工具名>_enabled 自动生效，
前端工具页自动显示开关，无需再改 chat.py / server.py / db.py / conf.py。
"""
import inspect
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

from .conf import FEEDS_FILE

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RobotCompanion/1.0"}

# 惊悚/暴力等不适合老人的内容关键词（命中即过滤）
BANNED = ["死亡", "尸体", "凶杀", "杀人", "爆炸伤亡", "强奸", "惨案", "遗体"]

# ---------------------------------------------------------------- 注册表
# name -> {"schema": OpenAI function-calling 声明, "fn": 实现函数, "enabled": 默认开关}
_TOOL_REGISTRY: dict[str, dict] = {}


def tool(name: str, description: str, parameters: dict, enabled: bool = True):
    """注册一个工具：OpenAI function-calling schema 与实现写在一起，run_tool 自动分发。"""
    def deco(fn):
        _TOOL_REGISTRY[name] = {
            "schema": {
                "type": "function",
                "function": {"name": name, "description": description, "parameters": parameters},
            },
            "fn": fn,
            "enabled": enabled,
        }
        return fn
    return deco


def _run_fn(fn, args: dict):
    """调用工具实现：按函数签名过滤模型传来的参数，缺省交给函数默认值兜底。"""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):   # 拿不到签名（如 C 函数）→ 直接展开
        return fn(**args)
    allowed = {p for p in sig.parameters if p not in ("self", "cls")}
    kwargs = {k: v for k, v in args.items() if k in allowed}
    return fn(**kwargs)


# ---------------------------------------------------------------- RSS 源
def _load_feeds() -> dict:
    """feeds.json: {"国内": ["url1", ...], "国际": [...], ...}"""
    try:
        with open(FEEDS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _fetch(url: str, timeout: int = 10) -> str | None:
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return None


def _parse_rss(xml_text: str, limit: int = 8) -> list[dict]:
    items = []
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return items
    for item in root.iter("item"):
        def g(tag):
            el = item.find(tag)
            return el.text.strip() if el is not None and el.text else ""
        title = g("title")
        desc = re.sub(r"<[^>]+>", "", g("description"))[:200]
        link = g("link")
        pub = g("pubDate") or g("dc:date")
        if title and not any(b in title or b in desc for b in BANNED):
            items.append({"title": title, "summary": desc, "link": link, "pub": pub})
        if len(items) >= limit:
            break
    return items


def _rss_items(categories=None, limit: int = 8) -> list[dict]:
    """并发抓取各源 RSS（不阻塞，控制总延迟），去重排序后返回。"""
    from concurrent.futures import ThreadPoolExecutor
    feeds = _load_feeds()
    if categories:
        urls = []
        for c in categories:
            urls += feeds.get(c, [])
    else:
        urls = [u for v in feeds.values() for u in v]

    def grab(url):
        text = _fetch(url)
        return _parse_rss(text) if text else []

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(grab, urls[:8]))

    all_items = [it for batch in results for it in batch]
    seen, uniq = set(), []
    for it in all_items:
        if it["title"] in seen:
            continue
        seen.add(it["title"])
        uniq.append(it)
    uniq.sort(key=lambda x: x["pub"] or "", reverse=True)
    return uniq[:limit]


# ---------------------------------------------------------------- 搜索
def _search_bocha(query: str) -> list[dict] | None:
    import os
    key = os.environ.get("BOCHA_API_KEY")
    if not key:
        return None
    try:
        req = urllib.request.Request(
            "https://api.bochaai.com/v1/web-search",
            data=json.dumps({"query": query, "count": 6, "freshness": "noLimit"}).encode(),
            headers={**UA, "Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        pages = (data.get("data") or {}).get("webPages", {}).get("value", [])
        return [{"title": p.get("name", ""), "summary": p.get("snippet", ""), "link": p.get("url", "")}
                for p in pages]
    except Exception:
        return None


def _search_serpapi(query: str) -> list[dict] | None:
    import os
    key = os.environ.get("SERPAPI_KEY")
    if not key:
        return None
    url = "https://serpapi.com/search.json?" + urllib.parse.urlencode(
        {"q": query, "engine": "google", "num": 6, "api_key": key, "hl": "zh-cn"})
    text = _fetch(url)
    if not text:
        return None
    try:
        data = json.loads(text)
        return [{"title": r.get("title", ""), "summary": r.get("snippet", ""), "link": r.get("link", "")}
                for r in data.get("organic_results", [])]
    except Exception:
        return None


def _search_ddg(query: str) -> list[dict]:
    """DuckDuckGo Instant Answer（免费无 key，中文命中率有限；超时短，不拖慢主流程）。"""
    url = "https://api.duckduckgo.com/?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "no_html": 1, "skip_disambig": 1})
    text = _fetch(url, timeout=4)
    out = []
    if not text:
        return out
    try:
        data = json.loads(text)
        if data.get("AbstractText"):
            out.append({"title": "DuckDuckGo: " + (data.get("Heading") or query),
                        "summary": data["AbstractText"][:300], "link": data.get("AbstractURL", "")})
        for topic in data.get("RelatedTopics", [])[:5]:
            if isinstance(topic, dict) and topic.get("Text"):
                out.append({"title": topic.get("FirstURL", "").rsplit("/", 1)[-1],
                            "summary": topic["Text"][:200], "link": topic.get("FirstURL", "")})
    except Exception:
        pass
    return out


def _search_corpus(query: str, limit: int = 6) -> list[dict]:
    """本地新闻语料关键词兜底：从 RSS 源里捞最近相关条目。"""
    items = _rss_items(limit=60)
    kws = [w for w in re.split(r"[\s,，。？！?！、]+", query) if len(w) >= 2]
    scored = []
    for it in items:
        text = it["title"] + it["summary"]
        hit = sum(1 for k in kws if k in text)
        if hit:
            scored.append((hit, it))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [it for _, it in scored[:limit]]


@tool(
    "web_search",
    "联网搜索最新信息（新闻、天气、常识、时事等）。老人问不知道的新鲜事时使用。",
    {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "要搜索的关键词或问题"}},
        "required": ["query"],
    },
)
def web_search(query: str) -> dict:
    query = (query or "").strip()
    if not query:
        return {"ok": False, "message": "搜索关键词为空"}
    started = time.time()
    # 官方 API（若配置）> DuckDuckGo 与本地新闻语料并行兜底
    results = _search_bocha(query) or _search_serpapi(query) or []
    source = "bocha/serpapi"
    if not results:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as ex:
            ddg_fut = ex.submit(_search_ddg, query)
            corpus_fut = ex.submit(_search_corpus, query)
            ddg = ddg_fut.result()
            corpus = corpus_fut.result()
        seen = {r["title"] for r in results}
        for r in ddg + corpus:
            if r["title"] not in seen:
                results.append(r)
                seen.add(r["title"])
        source = "news-rss+duckduckgo"
    if not results:
        # 完全没匹配 → 给最近新闻，注明是"相关推荐"
        items = _rss_items(limit=5)
        for it in items:
            results.append({"title": it["title"], "summary": it["summary"], "link": it["link"]})
        source = "recent-news(未精确命中)"
    if not results:
        return {"ok": False, "message": "联网搜索暂时没有结果，请稍后再试"}
    lines = [f"来源: {source}，耗时 {int((time.time() - started) * 1000)}ms"]
    for r in results[:6]:
        lines.append(f"- {r['title']}\n  {r['summary'][:150]}\n  {r['link']}")
    return {"ok": True, "result": "\n".join(lines)}


# ---------------------------------------------------------------- 新闻
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


# ---------------------------------------------------------------- 注册表导出
# 注意：必须在全部 @tool 注册之后生成（模块 import 时按顺序执行）
TOOLS = [reg["schema"] for reg in _TOOL_REGISTRY.values()]
TOOL_ENABLED_KEYS = [f"{n}_enabled" for n in _TOOL_REGISTRY]
TOOL_DEFAULTS = {f"{n}_enabled": reg["enabled"] for n, reg in _TOOL_REGISTRY.items()}


def effective_tools(settings: dict) -> list[dict]:
    """按 per-tool 开关（`<工具名>_enabled`）过滤，返回要传给模型的 schema 列表。"""
    return [reg["schema"] for name, reg in _TOOL_REGISTRY.items()
            if settings.get(f"{name}_enabled", reg["enabled"])]


def tools_with_state(settings: dict) -> list[dict]:
    """给前端用：schema + enabled（当前开关状态）+ switch_key（设置项 key）。"""
    out = []
    for name, reg in _TOOL_REGISTRY.items():
        item = dict(reg["schema"])
        item["enabled"] = bool(settings.get(f"{name}_enabled", reg["enabled"]))
        item["switch_key"] = f"{name}_enabled"
        out.append(item)
    return out


# ---------------------------------------------------------------- 调度入口
def run_tool(name: str, args: dict) -> dict:
    reg = _TOOL_REGISTRY.get(name)
    if not reg:
        return {"ok": False, "message": f"未知工具 {name}"}
    try:
        return _run_fn(reg["fn"], args or {})
    except Exception as e:
        return {"ok": False, "message": f"工具执行失败: {e}"}
