"""隔离前端冒烟检查：仅拦截本地 Vite API，不访问真实后端。"""
import json
import sys
import traceback
from pathlib import Path

from playwright.sync_api import sync_playwright

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
ROOT = Path(__file__).resolve().parent
ARTIFACTS = ROOT / "artifacts"


def mock_response(path, role):
    if path == "/auth/me":
        return {"user": {"email": f"{role}@example.com", "role": role}}
    if path == "/bootstrap/status":
        return {"initialized": True}
    if path in {"/companies", "/jobs", "/me/applications", "/recruitment-events", "/notifications"}:
        return []
    if path == "/admin/settings":
        return {"sync_interval_minutes": 10, "import_days": 30, "redaction_enabled": False, "llm_provider": {"enabled": False}, "backup": {"enabled": False}, "smtp": {"enabled": False}, "agent_api_enabled": False}
    if path == "/admin/connectors":
        return [{"kind": "tracememo", "base_url": "http://127.0.0.1:6131/api/v1", "enabled": False}]
    if path == "/admin/local-storage":
        return {"database": {"path": "mock", "size": 0}, "backups": [], "tracememo_cache": {"groups": 0, "messages": 0, "bytes": 0}, "chat_records": {"messages": 0, "artifacts": 0, "artifact_bytes": 0}}
    if path == "/admin/invitations":
        return []
    return {}


def check(role, width):
    print(f"START role={role} width={width}", flush=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=CHROME)
        page = browser.new_page(viewport={"width": width, "height": 800})
        page.set_default_timeout(5000)
        writes = []

        def route(route):
            request = route.request
            if "/api/v1/" not in request.url:
                route.continue_()
                return
            path = request.url.split("/api/v1", 1)[1].split("?", 1)[0]
            if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                writes.append((request.method, path))
            route.fulfill(status=200, content_type="application/json", body=json.dumps(mock_response(path, role)))

        page.route("**/*", route)
        try:
            print("LOAD", flush=True)
            page.goto("http://127.0.0.1:5173/", wait_until="domcontentloaded", timeout=10000)
            page.wait_for_timeout(600)
            sidebar = page.locator("#main-navigation")
            if width <= 680:
                print("MOBILE_MENU", flush=True)
                page.locator(".mobile-menu-button").click()
                assert "open" in (sidebar.get_attribute("class") or "")
                page.locator(".mobile-nav-close").click()
                page.wait_for_timeout(100)
                assert "open" not in (sidebar.get_attribute("class") or "")
                assert page.evaluate("document.activeElement.className") == "mobile-menu-button"
                page.locator(".mobile-menu-button").click()
            else:
                print("DESKTOP_SIDEBAR", flush=True)
            buttons = page.locator("#main-navigation .nav-button")
            assert buttons.count() == (4 if role == "member" else 7)
            buttons.last.click()
            page.wait_for_timeout(500)
            print("SETTINGS", flush=True)
            if role == "admin":
                assert page.get_by_role("heading", name="设置与管理").count() == 1
                assert page.locator(".settings-subnav button").count() == 5
                if width <= 680:
                    page.locator(".settings-mobile-select").select_option("connections")
                else:
                    page.locator(".settings-subnav button").nth(2).click()
                page.wait_for_timeout(300)
                assert page.get_by_role("heading", name="连接服务").count() == 1
                assert not any(method == "PUT" for method, _ in writes)
                page.locator(".service-row button").first.click()
                page.locator(".service-row button").nth(1).click()
                assert not any(method == "PUT" for method, _ in writes)
            else:
                assert page.get_by_role("heading", name="账户与安全").count() == 1
                password_fields = page.locator(".security-card input[type=password]")
                password_fields.nth(0).fill("short")
                password_fields.nth(1).fill("different")
                if width <= 680:
                    page.locator(".mobile-menu-button").click()
                page.locator("#main-navigation .nav-button").first.click()
                page.get_by_role("button", name="保存并离开").click()
                page.wait_for_timeout(250)
                assert "未保存修改" in page.locator(".app-navigation-dialog").inner_text()
                assert not any(path == "/auth/password" for _, path in writes)
            assert not any(method == "PUT" for method, _ in writes)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(ARTIFACTS / f"{role}-{width}.png"), full_page=True)
            print(f"PASS role={role} width={width} writes={writes}", flush=True)
        except Exception:
            ARTIFACTS.mkdir(parents=True, exist_ok=True)
            screenshot = ARTIFACTS / f"FAILED-{role}-{width}.png"
            page.screenshot(path=str(screenshot), full_page=True)
            print(f"FAIL role={role} width={width} screenshot={screenshot}", flush=True)
            traceback.print_exc()
            raise
        finally:
            browser.close()


def settings_outcome(fail_password=False):
    role = "member"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=CHROME)
        page = browser.new_page(viewport={"width": 680, "height": 800})
        page.set_default_timeout(5000)
        writes = []

        def route(route):
            request = route.request
            if "/api/v1/" not in request.url:
                route.continue_()
                return
            path = request.url.split("/api/v1", 1)[1].split("?", 1)[0]
            if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                writes.append((request.method, path))
            if path == "/auth/password" and fail_password:
                route.fulfill(status=400, content_type="application/json", body=json.dumps({"detail": "mock password failure"}))
                return
            route.fulfill(status=200, content_type="application/json", body=json.dumps(mock_response(path, role)))

        page.route("**/*", route)
        try:
            page.goto("http://127.0.0.1:5173/", wait_until="domcontentloaded", timeout=10000)
            page.wait_for_timeout(500)
            page.locator(".mobile-menu-button").click()
            page.locator("#main-navigation .nav-button").last.click()
            page.wait_for_timeout(300)
            fields = page.locator(".security-card input[type=password]")
            fields.nth(0).fill("valid-password")
            fields.nth(1).fill("valid-password")
            page.locator(".mobile-menu-button").click()
            page.locator("#main-navigation .nav-button").first.click()
            page.get_by_role("button", name="保存并离开").click()
            page.wait_for_timeout(500)
            if fail_password:
                assert page.get_by_role("heading", name="账户与安全").count() == 1
                assert page.locator(".security-card input[type=password]").nth(0).input_value() == "valid-password"
            else:
                assert page.get_by_role("heading", name="企业与岗位").count() == 1
                assert ("POST", "/auth/password") in writes
            print(f"PASS password={'400' if fail_password else 'success'} writes={writes}", flush=True)
        finally:
            browser.close()


def settings_stay_abandon():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=CHROME)
        page = browser.new_page(viewport={"width": 680, "height": 800})
        page.set_default_timeout(5000)
        def route(route):
            request = route.request
            if "/api/v1/" not in request.url:
                route.continue_()
                return
            path = request.url.split("/api/v1", 1)[1].split("?", 1)[0]
            data = mock_response(path, "member")
            route.fulfill(status=200, content_type="application/json", body=json.dumps(data))
        page.route("**/*", route)
        try:
            page.goto("http://127.0.0.1:5173/", wait_until="domcontentloaded", timeout=10000)
            page.wait_for_timeout(500)
            page.locator(".mobile-menu-button").click()
            page.locator("#main-navigation .nav-button").last.click()
            page.wait_for_timeout(300)
            page.locator(".security-card input[type=password]").nth(0).fill("valid-password")
            page.locator(".security-card input[type=password]").nth(1).fill("valid-password")
            page.locator(".mobile-menu-button").click()
            page.locator("#main-navigation .nav-button").first.click()
            page.get_by_role("button", name="留在当前页").click()
            assert page.get_by_role("heading", name="账户与安全").count() == 1
            page.locator(".mobile-menu-button").click()
            page.locator("#main-navigation .nav-button").first.click()
            print("PASS stay", flush=True)
            page.get_by_role("button", name="放弃修改").click()
            assert page.get_by_role("heading", name="企业与岗位").count() == 1
            print("PASS abandon", flush=True)
        finally:
            browser.close()


def business_mock_response(path, role):
    company = {
        "id": "company-1", "display_name": "示例科技", "legal_name": "示例科技有限公司",
        "summary": "招聘资料摘要", "primary_industry": "internet_software", "job_count": 1,
        "updated_at": "2026-01-01T00:00:00Z", "headquarters": "上海", "company_size": "100-499人",
        "verification_status": "已核对", "tags": [{"category": "industry", "code": "internet_software", "label": "互联网/软件"}],
    }
    job = {
        "id": "job-1", "canonical_title": "数据工程师", "recruitment_type": "社会招聘",
        "employment_type": "全职", "status": "active", "locations": ["上海"],
        "company_name": "示例科技", "updated_at": "2026-01-01T00:00:00Z", "education": ["本科"],
        "majors": ["计算机"], "requirements": "熟悉数据处理", "benefits": ["五险一金"],
    }
    events = [
        {"id": "event-1", "company_id": "company-1", "company_name": "示例科技", "title": "春招宣讲会", "event_type": "宣讲会", "start_at": "2026-01-15T10:00:00+08:00", "timezone": "Asia/Shanghai", "format": "线上", "city": "上海", "status": "upcoming", "notes": "线上宣讲说明", "job_ids": ["job-1"], "evidence_ids": ["evidence-1"]},
        {"id": "event-2", "company_id": "company-1", "company_name": "示例科技", "title": "网申截止", "event_type": "截止日期", "start_at": "2026-01-20T23:59:00+08:00", "timezone": "Asia/Shanghai", "format": "线上", "city": "上海", "status": "upcoming", "notes": "请在截止前提交", "job_ids": ["job-1"], "evidence_ids": ["evidence-1"]},
    ]
    detail = {
        **company, "aliases": [], "jobs": [job], "events": events,
        "evidences": [{"id": "evidence-1", "source_url": "https://example.com/source", "source_type": "public_web", "excerpt": "示例科技招聘信息", "observed_at": "2026-01-01T00:00:00Z", "raw_text": "来源原文"}],
        "recruitment_shared_details": [{"id": "shared-1", "batch_name": "2026 春招", "batch_year": 2026, "batch_season": "春季", "locations": ["上海"], "salary": {}, "education_requirements": ["本科"]}],
        "public_findings": [], "major_requirements": ["计算机"],
    }
    if path == "/auth/me":
        return {"user": {"email": f"{role}@example.com", "role": role}}
    if path == "/bootstrap/status":
        return {"initialized": True}
    if path == "/companies":
        return [company]
    if path == "/jobs":
        return [job]
    if path == "/me/applications":
        return [{**job, "state": "interested", "favorite": 1}]
    if path == "/recruitment-events":
        return events
    if path == "/companies/company-1":
        return detail
    if path == "/notifications":
        return []
    if path == "/admin/settings":
        return {"sync_interval_minutes": 10, "import_days": 30, "redaction_enabled": False, "llm_provider": {"enabled": False}, "backup": {"enabled": False}, "smtp": {"enabled": False}, "agent_api_enabled": False}
    if path == "/admin/connectors":
        return [{"kind": "tracememo", "base_url": "http://127.0.0.1:6131/api/v1", "enabled": False}]
    if path == "/admin/local-storage":
        return {"database": {"path": "mock", "size": 0}, "backups": [], "tracememo_cache": {"groups": 1, "messages": 1, "bytes": 10}, "chat_records": {"messages": 1, "artifacts": 0, "artifact_bytes": 0}}
    if path == "/admin/invitations" or path == "/admin/review-items":
        return []
    if path == "/admin/connectors/tracememo/groups":
        return [{"id": "group-1", "external_id": "wx-group-1", "name": "示例招聘群", "selected": False}]
    if path.startswith("/admin/tracememo/messages"):
        return {"days": 30, "groups": 1, "total": 1, "items": [{"id": "message-1", "external_message_id": "wx-message-1", "source_group_id": "group-1", "group_name": "示例招聘群", "sent_at": "2026-01-02T09:00:00Z", "sender": "示例发布者", "message_type": "text", "text_preview": "示例科技招聘数据工程师", "imported": False}]}
    if path == "/admin/tracememo/messages/import":
        return {"requested": 1, "created": 1, "updated": 0, "duplicates": 0, "recognized_skipped": 0}
    if path == "/admin/source-groups":
        return {"ok": True}
    if path == "/admin/processing-queue":
        item = {"id": "queue-1", "kind": "classify", "raw_message_id": "raw-1", "status": "failed", "stage": "classifying", "attempts": 1, "created_at": "2026-01-02T09:01:00Z", "updated_at": "2026-01-02T09:02:00Z", "source_group_name": "示例招聘群", "message_type": "text", "sender": "示例发布者", "sent_at": "2026-01-02T09:00:00Z", "recognition_status": "needs_review", "text_preview": "示例科技招聘数据工程师", "original_text": "示例科技招聘数据工程师，上海，本科", "error": "mock processing error", "source_reference": {"raw_message_id": "raw-1", "source_group_name": "示例招聘群", "external_message_id": "wx-message-1", "sender": "示例发布者", "sent_at": "2026-01-02T09:00:00Z", "message_type": "text", "original_text_available": True, "current_text_available": True, "source_status": "available"}, "subtasks": [{"id": "subtask-1", "kind": "consolidate_company", "status": "succeeded", "stage": "completed", "attempts": 1, "created_at": "2026-01-02T09:02:00Z", "updated_at": "2026-01-02T09:02:00Z", "result": {"status": "succeeded"}}]}
        return {"state": "paused", "stats": {"failed": 1}, "source_recognition": {"failed": 1, "needs_review": 1}, "items": [item], "total": 1, "job_total": 2}
    if path == "/admin/processing-queue/queue-1/logs":
        return [{"id": "log-1", "stage": "classifying", "level": "error", "message": "mock stage log", "details": {}, "created_at": "2026-01-02T09:02:00Z"}]
    return {}


def business_display_check():
    print("START business display", flush=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=CHROME)
        writes = []
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.set_default_timeout(5000)

        def route(route):
            request = route.request
            if "/api/v1/" not in request.url:
                route.continue_()
                return
            path = request.url.split("/api/v1", 1)[1].split("?", 1)[0]
            if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                writes.append((request.method, path))
            route.fulfill(status=200, content_type="application/json", body=json.dumps(business_mock_response(path, "admin"), ensure_ascii=False))

        page.route("**/*", route)
        try:
            page.goto("http://127.0.0.1:5173/", wait_until="domcontentloaded", timeout=10000)
            page.wait_for_timeout(500)
            page.locator(".search-input").fill("示例")
            page.locator(".search-input").press("Enter")
            page.wait_for_timeout(200)
            page.locator(".company-card").click()
            page.wait_for_selector(".company-tabs")
            assert page.get_by_role("heading", name="示例科技").count() >= 1
            assert page.locator(".company-tabs button").count() == 3
            page.get_by_role("button", name="查看来源与证据（1）").click()
            assert page.locator(".evidence-panel").count() == 1
            page.get_by_role("button", name="关闭来源与证据").click()
            page.locator(".company-tabs button").nth(1).click()
            assert page.locator(".timeline-event").count() == 2
            page.locator(".company-tabs button").nth(2).click()
            page.get_by_role("button", name="← 返回企业列表").click()
            assert page.locator(".company-card").count() == 1
            assert page.locator(".search-input").input_value() == "示例"
            page.locator("#main-navigation .nav-button").nth(1).click()
            assert page.locator(".timeline-event").count() == 2
            page.locator(".timeline-event").nth(0).locator("summary").click()
            page.locator(".timeline-event").nth(1).locator("summary").click()
            assert page.locator("details.timeline-event[open]").count() == 1
            page.locator("#main-navigation .nav-button").nth(2).click()
            assert page.locator(".kanban-tabs").count() == 1
            assert page.locator(".kanban-column").count() == 4
            page.screenshot(path=str(ARTIFACTS / "business-desktop.png"), full_page=True)
            print("PASS business company/timeline/desktop-board", flush=True)
        except Exception:
            screenshot = ARTIFACTS / "FAILED-business-desktop.png"
            page.screenshot(path=str(screenshot), full_page=True)
            print(f"FAIL business desktop screenshot={screenshot}", flush=True)
            traceback.print_exc()
            raise
        finally:
            page.close()

        page = browser.new_page(viewport={"width": 680, "height": 900})
        page.set_default_timeout(5000)
        page.route("**/*", route)
        try:
            page.goto("http://127.0.0.1:5173/", wait_until="domcontentloaded", timeout=10000)
            page.wait_for_timeout(500)
            page.locator(".mobile-menu-button").click()
            page.locator("#main-navigation .nav-button").nth(2).click()
            page.wait_for_selector(".kanban-tabs")
            page.locator(".kanban-tabs button").nth(1).click()
            assert page.locator('.kanban[data-active-state="applied"]').count() == 1
            page.locator(".mobile-menu-button").click()
            page.locator("#main-navigation .nav-button").nth(3).click()
            page.wait_for_selector(".import-tabs")
            page.locator(".import-tabs button").nth(2).click()
            page.wait_for_selector(".group-option")
            page.locator(".group-option input").check()
            page.get_by_role("button", name="保存选择").click()
            page.wait_for_timeout(300)
            assert "2 选择消息" in page.locator(".import-step-controls").inner_text()
            page.get_by_role("button", name="上一步").click()
            assert "1 选择群聊" in page.locator(".import-step-controls").inner_text()
            page.get_by_role("button", name="下一步").click()
            page.locator(".import-message input[type=checkbox]").check()
            assert "确认并导入 1 条记录" in page.locator(".import-messages-card").inner_text()
            page.get_by_role("button", name="确认并导入 1 条记录 →").click()
            page.wait_for_timeout(500)
            assert page.get_by_role("heading", name="处理队列").count() == 1
            assert page.locator(".queue-item").count() == 1
            assert "mock processing error" in page.locator(".queue-error").inner_text()
            assert "招聘识别与结构化" in page.locator(".queue-item > .queue-current-step").inner_text()
            original = page.locator("details.queue-original")
            assert not original.evaluate("element => element.open")
            original.locator("summary").click()
            assert original.evaluate("element => element.open")
            page.get_by_role("button", name="查看日志").first.click()
            page.get_by_text("mock stage log").wait_for()
            assert "mock stage log" in page.get_by_text("mock stage log").inner_text()
            page.screenshot(path=str(ARTIFACTS / "business-mobile.png"), full_page=True)
            print("PASS business mobile import/queue", flush=True)
        except Exception:
            screenshot = ARTIFACTS / "FAILED-business-mobile.png"
            page.screenshot(path=str(screenshot), full_page=True)
            print(f"FAIL business mobile screenshot={screenshot}", flush=True)
            traceback.print_exc()
            raise
        finally:
            page.close()
            browser.close()
    assert not any(path in {"/admin/sync", "/exports"} for _, path in writes)
    print(f"PASS business writes={writes}", flush=True)


if __name__ == "__main__":
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    if "--business" in sys.argv:
        business_display_check()
        sys.exit(0)
    for role in ("member", "admin"):
        for width in (360, 680, 1000, 1440):
            try:
                check(role, width)
            except Exception:
                sys.exit(1)
    settings_outcome(False)
    settings_outcome(True)
    settings_stay_abandon()
