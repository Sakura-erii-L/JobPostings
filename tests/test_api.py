from app import db
from app.main import app


def test_bootstrap_import_and_query(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    monkeypatch.setenv("JOBPOSTINGS_DOWNLOAD_DIR", str(tmp_path / "Downloads"))
    from fastapi.testclient import TestClient

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/v1/bootstrap/status").json() == {"initialized": False}
        bootstrap = client.post("/api/v1/bootstrap", json={"email": "admin@example.com"})
        assert bootstrap.status_code == 200
        assert client.get("/api/v1/auth/me").json()["user"]["role"] == "admin"
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
        assert client.post("/api/v1/bootstrap", json={"email": "admin@example.com"}).status_code == 200
        created = client.post(
            "/api/v1/admin/invitations",
            json={"email": "member@example.com", "role": "member"},
        )
        assert created.status_code == 200
        assert created.json()["email"] == "member@example.com"
        assert created.json()["expires_in_hours"] == 72

        listed = client.get("/api/v1/admin/invitations")
        assert listed.status_code == 200
        assert listed.json()[0]["email"] == "member@example.com"
        assert listed.json()[0]["role"] == "member"
        assert listed.json()[0]["used_at"] is None


def test_connector_secret_is_preserved_and_agent_scopes_are_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "data_dir", tmp_path)
    monkeypatch.setattr(db.config, "db_path", tmp_path / "data" / "jobpostings.db")
    from fastapi.testclient import TestClient

    with TestClient(app, client=("127.0.0.1", 50001)) as client:
        assert client.post("/api/v1/bootstrap", json={"email": "admin@example.com"}).status_code == 200
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
        assert client.post("/api/v1/bootstrap", json={"email": "admin@example.com"}).status_code == 200
        assert client.put("/api/v1/admin/connectors/tracememo", json={"enabled": True}).status_code == 200
        response = client.get("/api/v1/admin/connectors/tracememo/groups")
        assert response.status_code == 200
        groups = response.json()
        assert [group["external_id"] for group in groups] == ["room-1", "room-2"]
        assert [group["name"] for group in groups] == ["招聘群一", "招聘群二"]
        assert len({group["id"] for group in groups}) == 2
