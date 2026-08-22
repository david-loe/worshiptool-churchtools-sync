from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


class PushEndpointError(ValueError):
    pass


def _host_allowed(hostname: str, rules: list[str]) -> bool:
    for raw_rule in rules:
        rule = raw_rule.strip().casefold().rstrip(".")
        if not rule:
            continue
        if rule.startswith("."):
            suffix = rule.lstrip(".")
            if hostname == suffix or hostname.endswith(f".{suffix}"):
                return True
        elif hostname == rule:
            return True
    return False


def validate_push_endpoint(endpoint: str, allowed_hosts: list[str]) -> str:
    """Return a canonical public Web-Push URL or reject it before any I/O."""

    value = endpoint.strip()
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise PushEndpointError("invalid_port") from exc
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme.casefold() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or bool(parsed.fragment)
        or not _host_allowed(hostname, allowed_hosts)
    ):
        raise PushEndpointError("endpoint_not_allowed")
    # Host names are case-insensitive and an explicit default port is
    # equivalent. Canonicalizing both prevents duplicate subscriptions.
    return urlunsplit(("https", hostname, parsed.path, parsed.query, ""))
