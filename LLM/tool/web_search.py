# -*- coding: utf-8 -*-
r"""
web_search 工具：通用联网搜索。
有 BOCHA_API_KEY / SERPAPI_KEY 时走官方 API；否则走 DuckDuckGo Instant Answer + 本地新闻语料关键词检索兜底。
"""
import json
import re
import time
import urllib.parse
import urllib.request

from ..tools import tool
from ._rss import _fetch, _rss_items, UA


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
