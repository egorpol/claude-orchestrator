"""Artificial Analysis ratings from its documented, keyed free API only.

A refresh follows pagination (currently four requests) and is cached for 24h.
The free API allows 100 requests per fixed 24-hour window. Coding Index scores
are no longer published; keep the existing model shape with coding set to None.
"""
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "claude-delegate" / "aa-models.json"
SCHEMA = 3
TTL_S = 24 * 3600
API_URL = "https://artificialanalysis.ai/api/v2/language/models/free"
# Bound even a faulty server's has_more loop to a small part of the daily quota.
MAX_PAGES = 8
UA = "claude-delegate/1.0"
ATTRIBUTION = "Source: Artificial Analysis (https://artificialanalysis.ai/)."


def _tps(m):
    """Output tok/s: the median while AA serves `performance` (null from 2026-12-10), then the v3 mean."""
    for field, key in (("performance", "median_output_tokens_per_second"),
                       ("performance_v3", "mean_output_tokens_per_second")):
        value = (m.get(field) or {}).get(key)
        if value is not None:
            return value
    return None


class RatingsError(RuntimeError):
    """An actionable ratings failure safe to display without credentials."""


class MissingAPIKey(RatingsError):
    """No key and no usable cache; callers may choose quota-only ranking."""


def key_file():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "claude-orchestrator" / "aa-api-key"


def missing_key_message():
    return f"AA ratings need ARTIFICIAL_ANALYSIS_API_KEY or a key in {key_file()}."


def _key():
    key = os.environ.get("ARTIFICIAL_ANALYSIS_API_KEY", "").strip()
    if key:
        return key
    try:
        return key_file().read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError):
        raise RatingsError(f"Cannot read AA API key file: {key_file()}") from None


def _get(url, headers=None, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    # Unredirected: urllib copies ordinary headers, the key included, to any redirect target.
    for name, value in (headers or {}).items():
        req.add_unredirected_header(name, value)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def _blend(pin, pout):
    if pin is None or pout is None:
        return None
    return (3 * pin + pout) / 4


def from_api(key):
    models = {}
    for page in range(1, MAX_PAGES + 1):
        data = json.loads(_get(f"{API_URL}?page={page}", {"x-api-key": key}))
        pagination = data["pagination"]
        if not isinstance(data["data"], list) or not isinstance(pagination["has_more"], bool):
            raise RatingsError("Invalid AA free API pagination or model data")
        for m in data["data"]:
            ev = m.get("evaluations") or {}
            pr = m.get("pricing") or {}
            cost = m.get("artificial_analysis_intelligence_index_cost") or {}
            models[m["slug"]] = {
                "name": m.get("name"),
                "creator": (m.get("model_creator") or {}).get("name"),
                "intelligence": ev.get("artificial_analysis_intelligence_index"),
                "coding": None,
                "coding_is_proxy": False,
                "tps": _tps(m),
                "price_in": pr.get("price_1m_input_tokens"),
                "price_out": pr.get("price_1m_output_tokens"),
                "price_blended": _blend(pr.get("price_1m_input_tokens"), pr.get("price_1m_output_tokens")),
                "cost_per_task": (cost.get("cost_per_task") or {}).get("total_cost"),
                "deprecated": False,
            }
        if not pagination["has_more"]:
            return models
    raise RatingsError("AA free API pagination exceeded the page limit")


def _failure_reason(exc):
    # Exception text and response bodies may echo the request's secret header.
    # Report only a numeric HTTP status, this module's own fixed texts or a fixed
    # description, never str(exc) of anything else.
    if isinstance(exc, RatingsError):
        return str(exc)
    if isinstance(exc, urllib.error.HTTPError) and isinstance(exc.code, int):
        return f"HTTP {exc.code}"
    return "request, response or pagination error"


def load(refresh=False, log=lambda msg: None):
    """Return a schema-3 envelope, with same-schema stale fallback on failure."""
    cached = None
    try:
        candidate = json.loads(CACHE.read_text(encoding="utf-8"))
        if (isinstance(candidate, dict) and candidate.get("schema") == SCHEMA
                and isinstance(candidate.get("models"), dict)
                and isinstance(candidate.get("fetched_at"), (int, float))):
            cached = candidate
    except (OSError, ValueError):
        pass
    if cached and not refresh and time.time() - cached["fetched_at"] < TTL_S:
        return cached
    try:
        key = _key()
        if not key:
            raise MissingAPIKey(missing_key_message())
        models = from_api(key)
    except Exception as exc:  # Includes 429: keep a usable cache, never retry.
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()
        message = str(exc) if isinstance(exc, MissingAPIKey) else f"AA refresh failed ({_failure_reason(exc)})"
        if cached:
            log(f"warning: {message}; using cache from "
                f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(cached['fetched_at']))}")
            return cached
        if isinstance(exc, MissingAPIKey):
            raise
        raise RatingsError(message) from None
    env = {"schema": SCHEMA, "fetched_at": time.time(), "source": "api", "models": models}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(env, indent=1), encoding="utf-8")
    return env
