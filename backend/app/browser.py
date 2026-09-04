from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from .parsers import validate_public_url


MAX_BROWSER_IMAGES = 24
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def _browser_executable() -> Path | None:
    configured = str(os.getenv("JOBPOSTINGS_BROWSER_EXECUTABLE") or "").strip()
    if configured:
        candidate = Path(configured)
        if candidate.is_file():
            return candidate

    candidates = [
        Path(os.getenv("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.getenv("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path(os.getenv("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for name in ("chrome", "google-chrome", "chromium", "chromium-browser"):
        located = shutil.which(name)
        if located:
            candidates.append(Path(located))
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _scroll_for_lazy_images(page: Any) -> None:
    last_height = 0
    stable_rounds = 0
    for _ in range(30):
        page.evaluate("() => window.scrollTo(0, document.body ? document.body.scrollHeight : 0)")
        page.wait_for_timeout(500)
        height = int(page.evaluate("() => document.body ? document.body.scrollHeight : 0") or 0)
        if height == last_height and height > 0:
            stable_rounds += 1
        else:
            stable_rounds = 0
        last_height = max(last_height, height)
        if stable_rounds >= 3:
            break
    page.evaluate("() => window.scrollTo(0, document.body ? document.body.scrollHeight : 0)")
    page.wait_for_timeout(1500)


def _route_public_request(route: Any) -> None:
    request = route.request
    if request.url.startswith(("http://", "https://")):
        try:
            validate_public_url(request.url)
        except ValueError:
            route.abort()
            return
    route.continue_()


def _image_snapshot(page: Any) -> list[dict[str, Any]]:
    selector = "#js_content img"
    if page.locator(selector).count() == 0:
        selector = "img"
    return page.locator(selector).evaluate_all(
        """imgs => imgs.map((img, index) => ({
            index,
            source: img.dataset.src || img.getAttribute('data-original') || img.getAttribute('data-lazy-src') || img.currentSrc || img.src || '',
            loaded: img.complete && img.naturalWidth > 0,
            width: img.naturalWidth || 0,
            height: img.naturalHeight || 0
        }))"""
    )


def _image_url(value: str, page_url: str) -> str | None:
    candidate = urljoin(page_url, str(value or "").strip())
    if not candidate.startswith(("http://", "https://")):
        return None
    try:
        validate_public_url(candidate)
    except ValueError:
        return None
    return candidate


def fetch_public_browser(url: str) -> dict[str, Any]:
    """Render a public page in a temporary browser context for JS/lazy images."""
    validate_public_url(url)
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is not installed; install the web browser dependency") from exc

    with sync_playwright() as playwright:
        launch_options: dict[str, Any] = {"headless": True}
        executable = _browser_executable()
        if executable:
            launch_options["executable_path"] = str(executable)
        browser = playwright.chromium.launch(**launch_options)
        try:
            context = browser.new_context(
                locale="zh-CN",
                service_workers="block",
                extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
            )
            page = context.new_page()
            page.route("**/*", _route_public_request)
            response = None
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            except PlaywrightTimeoutError:
                if page.url in {"", "about:blank"}:
                    raise
            page.wait_for_timeout(3_000)
            _scroll_for_lazy_images(page)

            body_text = page.locator("body").inner_text(timeout=10_000).strip()
            article = page.locator("#js_content")
            article_text = article.first.inner_text(timeout=10_000).strip() if article.count() else ""
            text = article_text if len(article_text) >= 20 else (body_text if len(body_text) >= 200 else "")
            page_url = page.url or url
            image_snapshot = _image_snapshot(page)
            image_urls: list[str] = []
            for item in image_snapshot:
                if not item.get("loaded"):
                    continue
                image_url = _image_url(str(item.get("source") or ""), page_url)
                if image_url and image_url not in image_urls:
                    image_urls.append(image_url)

            image_data: list[dict[str, Any]] = []
            for image_url in image_urls[:MAX_BROWSER_IMAGES]:
                try:
                    image_response = context.request.get(
                        image_url,
                        timeout=30_000,
                        fail_on_status_code=False,
                    )
                    final_image_url = str(image_response.url)
                    validate_public_url(final_image_url)
                    if not 200 <= image_response.status < 300:
                        continue
                    data = image_response.body()
                    if not data or len(data) > MAX_IMAGE_BYTES:
                        continue
                    image_data.append({
                        "url": image_url,
                        "data": data,
                        "content_type": image_response.headers.get("content-type", ""),
                    })
                except Exception:
                    continue

            link_selector = "#js_content a" if article.count() else "body a"
            links = page.locator(link_selector).evaluate_all(
                "anchors => anchors.map(anchor => anchor.href).filter(Boolean)"
            )
            challenge_markers = [
                marker
                for marker in ("当前环境异常", "完成验证后即可继续", "环境验证", "验证码", "访问过于频繁")
                if marker in body_text or marker in page_url
            ]
            screenshot_data = page.screenshot(full_page=True, type="png")
            content_type = response.headers.get("content-type", "text/html") if response else "text/html"
            return {
                "url": page_url,
                "title": page.title()[:300],
                "text": text[:2_000_000],
                "content_type": content_type,
                "links": list(dict.fromkeys(urljoin(page_url, str(link)) for link in links))[:100],
                "images": image_urls[:MAX_BROWSER_IMAGES],
                "image_data": image_data,
                "screenshot_data": screenshot_data,
                "browser_rendered": True,
                "browser_image_count": len(image_snapshot),
                "browser_loaded_image_count": sum(bool(item.get("loaded")) for item in image_snapshot),
                "browser_downloaded_image_count": len(image_data),
                "browser_article_text_chars": len(article_text),
                "access_challenge": bool(challenge_markers),
                "access_error": "浏览器页面要求环境验证" if challenge_markers else "",
            }
        finally:
            browser.close()
