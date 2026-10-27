"""Artificial Analysis model ratings: intelligence, coding, speed, price.

Uses the official API when ARTIFICIAL_ANALYSIS_API_KEY is set (free key from
artificialanalysis.ai), supplemented with leaderboard task costs by exact slug.
Without a key it reads the public leaderboard page,
whose payload carries the same per-model numbers except an explicit Coding
Index; there `coding` is a proxy: mean of Terminal-Bench 4.0 and SciCode x100.
"""
import json
import os
import re
import time
import urllib.request
from pathlib import Path

CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "claude-delegate" / "aa-models.json"
TTL_S = 24 * 3600
API_URL = "https://artificialanalysis.ai/api/v2/data/llms/models"
PAGE_URL = "https://artificialanalysis.ai/leaderboards/models"
UA = "Mozilla/5.0 (X11; Linux x86_64) claude-delegate/1.0"


def _get(url, headers=None, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def _blend(pin, pout):
    if pin is None or pout is None:
        return None
    return (3 * pin + pout) / 4


def from_api(key):
    data = json.loads(_get(API_URL, {"x-api-key": key}))
    models = {}
    for m in data.get("data", []):
        ev = m.get("evaluations") or {}
        pr = m.get("pricing") or {}
        models[m["slug"]] = {
            "name": m.get("name"),
            "creator": (m.get("model_creator") or {}).get("name"),
            "intelligence": ev.get("artificial_analysis_intelligence_index"),
            "coding": ev.get("artificial_analysis_coding_index"),
            "coding_is_proxy": False,
            "tps": m.get("median_output_tokens_per_second"),
            "price_in": pr.get("price_1m_input_tokens"),
            "price_out": pr.get("price_1m_output_tokens"),
            "price_blended": pr.get("price_1m_blended_3_to_1"),
            "cost_per_task": None,  # No API field has been verified.
            "deprecated": bool(m.get("deprecated")),
        }
    return models


def _flight(html):
    """Concatenate the Next.js RSC payload chunks embedded in the page."""
    parts = []
    for m in re.finditer(r'self\.__next_f\.push\((\[.*?\])\)</script>', html, re.S):
        try:
            arr = json.loads(m.group(1))
        except ValueError:
            continue
        if len(arr) > 1 and isinstance(arr[1], str):
            parts.append(arr[1])
    return "".join(parts)


def _enclosing_object(text, i):
    depth = 0
    for j in range(i, -1, -1):
        c = text[j]
        if c == "}":
            depth += 1
        elif c == "{":
            if depth == 0:
                return j
            depth -= 1
    return None


def from_page():
    flight = _flight(_get(PAGE_URL))
    dec = json.JSONDecoder()
    models, pos = {}, 0
    while True:
        i = flight.find('"medianOutputTokensPerSecond"', pos)
        if i < 0:
            break
        pos = i + 1
        j = _enclosing_object(flight, i)
        if j is None:
            continue
        try:
            o, _ = dec.raw_decode(flight, j)
        except ValueError:
            continue
        slug = o.get("slug")
        if not slug or "intelligenceIndex" not in o or slug in models:
            continue
        # Per-benchmark scores moved into a nested `scores` dict (seen 2026-10-27).
        sc = o.get("scores") or {}
        coding_parts = [v for v in (sc.get("terminalbench-4-0", o.get("terminalBench40")),
                                    sc.get("scicode", o.get("scicode"))) if v is not None]
        models[slug] = {
            "name": o.get("name"),
            "creator": o.get("modelCreatorName"),
            "intelligence": o.get("intelligenceIndex"),
            "coding": 100 * sum(coding_parts) / len(coding_parts) if coding_parts else None,
            "coding_is_proxy": True,
            "tps": o.get("medianOutputTokensPerSecond"),
            "price_in": o.get("price1mInputTokens"),
            "price_out": o.get("price1mOutputTokens"),
            "price_blended": _blend(o.get("price1mInputTokens"), o.get("price1mOutputTokens")),
            "cost_per_task": o.get("intelligenceIndexCostPerTask"),
            "deprecated": bool(o.get("deprecated")),
        }
    if len(models) < 20:
        raise RuntimeError(f"leaderboard page parsed to only {len(models)} models; page layout changed?")
    if not any(m["coding"] is not None for m in models.values()):
        raise RuntimeError("leaderboard page has no Terminal-Bench 4.0 or SciCode scores; page layout changed?")
    return models


def load(refresh=False, log=lambda msg: None):
    """Return the cache envelope, refreshing it when older than TTL."""
    cached = None
    if CACHE.exists():
        try:
            cached = json.loads(CACHE.read_text())
        except ValueError:
            cached = None
    if cached and cached.get("schema") == 2 and not refresh and time.time() - cached.get("fetched_at", 0) < TTL_S:
        return cached
    key = os.environ.get("ARTIFICIAL_ANALYSIS_API_KEY")
    try:
        if key:
            models, source = from_api(key), "api"
            try:
                page_models = from_page()
            except Exception as exc:  # Task costs are optional; retain the API refresh.
                log(f"warning: AA leaderboard task costs unavailable ({exc}); "
                    "keeping API models without $/task")
            else:
                for slug, model in models.items():
                    model["cost_per_task"] = page_models.get(slug, {}).get("cost_per_task")
        else:
            models, source = from_page(), "leaderboard-page"
    except Exception as exc:  # network, layout change, auth
        if cached:
            log(f"warning: AA refresh failed ({exc}); using cache from "
                f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(cached['fetched_at']))}")
            return cached
        raise
    env = {"schema": 2, "fetched_at": time.time(), "source": source, "models": models}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(env, indent=1))
    return env
