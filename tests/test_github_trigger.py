from groc.github_trigger import DISPATCH_URL, trigger_scrape_now


class _FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def test_trigger_scrape_now_skips_when_no_token_configured(monkeypatch):
    monkeypatch.delenv("GH_DISPATCH_TOKEN", raising=False)
    calls = []
    monkeypatch.setattr("groc.github_trigger.requests.post", lambda *a, **k: calls.append((a, k)))

    result = trigger_scrape_now("M5V2H1")

    assert result is False
    assert calls == []  # never even attempted the request without a token


def test_trigger_scrape_now_success(monkeypatch):
    monkeypatch.setenv("GH_DISPATCH_TOKEN", "fake-token")
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return _FakeResponse(204)

    monkeypatch.setattr("groc.github_trigger.requests.post", fake_post)

    result = trigger_scrape_now("M5V2H1")

    assert result is True
    assert len(calls) == 1
    assert calls[0]["url"] == DISPATCH_URL
    assert calls[0]["headers"]["Authorization"] == "Bearer fake-token"
    assert calls[0]["json"] == {"ref": "main"}


def test_trigger_scrape_now_returns_false_on_error_status(monkeypatch):
    monkeypatch.setenv("GH_DISPATCH_TOKEN", "fake-token")
    monkeypatch.setattr("groc.github_trigger.requests.post", lambda *a, **k: _FakeResponse(401, "bad credentials"))

    assert trigger_scrape_now("M5V2H1") is False


def test_trigger_scrape_now_never_raises_on_network_error(monkeypatch):
    monkeypatch.setenv("GH_DISPATCH_TOKEN", "fake-token")

    def raise_network_error(*a, **k):
        raise ConnectionError("network is down")

    monkeypatch.setattr("groc.github_trigger.requests.post", raise_network_error)

    # Must not raise -- a failed trigger must never break the search request that caused it.
    assert trigger_scrape_now("M5V2H1") is False
