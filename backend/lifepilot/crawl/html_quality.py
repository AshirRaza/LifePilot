"""Conservative HTML content-quality checks for fetched government pages.

Detects recognizable bot-protection / WAF interstitials (e.g. Radware) so the
fetcher does not store them as successful knowledge documents.

Does not bypass challenges. Keeps false positives low by requiring clustered
signals for generic blocks; vendor-specific pages may use a tight marker set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape


OUTCOME_OK = "ok"
OUTCOME_BLOCKED_BY_BOT_PROTECTION = "blocked_by_bot_protection"
OUTCOME_INVALID_CONTENT = "invalid_content"

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_DESC_RE = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
_META_DESC_RE_ALT = re.compile(
    r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+name=["\']description["\']',
    re.IGNORECASE,
)
_OG_URL_RE = re.compile(
    r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
_OG_URL_RE_ALT = re.compile(
    r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']og:url["\']',
    re.IGNORECASE,
)
_OG_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']*)["\']',
    re.IGNORECASE,
)
_ANCHOR_RE = re.compile(r"<a\b", re.IGNORECASE)


@dataclass(frozen=True)
class HtmlQualityResult:
    ok: bool
    outcome: str
    reasons: tuple[str, ...] = ()

    @property
    def message(self) -> str:
        if self.ok:
            return "html content looks usable"
        detail = "; ".join(self.reasons) if self.reasons else self.outcome
        return f"{self.outcome}: {detail}"


def _first_group(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    if not match:
        return None
    return unescape(re.sub(r"\s+", " ", match.group(1))).strip()


def _title(html: str) -> str:
    return (_first_group(_TITLE_RE, html) or "").strip()


def _meta_description(html: str) -> str:
    return (
        _first_group(_META_DESC_RE, html)
        or _first_group(_META_DESC_RE_ALT, html)
        or ""
    ).strip()


def _og_url(html: str) -> str:
    return (
        _first_group(_OG_URL_RE, html) or _first_group(_OG_URL_RE_ALT, html) or ""
    ).strip().lower()


def _og_title(html: str) -> str:
    return (_first_group(_OG_TITLE_RE, html) or "").strip()


def _anchor_count(html: str) -> int:
    return len(_ANCHOR_RE.findall(html))


def _radware_signals(html: str, title: str, description: str, og_url: str) -> list[str]:
    signals: list[str] = []
    title_l = title.lower()
    og_title = _og_title(html).lower()
    if "radware page" in title_l:
        signals.append("title_radware_page")
    if "radware page" in og_title:
        signals.append("og_title_radware_page")
    if "radware.com" in og_url:
        signals.append("og_url_radware")
    if description.lower() in {"loader page.", "loader page"}:
        signals.append("meta_loader_page")
    # Observed interstitial has essentially no crawlable anchors.
    if _anchor_count(html) == 0 and signals:
        signals.append("no_anchor_tags")
    return signals


def _cloudflare_signals(html: str, title: str) -> list[str]:
    signals: list[str] = []
    lower = html.lower()
    title_l = title.lower()
    if "just a moment" in title_l:
        signals.append("title_just_a_moment")
    if "attention required" in title_l and "cloudflare" in title_l:
        signals.append("title_cloudflare_attention")
    if "cf-browser-verification" in lower or "cf-challenge" in lower:
        signals.append("cloudflare_challenge_dom")
    if "__cf_chl" in lower or "cdn-cgi/challenge" in lower:
        signals.append("cloudflare_challenge_token")
    if "enable javascript and cookies to continue" in lower and "cloudflare" in lower:
        signals.append("cloudflare_js_required")
    return signals


def _access_denied_signals(html: str, title: str) -> list[str]:
    signals: list[str] = []
    lower = html.lower()
    title_l = title.lower()
    if title_l in {"access denied", "403 forbidden", "403 - forbidden"}:
        signals.append("title_access_denied")
    if "access denied" in title_l and ("403" in title_l or "forbidden" in title_l):
        signals.append("title_access_denied")
    if "you don't have permission to access" in lower:
        signals.append("body_no_permission")
    if "request blocked" in lower and ("aws" in lower or "waf" in lower):
        signals.append("aws_waf_blocked")
    if "errors.edgesuite.net" in lower or "reference #" in lower and "access denied" in lower:
        signals.append("akamai_access_denied")
    return signals


def _captcha_signals(html: str, title: str) -> list[str]:
    signals: list[str] = []
    lower = html.lower()
    title_l = title.lower()
    if "captcha" in title_l and (
        "verify" in title_l or "human" in title_l or "robot" in title_l
    ):
        signals.append("title_captcha_challenge")
    if "g-recaptcha" in lower or "h-captcha" in lower:
        signals.append("captcha_widget_dom")
    if "why do i have to complete a captcha" in lower:
        signals.append("captcha_interstitial_copy")
    return signals


def assess_html_content(body: bytes | str) -> HtmlQualityResult:
    """Return whether HTML is suitable to store as a government document version."""
    if isinstance(body, bytes):
        if not body.strip():
            return HtmlQualityResult(
                ok=False,
                outcome=OUTCOME_INVALID_CONTENT,
                reasons=("empty_body",),
            )
        text = body.decode("utf-8", errors="replace")
    else:
        text = body
        if not text.strip():
            return HtmlQualityResult(
                ok=False,
                outcome=OUTCOME_INVALID_CONTENT,
                reasons=("empty_body",),
            )

    title = _title(text)
    description = _meta_description(text)
    og_url = _og_url(text)

    radware = _radware_signals(text, title, description, og_url)
    # Observed Radware loader: title + og:url alone are decisive.
    if (
        "title_radware_page" in radware and "og_url_radware" in radware
    ) or len(radware) >= 3:
        return HtmlQualityResult(
            ok=False,
            outcome=OUTCOME_BLOCKED_BY_BOT_PROTECTION,
            reasons=tuple(radware),
        )

    cloudflare = _cloudflare_signals(text, title)
    if len(cloudflare) >= 2:
        return HtmlQualityResult(
            ok=False,
            outcome=OUTCOME_BLOCKED_BY_BOT_PROTECTION,
            reasons=tuple(cloudflare),
        )

    denied = _access_denied_signals(text, title)
    if len(denied) >= 2:
        return HtmlQualityResult(
            ok=False,
            outcome=OUTCOME_BLOCKED_BY_BOT_PROTECTION,
            reasons=tuple(denied),
        )

    captcha = _captcha_signals(text, title)
    if len(captcha) >= 2:
        return HtmlQualityResult(
            ok=False,
            outcome=OUTCOME_BLOCKED_BY_BOT_PROTECTION,
            reasons=tuple(captcha),
        )

    # Extremely small HTML with a challenge-like title, no anchors.
    if (
        len(text) < 1500
        and _anchor_count(text) == 0
        and any(
            token in title.lower()
            for token in ("access denied", "forbidden", "just a moment", "captcha")
        )
    ):
        return HtmlQualityResult(
            ok=False,
            outcome=OUTCOME_INVALID_CONTENT,
            reasons=("tiny_challenge_like_html", f"title={title!r}"),
        )

    return HtmlQualityResult(ok=True, outcome=OUTCOME_OK, reasons=())
