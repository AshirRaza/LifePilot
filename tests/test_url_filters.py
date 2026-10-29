"""Conservative crawl-trap filter tests."""

from lifepilot.crawl.url_filters import is_crawl_trap_url


def test_allows_guidance_paths_with_events_or_print_segments():
    # Must not indiscriminately reject legitimate guidance URLs.
    ok, _ = is_crawl_trap_url(
        "https://www.gov.uk/guidance/immigration-events-for-sponsors"
    )
    assert ok is False
    ok, _ = is_crawl_trap_url("https://www.gov.uk/skilled-worker-visa/print")
    assert ok is False


def test_allows_non_trap_immigration_urls():
    ok, reason = is_crawl_trap_url(
        "https://www.gov.uk/skilled-worker-visa/eligibility"
    )
    assert ok is False
    assert reason == ""


def test_rejects_search_and_login_segments():
    trapped, reason = is_crawl_trap_url("https://www.gov.uk/search/all?q=visa")
    assert trapped is True
    assert "search" in reason
    trapped, _ = is_crawl_trap_url("https://www.gov.uk/login")
    assert trapped is True


def test_rejects_utm_tracking_queries():
    trapped, reason = is_crawl_trap_url(
        "https://www.gov.uk/skilled-worker-visa?utm_source=x&utm_medium=y"
    )
    assert trapped is True
    assert "utm" in reason or "tracking" in reason
