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
        assert client.get("/api/v1/auth/options").json() == {"password_login_enabled": True, "otp_login_enabled": False}
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

        retried = client.post(f"/api/v1/admin/processing-queue/{job['id']}/retry")
        assert retried.status_code == 200
        assert retried.json() == {"id": job["id"], "status": "pending"}


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
