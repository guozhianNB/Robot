# -*- coding: utf-8 -*-
r"""
RSS 新闻抓取共享辅助（LLM/tool/ 下的非工具模块，供 web_search 兜底与 get_news 共用）。
以下划线开头的模块不会被 tools.py 当作工具自动加载，仅作为共享代码被显式 import。
"""
import json
import re
import urllib.request
import xml.etree.ElementTree as ET

from ..conf import FEEDS_FILE

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RobotCompanion/1.0"}

# 惊悚/暴力等不适合老人的内容关键词（命中即过滤）
BANNED = ["死亡", "尸体", "凶杀", "杀人", "爆炸伤亡", "强奸", "惨案", "遗体"]


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
