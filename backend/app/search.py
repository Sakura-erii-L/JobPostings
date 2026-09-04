from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus

import httpx

from .db import connect, one, utc_now
from .model_provider import get_setting
from .parsers import extract_html, fetch_public_http, validate_public_url


def _safe_url(url: str) -> bool:
    try:
        validate_public_url(url)
        return True
    except (OSError, ValueError):
        return False


def _extract_result_links(html: str) -> list[tuple[str, str]]:
    links = re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S)
    result = []
    for url, title_html in links:
        title = re.sub(r"<[^>]+>", " ", title_html)
        title = re.sub(r"\s+", " ", title).strip()
        if url.startswith("http") and title and "bing.com" not in url and "baidu.com" not in url:
            result.append((title[:300], url))
    return list(dict.fromkeys(result))[:10]


def search_company(name: str) -> list[dict[str, Any]]:
    queries = [
        ("bing", f"https://www.bing.com/search?q={quote_plus(name + ' 官网 招聘') }"),
        ("bing", f"https://www.bing.com/search?q={quote_plus(name + ' 处罚 诉讼 事故 失信 欠薪 裁员') }"),
        ("baidu", f"https://www.baidu.com/s?wd={quote_plus(name + ' 官网 招聘') }"),
        ("baidu", f"https://www.baidu.com/s?wd={quote_plus(name + ' 处罚 诉讼 事故 失信 欠薪 裁员') }"),
    ]
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for provider, url in queries:
        try:
            response = httpx.get(url, headers={"User-Agent": "Mozilla/5.0 JobPostings/0.1"}, timeout=20, follow_redirects=True)
            response.raise_for_status()
            for title, link in _extract_result_links(response.text):
                if link not in seen and _safe_url(link):
                    seen.add(link)
                    result.append({"provider": provider, "title": title, "url": link})
        except Exception:
            continue
    return result[:20]


def fetch_search_sources(results: list[dict[str, Any]], max_pages: int = 5) -> list[dict[str, Any]]:
    sources = []
    for result in results[:max_pages]:
        try:
            response = fetch_public_http(result["url"], timeout=30)
            parsed = extract_html(response.text)
            if parsed["text"]:
                sources.append({**result, "final_url": str(response.url), "text": parsed["text"][:50_000]})
        except Exception:
            continue
    return sources


def enrich_company(company_id: str) -> dict[str, Any]:
    """Run the durable public research flow from legacy callers."""
    from .company_research import execute_company_research

    return execute_company_research(company_id, f"enrichment-{company_id}")
