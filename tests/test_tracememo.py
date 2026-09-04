import app.tracememo as tracememo


class FakeResponse:
    def __init__(self, value, content=b"", headers=None):
        self.value = value
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self.value


def test_tracememo_response_shapes(monkeypatch):
    def fake_get(url, **kwargs):
        if url.endswith("/health"):
            return FakeResponse({"status": "ok"})
        if url.endswith("/chatroom"):
            return FakeResponse({"data": [{"id": "room-1", "name": "招聘群"}]})
        if url.endswith("/media/media-1"):
            return FakeResponse(None, b"image-bytes", {"content-type": "image/png", "content-disposition": "attachment; filename=notice.png"})
        return FakeResponse({"data": [{"id": "message-1", "type": "text", "text": "招聘"}]})

    monkeypatch.setattr(tracememo.httpx, "get", fake_get)
    client = tracememo.TraceMemoClient("http://127.0.0.1:6131/api/v1", "token")
    assert client.health()["status"] == "ok"
    assert client.groups()[0]["id"] == "room-1"
    assert client.recent("room-1")[0]["id"] == "message-1"
    assert client.media("media-1") == (b"image-bytes", "notice.png")


def test_tracememo_chatroom_fields_are_normalized():
    normalized = tracememo.normalize_group(
        {
            "m_nsUsrName": "123@chatroom",
            "m_nsNickName": "招聘交流群",
            "md5": "room-md5",
        }
    )
    assert normalized == {
        "external_id": "123@chatroom",
        "name": "招聘交流群",
        "avatar": None,
    }
