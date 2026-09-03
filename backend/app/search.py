from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus

import httpx

from .db import connect, one, utc_now
from .model_provider import summarize_company
from .parsers import extract_html


def _safe_url(url: str) -> bool:
    from urllib.parse import urlparse
    import ipaddress
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if host in {"localhost", "metadata.google.internal"}:
        return False
    try:
        address = ipaddress.ip_address(host)
        return not (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved)
    except ValueError:
        return True


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
        ("baidu", f"https://www.baidu.com/s?wd={quote_plus(name + ' 官网 招聘') }"),
    ]
    for provider, url in queries:
        try:
            response = httpx.get(url, headers={"User-Agent": "Mozilla/5.0 JobPostings/0.1"}, timeout=20, follow_redirects=True)
            response.raise_for_status()
            results = []
            for title, link in _extract_result_links(response.text):
                if _safe_url(link):
                    results.append({"provider": provider, "title": title, "url": link})
            if results:
                return results
        except Exception:
            continue
    return []


def fetch_search_sources(results: list[dict[str, Any]], max_pages: int = 5) -> list[dict[str, Any]]:
    sources = []
    for result in results[:max_pages]:
        try:
            response = httpx.get(result["url"], headers={"User-Agent": "Mozilla/5.0 JobPostings/0.1"}, timeout=30, follow_redirects=True)
            response.raise_for_status()
            if len(response.content) > 10 * 1024 * 1024:
                continue
            parsed = extract_html(response.text)
            if parsed["text"]:
                sources.append({**result, "final_url": str(response.url), "text": parsed["text"][:50_000]})
        except Exception:
            continue
    return sources


def enrich_company(company_id: str) -> dict[str, Any]:
    """Best-effort public-web enrichment; never raises into recruitment ingestion."""
    company = one("SELECT * FROM companies WHERE id=?", (company_id,))
    if not company:
        return {"company_id": company_id, "status": "missing"}
    recent = one(
        "SELECT retrieved_at FROM company_claims WHERE company_id=? ORDER BY retrieved_at DESC LIMIT 1",
        (company_id,),
    )
    if recent:
        from datetime import datetime, timedelta, timezone

        try:
            if datetime.fromisoformat(recent["retrieved_at"]) > datetime.now(timezone.utc) - timedelta(days=30):
                return {"company_id": company_id, "status": "cached"}
        except ValueError:
            pass
    results = search_company(company["display_name"])
    sources = fetch_search_sources(results)
    if not sources:
        with connect() as connection:
            connection.execute("UPDATE companies SET verification_status='search_failed',updated_at=? WHERE id=?", (utc_now(), company_id))
        return {"company_id": company_id, "status": "search_failed"}
    summary = ""
    facts: list[dict[str, Any]] = []
    try:
        model_result = summarize_company(company["display_name"], sources)
        summary = str(model_result.payload.get("summary") or "").strip()
        facts = model_result.payload.get("facts") or []
    except Exception:
        summary = sources[0]["text"][:600].strip()
    now = utc_now()
    with connect() as connection:
        connection.execute("UPDATE companies SET summary=?,verification_status='public_web',updated_at=? WHERE id=?", (summary[:3000], now, company_id))
        connection.execute("UPDATE company_claims SET is_current=0 WHERE company_id=? AND field_name='summary'", (company_id,))
        connection.execute("INSERT INTO company_claims(id,company_id,field_name,field_value,source_url,source_type,retrieved_at,confidence,is_current) VALUES(?,?,?,?,?,?,?,?,1)", (__import__('uuid').uuid4().hex, company_id, "summary", summary[:3000], sources[0].get("final_url") or sources[0].get("url"), "public_web", now, 0.75 if summary else 0.35))
        for fact in facts:
            if isinstance(fact, dict) and fact.get("fact"):
                connection.execute("INSERT INTO company_claims(id,company_id,field_name,field_value,source_url,source_type,retrieved_at,confidence,is_current) VALUES(?,?,?,?,?,?,?,?,0)", (__import__('uuid').uuid4().hex, company_id, "fact", str(fact["fact"])[:1000], fact.get("source_url"), "public_web", now, 0.65))
        for source in sources:
            connection.execute("INSERT INTO evidences(id,company_id,source_url,source_type,excerpt,observed_at) VALUES(?,?,?,?,?,?)", (__import__('uuid').uuid4().hex, company_id, source.get("final_url") or source.get("url"), "public_web", source.get("text", "")[:4000], now))
    return {"company_id": company_id, "status": "enriched", "sources": len(sources)}
