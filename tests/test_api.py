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
