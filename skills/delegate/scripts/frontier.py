"""Runnable models, exact AA matches, independent cost frontiers and ask fallback.

Discovery is kept at the edges; build_report and the selection functions take
plain data and never change the hand-maintained catalog.
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_EFFORT_SUFFIX = re.compile(
    r"[-_ ](extra[-_ ]high|non[-_ ]reasoning|none|minimal|low|medium|high|xhigh|max)$", re.I)


def normalize_effort(value):
    token = re.sub(r"[^a-z0-9]", "", (value or "").lower())
    return {"extrahigh": "xhigh", "nonreasoning": "none"}.get(token, token) or None


def split_effort(model):
    """Keep spelling for display; remove only a recognized final effort token."""
    match = _EFFORT_SUFFIX.search(model)
    return (model[:match.start()], normalize_effort(match.group(1))) if match else (model, None)


def family_key(model):
    return re.sub(r"[^a-z0-9]", "", model.lower())


def aa_key(slug, rating):
    family, effort = split_effort(slug)
    paren = re.search(r"\(([^)]*)\)", rating.get("name") or "")
    if paren:
        named = normalize_effort(paren.group(1).split(",")[0])
        if named in EFFORTS:
            effort = named
    return family_key(family), effort


def aa_index(models):
    """An ambiguous normalized key is deliberately unusable without an alias."""
    groups = {}
    for slug, rating in models.items():
        groups.setdefault(aa_key(slug, rating), []).append(slug)
    index, warnings = {}, []
    for key, slugs in sorted(groups.items(), key=lambda item: (item[0][0], item[0][1] or "")):
        if len(slugs) == 1:
            index[key] = slugs[0]
        else:
            warnings.append(f"ambiguous AA key {key[0]}@{key[1] or '(no effort)'}: "
                            + ", ".join(sorted(slugs)) + "; automatic match disabled")
    return index, warnings


def match_aa(agent, model, effort, models, index, aliases):
    key = f"{agent}:{model}" + (f"@{effort}" if agent == "codex" else "")
    if key in aliases:
        slug = aliases[key]
        return slug if slug in models else None
    family, suffix = split_effort(model)
    return index.get((family_key(family), effort if agent == "codex" else suffix))


def parse_cursor_models(text):
    """Only accept `id - Display Name` lines after the CLI's models header."""
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    found, ids = False, []
    for line in text.splitlines():
        if "Available models" in line:
            found = True
            continue
        match = re.match(r"^\s*([A-Za-z0-9_.-]+)\s+-\s+\S", line)
        if found and match and match.group(1) not in ids:
            ids.append(match.group(1))
    return ids


def cursor_models():
    # Use the same environment hygiene as ask/launch under an IDE/AppImage.
    from ask import clean_env
    result = subprocess.run(["cursor-agent", "models"], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=20, check=True,
                            env=clean_env())
    ids = parse_cursor_models(result.stdout)
    if not ids:
        raise RuntimeError("cursor-agent models returned no model ids after Available models")
    return ids


def codex_cache():
    path = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "models_cache.json"
    return json.loads(path.read_text(encoding="utf-8"))


def auto_family(model):
    return model == "auto" or model.startswith(("grok", "cursor-grok", "composer"))


def pool_for(agent, model, creator):
    if agent == "codex":
        return "codex"
    if agent == "claude":
        return "claude"
    if auto_family(model):
        return "cursor-auto"
    if (creator or "").lower() in ("openai", "anthropic"):
        return "cursor-premium"
    return "cursor-premium?"


def _row(agent, model, effort, slug, models):
    rating = models.get(slug) or {}
    row = {"agent": agent, "model": model, "effort": effort, "aa": slug}
    for key in ("intelligence", "cost_per_task", "price_blended", "creator"):
        row[key] = rating.get(key)
    row["pool"] = pool_for(agent, model, row["creator"])
    return row


def runnable_rows(catalog, models, cursor_ids, cache, default_codex):
    index, warnings = aa_index(models)
    aliases = catalog.get("aa_aliases", {})
    for key, slug in sorted(aliases.items()):
        if slug not in models:
            warnings.append(f"AA alias {key} points to missing slug {slug!r}")
    rows, skipped = [], {"cursor_auto": 0, "cursor_fast": 0}
    for model in sorted(set(cursor_ids)):
        if model == "auto":
            skipped["cursor_auto"] += 1
            continue
        if model.endswith("-fast"):
            skipped["cursor_fast"] += 1
            continue
        _, effort = split_effort(model)
        slug = match_aa("cursor", model, effort, models, index, aliases)
        rows.append(_row("cursor", model, effort, slug, models))

    codex = {}
    for model in cache.get("models", []):
        codex[model["slug"]] = {normalize_effort(level["effort"])
                                for level in model.get("supported_reasoning_levels", [])}
        if not codex[model["slug"]]:
            codex[model["slug"]].add(None)
    extras = [(d["model"], d.get("effort")) for d in catalog.get("delegates", [])
              if d["agent"] == "codex" and not d.get("aa_proxy")] + [default_codex]
    configured = {}
    for model, effort in extras:
        configured.setdefault(model, set()).add(normalize_effort(effort))
    for model, configured_efforts in configured.items():
        if model in codex:
            continue
        # Catalog/default models can be newer than the client cache. Include all
        # exact AA efforts, plus their explicitly configured effort if unrated.
        efforts = {aa_key(slug, rating)[1] for slug, rating in models.items()
                   if aa_key(slug, rating)[0] == family_key(split_effort(model)[0])}
        efforts.update(normalize_effort(key.rsplit("@", 1)[1]) for key in aliases
                       if key.startswith(f"codex:{model}@"))
        efforts.update(configured_efforts)
        codex[model] = efforts
    for model, efforts in sorted(codex.items()):
        for effort in sorted(efforts, key=lambda e: (EFFORTS.index(e) if e in EFFORTS else len(EFFORTS), e or "")):
            slug = match_aa("codex", model, effort, models, index, aliases)
            rows.append(_row("codex", model, effort, slug, models))
    for delegate in catalog.get("delegates", []):
        if delegate["agent"] == "claude-subagent" and not delegate.get("aa_proxy"):
            slug = delegate.get("aa")
            effort = aa_key(slug, models[slug])[1] if slug in models else delegate.get("effort")
            rows.append(_row("claude", delegate["model"], effort,
                             slug if slug in models else None, models))
    return rows, warnings, skipped


def dominates(other, row, unit):
    oi, ri, oc, rc = (other.get("intelligence"), row.get("intelligence"),
                      other.get(unit), row.get(unit))
    return (oi is not None and ri is not None and oc is not None and rc is not None
            and oi >= ri and oc <= rc and (oi > ri or oc < rc))


def _cost_order(row, unit):
    return row[unit], -row["intelligence"], row["agent"], row["model"], row["effort"] or ""


def pareto(rows, unit):
    rated = [r for r in rows if r.get("intelligence") is not None and r.get(unit) is not None]
    return sorted((r for r in rated if not any(dominates(o, r, unit) for o in rated)),
                  key=lambda r: _cost_order(r, unit))


def catalog_checks(catalog, models, rows):
    problems = []
    for delegate in catalog.get("delegates", []):
        slug = delegate.get("aa")
        base = {"id": delegate["id"], "aa": slug, "unit": None, "by": None,
                "similar": [], "proxy": bool(delegate.get("aa_proxy"))}
        if slug not in models:
            words = re.findall(r"[a-zA-Z]+", delegate["model"])
            last = words[-1].lower() if words else ""
            hints = sorted(s for s in models if last and last in s.lower())[:5]
            problems.append(dict(base, problem="aa-missing", similar=hints))
            continue
        rating = models[slug]
        unit = "cost_per_task" if rating.get("cost_per_task") is not None else "price_blended"
        better = [row for row in rows if dominates(row, rating, unit)]
        if better:
            problems.append(dict(base, problem="dominated", unit=unit,
                                 by=min(better, key=lambda r: _cost_order(r, unit))))
    return problems


def pick_fallback(rows, model, effort):
    """Return (selection, reason); never substitute a different Codex effort."""
    effort = normalize_effort(effort)
    replaced = next((r for r in rows if r["agent"] == "codex" and r["model"] == model
                     and r["effort"] == effort), None)
    if replaced is None or replaced.get("cost_per_task") is None:
        return None, f"{model}@{effort} has no AA $/task"
    cost = replaced["cost_per_task"]
    cap = 2 * cost
    candidates = [r for r in rows if r["agent"] == "cursor" and not auto_family(r["model"])
                  and not r["model"].endswith("-fast") and r.get("aa")
                  and (r.get("creator") or "").lower() != "anthropic"
                  and r.get("intelligence") is not None and r.get("cost_per_task") is not None
                  and r["cost_per_task"] <= cap]
    if not candidates:
        return None, f"no eligible Cursor model fits ${cap:.3f}/task (2x {model}@{effort} ${cost:.3f}/task)"
    row = min(candidates, key=lambda r: (-r["intelligence"], r["cost_per_task"], r["model"]))
    return {"row": row, "cap": cap,
            "replaces": {"model": model, "effort": effort, "cost_per_task": cost}}, None


def fallback_description(selection):
    row, old = selection["row"], selection["replaces"]
    return (f"cursor {row['model']} (AA {row['intelligence']:.1f}, ${row['cost_per_task']:.3f}/task); "
            f"cap ${selection['cap']:.3f}/task = 2x {old['model']}@{old['effort']} "
            f"${old['cost_per_task']:.3f}/task; bills {row['pool']}")


def build_report(catalog, env, cursor_ids, cache, default_codex):
    rows, warnings, skipped = runnable_rows(catalog, env["models"], cursor_ids, cache, default_codex)
    fallback, reason = pick_fallback(rows, *default_codex)
    not_rated = {"cursor": set(), "codex": set()}
    matched = {"cursor": set(), "codex": set()}
    for row in rows:
        if row["agent"] in not_rated:
            family = split_effort(row["model"])[0] if row["agent"] == "cursor" else row["model"]
            not_rated[row["agent"]].add(family)
            if row["aa"] is not None:
                matched[row["agent"]].add(family)
    return {"source": env["source"], "fetched_at": env["fetched_at"], "runnable": rows,
            "frontiers": {unit: pareto(rows, unit) for unit in ("cost_per_task", "price_blended")},
            "catalog": catalog_checks(catalog, env["models"], rows), "fallback": fallback,
            "fallback_reason": reason, "not_rated": {k: sorted(v - matched[k]) for k, v in not_rated.items()},
            "warnings": warnings, "skipped": skipped,
            "codex_cache_client_version": cache.get("client_version")}


def load_report(catalog, refresh=False, default_codex=None):
    """Best-effort local discovery; AA load still uses its existing stale fallback."""
    import ratings
    from ask import DEFAULTS
    warnings = []
    try:
        ids = cursor_models()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        ids = []
        warnings.append(f"Cursor models unavailable: {exc}")
    try:
        cache = codex_cache()
    except (OSError, ValueError) as exc:
        cache = {}
        warnings.append(f"Codex models cache unavailable: {exc}")
    env = ratings.load(refresh=refresh, log=warnings.append)
    report = build_report(catalog, env, ids, cache, default_codex or DEFAULTS["codex"])
    report["warnings"].extend(warnings)
    return report


def _table(rows, unit):
    body = [("AGENT", "MODEL", "EFFORT", "AA SLUG", "INT", unit, "POOL")]
    for row in rows:
        key = "cost_per_task" if unit == "$/TASK" else "price_blended"
        body.append((row["agent"], row["model"], row["effort"] or "-", row["aa"] or "-",
                     f"{row['intelligence']:.1f}", f"{row[key]:.3f}", row["pool"]))
    widths = [max(len(r[i]) for r in body) for i in range(len(body[0]))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in body)


def render(report, catalog):
    fetched = time.strftime("%Y-%m-%d %H:%M", time.localtime(report["fetched_at"]))
    lines = [f"AA source: {report['source']}, fetched {fetched}; "
             f"Codex cache client_version: {report['codex_cache_client_version'] or 'unknown'}",
             f"Cursor skipped: {report['skipped']['cursor_auto']} auto, "
             f"{report['skipped']['cursor_fast']} -fast priority tiers", "", "Intelligence vs $/task"]
    if not any(r.get("cost_per_task") is not None for r in report["runnable"]):
        lines.append("No runnable AA $/task data in this cache; run `delegate ratings --refresh`.")
    else:
        lines.append(_table(report["frontiers"]["cost_per_task"], "$/TASK"))
    lines.extend(["", "Intelligence vs blended $/1M (3:1 input:output)",
                  _table(report["frontiers"]["price_blended"], "$/1M"), "", "Catalog checks:"])
    problems = {p["id"]: p for p in report["catalog"]}
    for delegate in catalog.get("delegates", []):
        label = delegate["id"] + (" ~ (proxy)" if delegate.get("aa_proxy") else "")
        problem = problems.get(delegate["id"])
        if not problem:
            lines.append(f"  {label}: ok")
        elif problem["problem"] == "aa-missing":
            hints = "; similar: " + ", ".join(problem["similar"]) if problem["similar"] else ""
            lines.append(f"  {label}: AA slug {problem['aa']!r} missing{hints}")
        else:
            row = problem["by"]
            unit = "$/task" if problem["unit"] == "cost_per_task" else "$/1M"
            effort = f"@{row['effort']}" if row["effort"] else ""
            lines.append(f"  {label}: dominated on {unit} by {row['agent']} {row['model']}{effort} "
                         f"(AA {row['intelligence']:.1f}, ${row[problem['unit']]:.3f})")
    for agent, families in report["not_rated"].items():
        lines.append(f"Not rated ({agent}): " + (", ".join(families) or "none"))
    if any(r["pool"] == "cursor-premium?" for r in report["runnable"]):
        lines.append("cursor-premium?: billing pool has not been verified for this model's creator.")
    lines.extend(f"warning: {warning}" for warning in report["warnings"])
    lines.append("ask --agent both if Codex empty: " + (fallback_description(report["fallback"])
                 if report["fallback"] else "keep Codex; " + report["fallback_reason"]))
    return "\n".join(lines)
