"""URL normalization, validation, and safe scope matching for the source registry."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


class InvalidRegistryUrlError(ValueError):
    """Raised when a seed URL or allowlist pattern is unsafe or malformed."""


def _split_url(url: str):
    raw = url.strip()
    if not raw:
        raise InvalidRegistryUrlError("URL is empty")
    return raw, urlsplit(raw)


def normalize_url(url: str) -> str:
    """Return a stable normalized URL for uniqueness checks.

    Keeps the original seed URL separately in ``sources.seed_url``.
    Normalization rules:
    - strip whitespace
    - lowercase scheme and host
    - drop fragments
    - drop default ports (80/443)
    - drop trailing slash on non-root paths
    - preserve query string as-is (seeds should not rely on tracking params)
    - reject embedded credentials
    """
    raw, parts = _split_url(url)
    if not parts.scheme or not parts.netloc:
        raise InvalidRegistryUrlError(f"Invalid URL (missing scheme or host): {url!r}")
    if parts.username is not None or parts.password is not None:
        raise InvalidRegistryUrlError(
            f"URL must not contain embedded credentials: {url!r}"
        )

    scheme = parts.scheme.lower()
    host = parts.hostname.lower() if parts.hostname else ""
    if not host:
        raise InvalidRegistryUrlError(f"Invalid URL (empty host): {url!r}")
    if "." not in host and host != "localhost":
        # Reject clearly malformed / non-DNS hosts for registry seeds.
        raise InvalidRegistryUrlError(f"Invalid URL host: {host!r}")

    port = parts.port
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"
    else:
        netloc = host

    path = parts.path or ""
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    return urlunsplit((scheme, netloc, path, parts.query, ""))


def host_of(url: str) -> str | None:
    """Return the lowercase hostname for an absolute URL, if present."""
    hostname = urlsplit(url.strip()).hostname
    return hostname.lower() if hostname else None


def path_prefix_pattern(seed_url: str) -> str:
    """Build a narrow allowlist pattern from a seed URL (legacy compatible).

    Stored format remains ``{normalized_url}*`` meaning: exact path or
    genuine path descendants under that URL. Matching must use
    ``url_matches_allowed_pattern``, not naive string prefix checks.
    """
    return f"{normalize_url(seed_url)}*"


@dataclass(frozen=True)
class ParsedAllowedPattern:
    """Structured form of a stored allowlist pattern."""

    scheme: str
    host: str
    path: str  # normalized path without trailing slash (except root "/")
    recursive: bool
    original: str


def parse_allowed_pattern(pattern: str) -> ParsedAllowedPattern:
    """Parse a stored pattern such as ``https://host/path*``."""
    raw = pattern.strip()
    if not raw:
        raise InvalidRegistryUrlError("Allowlist pattern is empty")

    recursive = raw.endswith("*")
    base = raw[:-1] if recursive else raw
    if not base:
        raise InvalidRegistryUrlError(f"Invalid allowlist pattern: {pattern!r}")

    normalized = normalize_url(base)
    parts = urlsplit(normalized)
    if parts.scheme not in {"http", "https"}:
        raise InvalidRegistryUrlError(
            f"Allowlist pattern must use http or https: {pattern!r}"
        )
    if parts.query:
        raise InvalidRegistryUrlError(
            f"Allowlist pattern must not include a query string: {pattern!r}"
        )
    path = parts.path or "/"
    return ParsedAllowedPattern(
        scheme=parts.scheme,
        host=parts.hostname or "",
        path=path,
        recursive=recursive,
        original=raw,
    )


def _normalize_path(path: str) -> str:
    if not path:
        return "/"
    if path != "/" and path.endswith("/"):
        return path.rstrip("/")
    return path


def path_is_self_or_descendant(candidate_path: str, allowed_path: str) -> bool:
    """True if candidate is exactly allowed_path or a path-segment descendant."""
    candidate = _normalize_path(candidate_path)
    allowed = _normalize_path(allowed_path)
    if candidate == allowed:
        return True
    # Prevent `/work` from matching `/work-evil` via naive prefixing.
    prefix = allowed if allowed.endswith("/") else f"{allowed}/"
    return candidate.startswith(prefix)


def url_matches_allowed_pattern(url: str, pattern: str) -> bool:
    """Return True if url is inside the allowlist pattern scope.

    Requires:
    - exact scheme match
    - exact hostname match (no silent www aliasing)
    - path equal to the pattern path, or a genuine descendant when pattern
      ends with ``*``
    """
    parsed = parse_allowed_pattern(pattern)
    try:
        normalized = normalize_url(url)
    except InvalidRegistryUrlError:
        return False

    parts = urlsplit(normalized)
    if parts.scheme != parsed.scheme:
        return False
    if (parts.hostname or "") != parsed.host:
        return False

    candidate_path = _normalize_path(parts.path or "/")
    if parsed.recursive:
        return path_is_self_or_descendant(candidate_path, parsed.path)
    return candidate_path == parsed.path


def url_in_any_allowed_pattern(url: str, patterns: list[str] | tuple[str, ...]) -> bool:
    """True if url matches at least one allowlist pattern."""
    return any(url_matches_allowed_pattern(url, pattern) for pattern in patterns)


def validate_http_url(
    url: str,
    *,
    require_https: bool = False,
    allow_http: bool = True,
) -> str:
    """Validate and normalize a registry URL. Returns normalized form."""
    normalized = normalize_url(url)
    parts = urlsplit(normalized)
    scheme = parts.scheme
    if scheme not in {"http", "https"}:
        raise InvalidRegistryUrlError(
            f"Unsupported URL scheme {scheme!r}; only http/https are allowed"
        )
    if require_https and scheme != "https":
        raise InvalidRegistryUrlError(
            f"HTTPS is required for approved official seeds: {url!r}"
        )
    if scheme == "http" and not allow_http:
        raise InvalidRegistryUrlError(f"HTTP URLs are not allowed: {url!r}")
    return normalized


def validate_seed_and_patterns(
    seed_url: str,
    patterns: list[str] | tuple[str, ...],
    *,
    require_https: bool = True,
) -> tuple[str, list[str]]:
    """Validate seed URL and allowlist patterns; return normalized seed + patterns."""
    normalized_seed = validate_http_url(
        seed_url, require_https=require_https, allow_http=not require_https
    )
    seed_parts = urlsplit(normalized_seed)
    seed_host = seed_parts.hostname or ""
    seed_scheme = seed_parts.scheme

    validated: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        parsed = parse_allowed_pattern(pattern)
        if parsed.scheme != seed_scheme:
            raise InvalidRegistryUrlError(
                f"Pattern scheme must match seed scheme ({seed_scheme}): {pattern!r}"
            )
        if parsed.host != seed_host:
            raise InvalidRegistryUrlError(
                f"Pattern host must match seed host ({seed_host}): {pattern!r}"
            )
        if require_https and parsed.scheme != "https":
            raise InvalidRegistryUrlError(
                f"HTTPS is required for approved seed patterns: {pattern!r}"
            )
        # Pattern path must be the seed path or a descendant (never wider).
        seed_path = _normalize_path(seed_parts.path or "/")
        if parsed.path != seed_path and not path_is_self_or_descendant(
            parsed.path, seed_path
        ):
            raise InvalidRegistryUrlError(
                f"Pattern path is out of scope for seed {normalized_seed!r}: "
                f"{pattern!r}"
            )
        # Re-store in canonical legacy-compatible form.
        base = urlunsplit((parsed.scheme, parsed.host, parsed.path, "", ""))
        if parsed.path != "/" and base.endswith("/"):
            base = base.rstrip("/")
        canonical = f"{base}*" if parsed.recursive else base
        if canonical not in seen:
            seen.add(canonical)
            validated.append(canonical)

    if not validated:
        raise InvalidRegistryUrlError("At least one allowlist pattern is required")
    return normalized_seed, validated
