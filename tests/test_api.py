import json
from datetime import datetime, timedelta, timezone

from app import db
from app.main import app


def test_bootstrap_import_and_query(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    monkeypatch.setenv("JOBPOSTINGS_DOWNLOAD_DIR", str(tmp_path / "Downloads"))
    from fastapi.testclient import TestClient

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/v1/bootstrap/status").json() == {"initialized": False}
        bootstrap = client.post("/api/v1/bootstrap", json={"email": "admin@example.com", "password": "AdminPass123!"})
        assert bootstrap.status_code == 200
        current_user = client.get("/api/v1/auth/me").json()["user"]
        assert current_user["role"] == "admin"
        assert current_user["password_configured"] is True
        assert "password_hash" not in current_user
        assert client.post("/api/v1/auth/logout").status_code == 200
        assert client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "wrong-pass"}).status_code == 401
        assert client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "AdminPass123!"}).status_code == 200
        assert client.post("/api/v1/auth/password", json={"password": "NewAdminPass123!"}).json() == {"ok": True, "password_configured": True}
        assert client.post("/api/v1/auth/logout").status_code == 200
        assert client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "AdminPass123!"}).status_code == 401
        assert client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "NewAdminPass123!"}).status_code == 200
        assert client.get("/api/v1/auth/options").json() == {
            "password_login_enabled": True,
            "otp_login_enabled": False,
            "initial_admin_password_required": False,
            "local_password_setup_allowed": True,
        }
        assert client.post("/api/v1/auth/request-code", json={"email": "admin@example.com"}).status_code == 403
        imported = client.post("/api/v1/imports/text", json={"text": "测试科技招聘算法工程师，地点南京"})
        assert imported.status_code == 200
        assert client.get("/api/v1/companies").status_code == 200
        exported = client.post("/api/v1/exports?fmt=csv")
        assert exported.status_code == 200
        assert client.get(exported.json()["download_url"]).status_code == 200


def test_admin_can_create_and_list_invitations(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    from fastapi.testclient import TestClient

    with TestClient(app, client=("127.0.0.1", 50002)) as client:
        assert client.post("/api/v1/bootstrap", json={"email": "admin@example.com", "password": "AdminPass123!"}).status_code == 200
        created = client.post(
            "/api/v1/admin/invitations",
            json={"email": "member@example.com", "role": "member", "password": "MemberPass123!"},
        )
        assert created.status_code == 200
        assert created.json()["email"] == "member@example.com"
        assert created.json()["expires_in_hours"] == 72

        listed = client.get("/api/v1/admin/invitations")
        assert listed.status_code == 200
        assert listed.json()[0]["email"] == "member@example.com"
        assert listed.json()[0]["role"] == "member"
        assert listed.json()[0]["used_at"] is None
        logged_in = client.post("/api/v1/auth/login", json={"email": "member@example.com", "password": "MemberPass123!"})
        assert logged_in.status_code == 200
        assert logged_in.json()["user"]["role"] == "member"
        assert db.one("SELECT used_at FROM invitations WHERE email=?", ("member@example.com",))["used_at"] is not None
        for path in ("/api/v1/admin/settings", "/api/v1/admin/connectors", "/api/v1/admin/processing-queue", "/api/v1/admin/review-items"):
            assert client.get(path).status_code == 403
        assert client.post("/api/v1/admin/sync").status_code == 403
        assert client.post("/api/v1/imports/text", json={"text": "普通账户不应导入"}).status_code == 403
        assert client.get("/api/v1/companies").status_code == 200


def test_connector_secret_is_preserved_and_agent_scopes_are_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    from fastapi.testclient import TestClient

    with TestClient(app, client=("127.0.0.1", 50001)) as client:
        assert client.post("/api/v1/bootstrap", json={"email": "admin@example.com", "password": "AdminPass123!"}).status_code == 200
        first = client.put(
            "/api/v1/admin/connectors/tracememo",
            json={"base_url": "http://127.0.0.1:6131/api/v1", "enabled": True, "token": "secret-token"},
        )
        assert first.status_code == 200
        original_token = db.one("SELECT config_json FROM connectors WHERE kind='tracememo'")["config_json"]
        second = client.put(
            "/api/v1/admin/connectors/tracememo",
            json={"base_url": "http://127.0.0.1:6131/api/v1", "enabled": False},
        )
        assert second.status_code == 200
        assert db.one("SELECT config_json FROM connectors WHERE kind='tracememo'")["config_json"] == original_token

        assert client.put("/api/v1/admin/settings", json={"values": {"agent_api_enabled": True}}).status_code == 200
        created = client.post("/api/v1/api-tokens", json={"name": "catalog", "scopes": ["catalog:read"]})
        assert created.status_code == 200
        bearer = {"Authorization": f"Bearer {created.json()['token']}"}
        assert client.get("/api/v1/companies", headers=bearer).status_code == 200
        assert client.get("/api/v1/me/applications", headers=bearer).status_code == 403


def test_tracememo_groups_keep_distinct_trace_memo_ids_and_names(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    from fastapi.testclient import TestClient
    from app import main as main_module

    monkeypatch.setattr(
        main_module.TraceMemoClient,
        "groups",
        lambda self: [
            {"m_nsUsrName": "room-1", "m_nsNickName": "招聘群一", "md5": "md5-1"},
            {"m_nsUsrName": "room-2", "m_nsNickName": "招聘群二", "md5": "md5-2"},
        ],
    )
    with TestClient(app, client=("127.0.0.1", 50003)) as client:
        assert client.post("/api/v1/bootstrap", json={"email": "admin@example.com", "password": "AdminPass123!"}).status_code == 200
        assert client.put("/api/v1/admin/connectors/tracememo", json={"enabled": True}).status_code == 200
        response = client.get("/api/v1/admin/connectors/tracememo/groups")
        assert response.status_code == 200
        groups = response.json()
        assert [group["external_id"] for group in groups] == ["room-1", "room-2"]
        assert [group["name"] for group in groups] == ["招聘群一", "招聘群二"]
        assert len({group["id"] for group in groups}) == 2

        selected = client.put(
            "/api/v1/admin/source-groups",
            json={"groups": [{"id": groups[0]["id"], "selected": True, "enabled": True}]},
        )
        assert selected.status_code == 200
        assert client.put("/api/v1/admin/connectors/tracememo", json={"enabled": True}).status_code == 200
        persisted = client.get("/api/v1/admin/connectors/tracememo/groups")
        assert persisted.status_code == 200
        assert persisted.json()[0]["selected"] is True


def test_processing_queue_lists_and_retries_failed_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    from fastapi.testclient import TestClient
    from app import main as main_module

    monkeypatch.setattr(main_module, "process_one_batch", lambda limit: None)
    monkeypatch.setattr(main_module, "process_one_enrichment", lambda: None)
    with TestClient(app, client=("127.0.0.1", 50004)) as client:
        assert client.post("/api/v1/bootstrap", json={"email": "admin@example.com", "password": "AdminPass123!"}).status_code == 200
        imported = client.post("/api/v1/imports/text", json={"text": "锦浪科技招聘产品研发类岗位"})
        assert imported.status_code == 200
        raw_message_id = imported.json()["raw_message_id"]
        job = db.one("SELECT id FROM processing_jobs WHERE raw_message_id=?", (raw_message_id,))
        assert job
        with db.connect() as connection:
            connection.execute(
                "UPDATE processing_jobs SET status='needs_review',error='test failure',updated_at=? WHERE id=?",
                ("2026-09-04T00:00:00+00:00", job["id"]),
            )

        queue = client.get("/api/v1/admin/processing-queue?status=needs_review")
        assert queue.status_code == 200
        assert queue.json()["items"][0]["raw_message_id"] == raw_message_id
        assert queue.json()["items"][0]["text_preview"].startswith("锦浪科技")

        canceled = client.post("/api/v1/admin/processing-queue/cancel", json={"ids": [job["id"]]})
        assert canceled.status_code == 200
        assert canceled.json()["canceled"] == 1
        assert db.one("SELECT status FROM processing_jobs WHERE id=?", (job["id"],))["status"] == "canceled"
        assert db.one("SELECT stage FROM processing_logs WHERE processing_job_id=?", (job["id"],))["stage"] == "canceled"
        assert db.one("SELECT recognition_status FROM raw_messages WHERE id=?", (raw_message_id,))["recognition_status"] == "canceled"
        visible = client.get("/api/v1/admin/processing-queue")
        assert visible.status_code == 200
        assert all(item["id"] != job["id"] for item in visible.json()["items"])
        canceled_view = client.get("/api/v1/admin/processing-queue?status=canceled")
        assert canceled_view.status_code == 200
        assert canceled_view.json()["items"][0]["id"] == job["id"]

        retried = client.post(f"/api/v1/admin/processing-queue/{job['id']}/retry")
        assert retried.status_code == 200
        assert retried.json() == {"id": job["id"], "status": "pending"}
        assert db.one("SELECT recognition_status FROM raw_messages WHERE id=?", (raw_message_id,))["recognition_status"] == "pending"


def test_manual_sync_requires_selected_groups(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    from fastapi.testclient import TestClient

    with TestClient(app, client=("127.0.0.1", 50005)) as client:
        assert client.post("/api/v1/bootstrap", json={"email": "admin@example.com", "password": "AdminPass123!"}).status_code == 200
        assert client.put("/api/v1/admin/connectors/tracememo", json={"enabled": True}).status_code == 200
        response = client.post("/api/v1/admin/sync")
        assert response.status_code == 400
        assert "没有已选中的微信群" in response.json()["detail"]


def test_review_items_include_current_task_original_message_and_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    from fastapi.testclient import TestClient

    with TestClient(app, client=("127.0.0.1", 50008)) as client:
        assert client.post("/api/v1/bootstrap", json={"email": "admin@example.com", "password": "AdminPass123!"}).status_code == 200
        imported = client.post("/api/v1/imports/text", json={"text": "审核用的完整原始招聘正文"})
        assert imported.status_code == 200
        raw_id = imported.json()["raw_message_id"]
        job = db.one("SELECT id FROM processing_jobs WHERE raw_message_id=?", (raw_id,))
        assert job
        with db.connect() as connection:
            connection.execute("UPDATE processing_jobs SET status='needs_review',stage='failed',error='完整错误信息' WHERE id=?", (job["id"],))
            connection.execute(
                "INSERT INTO processing_logs(id,processing_job_id,stage,level,message,details_json,created_at) VALUES(?,?,?,?,?,?,?)",
                ("review-log", job["id"], "failed", "error", "失败阶段日志", json.dumps({"detail": "保留"}, ensure_ascii=False), db.utc_now()),
            )
            connection.execute(
                "INSERT INTO review_items(id,kind,entity_type,entity_id,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                ("review-1", "processing_failed", "processing_job", raw_id, json.dumps({"job_id": job["id"]}, ensure_ascii=False), db.utc_now()),
            )
        response = client.get("/api/v1/admin/review-items")
        assert response.status_code == 200
        payload = response.json()[0]["payload"]
        assert payload["job"]["error"] == "完整错误信息"
        assert payload["original_message"]["original_text_content"] == "审核用的完整原始招聘正文"
        assert payload["processing_logs"][0]["message"] == "失败阶段日志"


def test_manual_sync_accepts_force_request(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    from fastapi.testclient import TestClient
    from app import main as main_module

    monkeypatch.setattr(main_module, "sync_tracememo_once", lambda force=False: {"status": "completed", "force": force, "fetched": 0, "created": 0, "updated": 0, "duplicates": 0, "ignored": 0, "groups": 1})
    with TestClient(app, client=("127.0.0.1", 50009)) as client:
        assert client.post("/api/v1/bootstrap", json={"email": "admin@example.com", "password": "AdminPass123!"}).status_code == 200
        assert client.put("/api/v1/admin/connectors/tracememo", json={"enabled": True}).status_code == 200
        with db.connect() as connection:
            connector_id = connection.execute("SELECT id FROM connectors WHERE kind='tracememo'").fetchone()["id"]
            connection.execute("INSERT INTO source_groups(id,connector_id,external_id,name,selected,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", ("group", connector_id, "room", "招聘群", 1, 1, db.utc_now(), db.utc_now()))
        response = client.post("/api/v1/admin/sync", json={"force": True})
        assert response.status_code == 200
        assert response.json()["force"] is True


def test_tracememo_sync_uses_rolling_import_days_and_source_datetime(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    from app import main as main_module

    with db.connect() as connection:
        connection.execute(
            "INSERT INTO connectors(id,kind,base_url,enabled,config_json,updated_at) VALUES(?,?,?,?,?,?)",
            ("trace", "tracememo", "http://trace", 1, "{}", db.utc_now()),
        )
        connection.execute(
            "INSERT INTO source_groups(id,connector_id,external_id,name,selected,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("group", "trace", "room", "招聘群", 1, 1, db.utc_now(), db.utc_now()),
        )
        connection.execute(
            "INSERT INTO sync_cursors(source_group_id,cursor_time,cursor_message_id,updated_at) VALUES(?,?,?,?)",
            ("group", "2026-01-01T00:00:00+00:00", "old-cursor", db.utc_now()),
        )
        connection.execute(
            "UPDATE system_settings SET value_json=? WHERE key='import_days'",
            (json.dumps(1),),
        )

    captured: dict[str, datetime] = {}
    now = datetime.now(timezone.utc)

    class FakeTraceMemoClient:
        def __init__(self, base_url, token):
            pass

        def messages(self, talker, start, end):
            captured["start"] = start
            captured["end"] = end
            return [
                {
                    "id": "inside-window",
                    "type": "普通文本",
                    "text": "窗口内招聘信息",
                    "datetime": (now - timedelta(hours=2)).isoformat(timespec="seconds"),
                },
                {
                    "id": "outside-window",
                    "type": "普通文本",
                    "text": "窗口外历史招聘信息",
                    "datetime": (now - timedelta(days=2)).isoformat(timespec="seconds"),
                },
                {
                    "id": "creation-only",
                    "type": "普通文本",
                    "text": "只有创建时间的消息",
                    "createTime": int(now.timestamp()),
                },
            ]

    monkeypatch.setattr(main_module, "TraceMemoClient", FakeTraceMemoClient)
    result = main_module._sync_tracememo_once()
    assert captured["end"] - captured["start"] == timedelta(days=1)
    assert result["import_days"] == 1
    assert result["fetched"] == 3
    assert result["created"] == 1
    assert result["outside_window"] == 1
    assert result["missing_source_time"] == 1
    assert db.one("SELECT COUNT(*) AS count FROM raw_messages")["count"] == 1
    assert db.one("SELECT external_message_id FROM raw_messages")["external_message_id"] == "inside-window"


def test_tracememo_messages_are_cached_until_force_refetch(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    from app import main as main_module

    with db.connect() as connection:
        connection.execute(
            "INSERT INTO connectors(id,kind,base_url,enabled,config_json,updated_at) VALUES(?,?,?,?,?,?)",
            ("trace", "tracememo", "http://trace", 1, "{}", db.utc_now()),
        )
        connection.execute(
            "INSERT INTO source_groups(id,connector_id,external_id,name,selected,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("group", "trace", "room", "招聘群", 1, 1, db.utc_now(), db.utc_now()),
        )
    calls: list[str] = []
    now = datetime.now(timezone.utc)

    class FakeTraceMemoClient:
        def __init__(self, base_url, token):
            pass

        def messages(self, talker, start, end):
            calls.append(talker)
            return [{"id": "cached-message", "type": "普通文本", "text": "缓存招聘信息", "datetime": (now - timedelta(hours=1)).isoformat(timespec="seconds")}]

    monkeypatch.setattr(main_module, "TraceMemoClient", FakeTraceMemoClient)
    first = main_module._sync_tracememo_once()
    second = main_module._sync_tracememo_once()
    forced = main_module._sync_tracememo_once(force=True)

    assert calls == ["room", "room"]
    assert first["cache_mode"] == "tracememo"
    assert first["remote_fetches"] == 1
    assert second["cache_mode"] == "cache"
    assert second["cached_messages"] == 1
    assert forced["cache_mode"] == "tracememo"
    assert db.one("SELECT COUNT(*) AS count FROM tracememo_message_cache")["count"] == 1


def test_tracememo_auto_sync_fetches_from_group_cursor_and_skips_recognized_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    from app import main as main_module

    initial_cursor = datetime.now(timezone.utc) - timedelta(minutes=10)
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO connectors(id,kind,base_url,enabled,config_json,updated_at) VALUES(?,?,?,?,?,?)",
            ("trace", "tracememo", "http://trace", 1, "{}", db.utc_now()),
        )
        connection.execute(
            "INSERT INTO source_groups(id,connector_id,external_id,name,selected,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("group", "trace", "room", "招聘群", 1, 1, db.utc_now(), db.utc_now()),
        )
        connection.execute(
            "INSERT INTO sync_cursors(source_group_id,cursor_time,cursor_message_id,updated_at) VALUES(?,?,?,?)",
            ("group", initial_cursor.isoformat(timespec="seconds"), None, db.utc_now()),
        )

    calls: list[tuple[datetime, datetime]] = []

    class FakeTraceMemoClient:
        def __init__(self, base_url, token):
            pass

        def messages(self, talker, start, end):
            calls.append((start, end))
            return [{
                "id": "incremental-message",
                "type": "普通文本",
                "text": "增量招聘信息",
                "datetime": (end - timedelta(seconds=1)).isoformat(timespec="seconds"),
            }]

    monkeypatch.setattr(main_module, "TraceMemoClient", FakeTraceMemoClient)
    first = main_module._sync_tracememo_once(incremental=True)
    raw_id = db.one("SELECT id FROM raw_messages WHERE external_message_id='incremental-message'")["id"]
    with db.connect() as connection:
        connection.execute("UPDATE processing_jobs SET status='succeeded',stage='completed' WHERE raw_message_id=?", (raw_id,))
        connection.execute("UPDATE raw_messages SET is_recruitment=0,recognition_status='succeeded',recognized_at=? WHERE id=?", (db.utc_now(), raw_id))
    second = main_module._sync_tracememo_once(incremental=True)

    assert first["incremental"] is True
    assert first["created"] == 1
    assert first["remote_fetches"] == 1
    assert second["incremental"] is True
    assert second["created"] == 0
    assert second["recognized_skipped"] == 1
    assert second["cached_groups"] == 0
    assert len(calls) == 2
    assert calls[1][0] >= calls[0][1] - timedelta(minutes=1, seconds=1)
    assert db.one("SELECT COUNT(*) AS count FROM processing_jobs WHERE raw_message_id=?", (raw_id,))["count"] == 1


def test_admin_can_manage_local_storage_and_edit_company(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    from fastapi.testclient import TestClient
    from app import main as main_module
    from app.tracememo_cache import store_messages

    monkeypatch.setattr(main_module, "process_one_batch", lambda limit: None)
    monkeypatch.setattr(main_module, "process_one_enrichment", lambda: None)
    with TestClient(app, client=("127.0.0.1", 50010)) as client:
        assert client.post("/api/v1/bootstrap", json={"email": "admin@example.com", "password": "AdminPass123!"}).status_code == 200
        now = datetime.now(timezone.utc)
        with db.connect() as connection:
            connection.execute(
                "INSERT INTO connectors(id,kind,base_url,enabled,config_json,updated_at) VALUES(?,?,?,?,?,?)",
                ("trace", "tracememo", "http://trace", 1, "{}", db.utc_now()),
            )
            connection.execute(
                "INSERT INTO source_groups(id,connector_id,external_id,name,selected,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                ("group", "trace", "room", "招聘群", 1, 1, db.utc_now(), db.utc_now()),
            )
            connection.execute(
                "INSERT INTO companies(id,display_name,primary_industry,created_at,updated_at) VALUES(?,?,?,?,?)",
                ("company", "原始企业名", "other", db.utc_now(), db.utc_now()),
            )
        store_messages("trace", "group", [{"id": "cached", "type": "普通文本", "text": "缓存消息", "datetime": now.isoformat(timespec="seconds")}], now - timedelta(days=1), now)
        raw_id = main_module.ingest_message({"id": "raw", "type": "普通文本", "text": "本地聊天记录", "datetime": now.isoformat(timespec="seconds")}, "manual", None)
        assert raw_id
        backup_dir = db.config.data_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "manual-test.db").write_bytes(b"test backup")

        snapshot = client.get("/api/v1/admin/local-storage")
        assert snapshot.status_code == 200
        assert snapshot.json()["tracememo_cache"]["messages"] == 1
        assert snapshot.json()["chat_records"]["messages"] == 1
        assert snapshot.json()["backups"][0]["name"] == "manual-test.db"

        edited = client.put(
            "/api/v1/companies/company",
            json={
                "display_name": "管理员企业名",
                "legal_name": "管理员企业全称",
                "aliases": ["招聘品牌名"],
                "summary": "管理员确认的企业简介",
                "primary_industry": "electronics_semiconductor",
                "businesses": ["逆变器"],
            },
        )
        assert edited.status_code == 200
        assert edited.json()["display_name"] == "管理员企业名"
        assert edited.json()["aliases"] == ["招聘品牌名"]
        stored = db.one("SELECT display_name,summary,manual_overrides_json FROM companies WHERE id='company'")
        assert stored["display_name"] == "管理员企业名"
        assert json.loads(stored["manual_overrides_json"])["display_name"] == "管理员企业名"

        cleared_cache = client.delete("/api/v1/admin/local-storage/cache")
        assert cleared_cache.status_code == 200
        assert cleared_cache.json()["deleted"]["messages"] == 1
        cleared_chat = client.delete("/api/v1/admin/local-storage/chat-records")
        assert cleared_chat.status_code == 200
        assert cleared_chat.json()["deleted"]["messages"] == 1
        assert cleared_chat.json()["storage"]["chat_records"]["messages"] == 0
        deleted_backup = client.delete("/api/v1/admin/local-storage/backups/manual-test.db")
        assert deleted_backup.status_code == 200
        assert not (backup_dir / "manual-test.db").exists()


def test_local_admin_initial_password_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    from fastapi.testclient import TestClient

    with TestClient(app, client=("127.0.0.1", 50006)) as client:
        assert client.post("/api/v1/bootstrap", json={"email": "admin@example.com", "password": "AdminPass123!"}).status_code == 200
        with db.connect() as connection:
            connection.execute("UPDATE users SET password_hash=NULL WHERE email=?", ("admin@example.com",))
        assert client.post("/api/v1/auth/logout").status_code == 200
        options = client.get("/api/v1/auth/options")
        assert options.status_code == 200
        assert options.json()["initial_admin_password_required"] is True
        recovered = client.post("/api/v1/auth/initial-password", json={"email": "admin@example.com", "password": "RecoveredPass123!"})
        assert recovered.status_code == 200
        assert recovered.json()["user"]["password_configured"] is True
        assert client.post("/api/v1/auth/logout").status_code == 200
        assert client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "RecoveredPass123!"}).status_code == 200
        assert client.post("/api/v1/auth/initial-password", json={"email": "admin@example.com", "password": "AnotherPass123!"}).status_code == 409


def test_remote_admin_initial_password_recovery_is_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    db.init_db()
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO users(id,email,role,created_at) VALUES(?,?,?,?)",
            ("admin-id", "admin@example.com", "admin", "2026-09-04T00:00:00+00:00"),
        )
    from fastapi.testclient import TestClient

    with TestClient(app, client=("203.0.113.10", 50007)) as client:
        blocked = client.post("/api/v1/auth/initial-password", json={"email": "admin@example.com", "password": "RemotePass123!"})
        assert blocked.status_code == 403
