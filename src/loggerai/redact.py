"""
Masks secrets before anything leaves the app: by key name (fields, headers,
query parameters) and by shape (bearer tokens and JWTs anywhere in a string).
The same rules as LoggerAI's server, browser script and other SDKs.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MASK = "[REDACTED]"

# A key is sensitive when one of its words is one of these
# ("accessToken" -> access, token; "X-CSRF-Token" -> x, csrf, token).
_WORDS = {
    "password", "passwd", "pwd", "pass", "passphrase", "secret", "token", "otp", "pin", "cvv", "cvc",
    "ssn", "cookie", "authorization", "auth", "signature", "sig", "credential", "credentials", "bvn", "nin",
}
# ...or when its words run together into one of these ("x-api-key" -> xapikey).
_JOINED = (
    "apikey", "accesskey", "privatekey", "secretkey", "cardnumber", "clientsecret", "sessionid",
    "authtoken", "accesstoken", "refreshtoken", "idtoken", "csrftoken", "xsrf", "setcookie",
)
# Query parameters that carry credentials in URLs (e.g. Google's ?key=).
_URL_EXTRA = {"key", "code", "access_token", "id_token"}

_SPLIT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_BEARER = re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9\-._~+/]{12,}=*")
_URLISH = re.compile(r"^https?://", re.I)


def is_sensitive(key: Any) -> bool:
    if not isinstance(key, str) or not key:
        return False
    words = [w.lower() for w in _SPLIT.split(key) if w]
    if any(w in _WORDS for w in words):
        return True
    joined = "".join(words)
    return any(j in joined for j in _JOINED)


def redact_string(s: str) -> str:
    s = _JWT.sub("[REDACTED_JWT]", s)
    return _BEARER.sub(lambda m: f"{m.group(1)} {MASK}", s)


def redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return redact_string(url)
    if not parts.query:
        return redact_string(url)
    q = [(k, MASK if (is_sensitive(k) or k.lower() in _URL_EXTRA) and v else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    netloc = parts.netloc
    if "@" in netloc:  # user:password@host
        netloc = MASK + "@" + netloc.rsplit("@", 1)[1]
    return redact_string(urlunsplit((parts.scheme, netloc, parts.path, urlencode(q, safe="[]/:,"), parts.fragment)))


def _redact_text(s: str) -> str:
    """A string that may itself be JSON, a form body or a URL."""
    t = s.strip()
    if t[:1] in "{[":
        try:
            return json.dumps(redact(json.loads(t)), ensure_ascii=False)
        except (ValueError, RecursionError):
            pass
    if _URLISH.match(t):
        return redact_url(t)
    if "=" in t and " " not in t and len(t) < 20000:
        pairs = parse_qsl(t, keep_blank_values=True)
        if pairs and any(is_sensitive(k) for k, _ in pairs):
            return urlencode([(k, MASK if is_sensitive(k) and v else v) for k, v in pairs], safe="[]")
    return redact_string(s)


def redact(value: Any, _depth: int = 0) -> Any:
    """A copy of value with secrets masked. Safe on any JSON-like data."""
    if _depth > 40:
        return value
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if is_sensitive(k) and v not in (None, "", [], {}):
                out[k] = MASK
            elif isinstance(k, str) and k.lower() in ("url", "href", "referrer", "referer", "location") and isinstance(v, str):
                out[k] = redact_url(v)
            else:
                out[k] = redact(v, _depth + 1)
        return out
    if isinstance(value, list):
        return [redact(v, _depth + 1) for v in value]
    if isinstance(value, str):
        return _redact_text(value) if value else value
    return value
