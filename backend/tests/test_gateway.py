import json

import httpx

from .conftest import login


class FakeResp:
    def __init__(self, content, usage=True):
        self.status_code = 200
        self._d = {"choices": [{"message": {"content": content}}]}
        if usage:
            self._d["usage"] = {"prompt_tokens": 1000, "completion_tokens": 200}
        self.request = httpx.Request("POST", "http://x")

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


def test_openai_compatible_retry_and_cost(client, monkeypatch):
    from reviewdesk.config import get_settings

    monkeypatch.setattr(get_settings(), "offline", False)
    c = client
    h = login(c)
    pid = c.post("/api/projects", json={"name": "p"}, headers=h).json()["id"]
    c.post("/api/admin/providers", json={"name": "deepseek", "base_url": "http://fake/v1", "api_key": "sk", "default_model": "deepseek-chat", "price_in": 2.0, "price_out": 8.0}, headers=h)
    c.put("/api/admin/routes", json={"task": "P16", "provider": "deepseek"}, headers=h)
    c.put(f"/api/projects/{pid}/settings", json={"ai_level_max": "L2"}, headers=h)
    calls = []
    good = json.dumps({"points": [{"text": "mitophagy down", "card_ids": ["EC-1"]}], "order": "a", "rationale": "b", "author_judgment": "c"})

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url, json, headers))
        return FakeResp("not json at all" if len(calls) == 1 else f"```json\n{good}\n```")

    monkeypatch.setattr("reviewdesk.gateway.gateway.httpx.post", fake_post)
    r = c.post(f"/api/projects/{pid}/ai/P16", json={"params": {"n": 1, "thesis": "t", "cards": "EC-1"}}, headers=h).json()
    assert r["valid"] and r["provider"] == "deepseek" and r["data"]["points"][0]["text"] == "mitophagy down"
    assert len(calls) == 2 and "未通过格式校验" in calls[1][1]["messages"][1]["content"]
    assert calls[0][0] == "http://fake/v1/chat/completions" and calls[0][2]["Authorization"] == "Bearer sk"
    assert calls[0][1]["temperature"] == 0.2
    run = c.get(f"/api/projects/{pid}/runs/{r['run_id']}", headers=h).json()
    assert run["prompt_version"] == "P16@1.1.0" and run["tokens_in"] == 2000
    assert abs(run["cost"] - (2000 * 2 + 400 * 8) / 1e6) < 1e-9
    assert run["input_hash"].startswith("sha256:")

    # 预算上限
    c.put(f"/api/projects/{pid}/settings", json={"budget_limit": 0.001}, headers=h)
    assert c.post(f"/api/projects/{pid}/ai/P16", json={}, headers=h).status_code == 402
