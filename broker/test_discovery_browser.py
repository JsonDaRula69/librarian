import hashlib

from broker.discovery_browser import DiscoveryBrowser


def test_fetch_read_and_links_are_bounded_and_provenanced(monkeypatch):
    class Wire:
        data = b"<html><script>secret()</script><body>hello <a href='/next'>Next</a></body></html>"
        url = "https://fictional.example/docs"
        status = 200
        redirects = ()

    calls = []
    def fake_fetch(url, *, max_bytes, timeout, max_redirects):
        calls.append((url, max_bytes, timeout, max_redirects))
        return Wire()

    browser = DiscoveryBrowser(fetch=fake_fetch, max_bytes=100, timeout=3)
    receipt = browser.fetch("https://fictional.example/docs")
    assert receipt["authority"] == "unverified_candidate"
    assert receipt["requested_url"] == "https://fictional.example/docs"
    assert receipt["final_url"] == Wire.url
    assert receipt["http_status"] == 200
    assert receipt["sha256"] == hashlib.sha256(Wire.data).hexdigest()
    assert browser.read(receipt["receipt_id"], 0, 100)["text"] == "hello Next"
    links = browser.enumerate_links(receipt["receipt_id"])
    assert links == [{"url": "https://fictional.example/next", "source_receipt_id": receipt["receipt_id"], "ordinal": 0, "discovered_from": Wire.url}]
    assert calls == [("https://fictional.example/docs", 100, 3, 5)]


def test_pagination_and_truncation_do_not_use_paths():
    class Wire:
        data = b"# Docs\n\n[one](https://fictional.example/one) [two](https://fictional.example/two)\n\nlong text"
        url = "https://fictional.example/start"
        status = 200
        redirects = ("https://fictional.example/start",)
    browser = DiscoveryBrowser(fetch=lambda *a, **k: Wire(), max_text=8)
    rec = browser.fetch(Wire.url)
    assert browser.read(rec["receipt_id"], 0, 5)["text"] == "# Doc"
    assert browser.read(rec["receipt_id"], 5, 5)["text"] == "s\n\n"
    assert browser.read(rec["receipt_id"], 0, 5)["truncated"] is True
    assert browser.enumerate_links(rec["receipt_id"])[1]["discovered_from"] == Wire.url
    assert rec["redirects"] == Wire.redirects


def test_unknown_receipt_is_rejected():
    browser = DiscoveryBrowser(fetch=lambda *a, **k: None)
    try:
        browser.read("missing", 0, 1)
    except KeyError:
        pass
    else:
        raise AssertionError("unknown receipt must not access caller paths")
