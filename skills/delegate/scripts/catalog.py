"""Load the delegate catalog, optionally merged with a personal overlay.

Personal tuning lives outside the public checkout (see local_path()). Merge
follows JSON Merge Patch (RFC 7396) with a delegates-by-id exception.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

# Fields a local delegate may set even when no base delegate carries them.
_EXTRA_DELEGATE_FIELDS = frozenset({"prior", "aa_proxy", "remove"})
_WARN_PREFIX = "delegate: catalog.local.json:"
_warnings_emitted = False


class CatalogError(Exception):
    """Raised when the base or local catalog cannot be used; message names the file."""


def local_path():
    """Return the path of the personal overlay, or None when local overrides are off."""
    if "DELEGATE_CATALOG_LOCAL" in os.environ:
        val = os.environ["DELEGATE_CATALOG_LOCAL"]
        if val == "":
            return None
        return Path(val)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if not xdg:
        xdg = str(Path.home() / ".config")
    return Path(xdg) / "claude-orchestrator" / "catalog.local.json"


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _delegate_known_fields(base_delegates):
    known = set(_EXTRA_DELEGATE_FIELDS)
    for d in base_delegates or []:
        if isinstance(d, dict):
            known.update(d.keys())
    return known


def _merge_object(base_obj, local_obj, prefix, overrides):
    """RFC 7396 object merge; append override paths; return a new dict.

    An object patch always merges recursively. When the target is missing or not
    an object, the merge starts from {}.
    """
    out = copy.deepcopy(base_obj) if isinstance(base_obj, dict) else {}
    if not isinstance(local_obj, dict):
        path = prefix or "<root>"
        overrides.append(path)
        return copy.deepcopy(local_obj)
    for key, value in local_obj.items():
        path = f"{prefix}.{key}" if prefix else key
        if value is None:
            out.pop(key, None)
            overrides.append(path)
        elif isinstance(value, dict):
            target = out[key] if isinstance(out.get(key), dict) else {}
            out[key] = _merge_object(target, value, path, overrides)
        else:
            # Scalar or array: replace.
            out[key] = copy.deepcopy(value)
            overrides.append(path)
    return out


def _merge_delegates(base_list, local_list, overrides, warnings, known_fields):
    if not isinstance(local_list, list):
        raise CatalogError("delegates must be a list of objects")
    by_id = {}
    order = []
    for d in base_list or []:
        if not isinstance(d, dict) or not isinstance(d.get("id"), str):
            continue
        did = d["id"]
        by_id[did] = copy.deepcopy(d)
        order.append(did)

    seen_local = set()
    new_ids = []
    for entry in local_list:
        if not isinstance(entry, dict):
            raise CatalogError("delegates entries must be objects")
        did = entry.get("id")
        if not isinstance(did, str) or not did:
            raise CatalogError("delegates entry missing string id")
        if did in seen_local:
            raise CatalogError(f"duplicate local delegate id {did!r}")
        seen_local.add(did)

        if "remove" in entry:
            if entry["remove"] is True:
                if did not in by_id:
                    raise CatalogError(f"cannot remove unknown delegate {did!r}")
                del by_id[did]
                order = [x for x in order if x != did]
                overrides.append(f"delegates[-{did}]")
                continue
            if entry["remove"] is not False:
                raise CatalogError(
                    f"delegates[{did}].remove must be a boolean")
            # remove: false is a no-op control; never copied into the delegate.

        # Control key `remove` and `id` are never merged as fields.
        fields = {k: v for k, v in entry.items() if k not in ("id", "remove")}
        unknown = [k for k in fields if k not in known_fields]
        for k in unknown:
            warnings.append(f"unknown delegate field {k!r} on {did}")

        if did in by_id:
            field_overrides = []
            merged = _merge_object(by_id[did], fields,
                                   f"delegates[{did}]", field_overrides)
            overrides.extend(field_overrides)
            by_id[did] = merged
        else:
            if "agent" not in fields or "model" not in fields:
                raise CatalogError(
                    f"new delegate {did!r} must carry agent and model")
            by_id[did] = copy.deepcopy(fields)
            by_id[did]["id"] = did
            new_ids.append(did)
            overrides.append(f"delegates[+{did}]")

    result = [by_id[i] for i in order if i in by_id]
    result.extend(by_id[i] for i in new_ids)
    return result


def merge(base, local):
    """Merge local over base. Returns (merged, overrides, warnings); never mutates inputs."""
    if not isinstance(base, dict):
        raise CatalogError("base catalog must be a JSON object")
    if not isinstance(local, dict):
        raise CatalogError("local catalog must be a JSON object")

    warnings = []
    overrides = []
    base_keys = set(base.keys())
    known_fields = _delegate_known_fields(base.get("delegates"))

    # Work on a deep copy so we never mutate base.
    out = copy.deepcopy(base)

    for key, value in local.items():
        if key.startswith("_"):
            continue
        if key not in base_keys:
            # New tasks under tasks are allowed (and tasks may be introduced if
            # absent from a stub base). Any other unknown top-level key is a typo:
            # warn and skip, so it never becomes an override.
            if key != "tasks":
                warnings.append(f"unknown top-level key {key!r}")
                continue

        if key == "delegates":
            if value is None:
                out.pop("delegates", None)
                overrides.append("delegates")
            else:
                out["delegates"] = _merge_delegates(
                    base.get("delegates") or [], value, overrides, warnings, known_fields)
            continue

        if value is None:
            out.pop(key, None)
            overrides.append(key)
        elif isinstance(value, dict):
            target = out[key] if isinstance(out.get(key), dict) else {}
            out[key] = _merge_object(target, value, key, overrides)
        else:
            out[key] = copy.deepcopy(value)
            overrides.append(key)

    # prefer task names not in merged tasks
    prefer = out.get("prefer")
    tasks = out.get("tasks") if isinstance(out.get("tasks"), dict) else {}
    if isinstance(prefer, dict):
        for task_name in prefer:
            if task_name not in tasks:
                warnings.append(f"prefer task {task_name!r} is not in tasks")

    return out, overrides, warnings


def validate(catalog):
    """Raise CatalogError if the merged catalog is not usable."""
    if not isinstance(catalog, dict):
        raise CatalogError("catalog must be a JSON object")

    delegates = catalog.get("delegates", _MISSING)
    if delegates is _MISSING or not isinstance(delegates, list):
        raise CatalogError("delegates must be a list of objects")
    seen = set()
    for d in delegates:
        if not isinstance(d, dict):
            raise CatalogError("delegates entries must be objects")
        did = d.get("id")
        if not isinstance(did, str) or not did:
            raise CatalogError("delegate missing string id")
        if did in seen:
            raise CatalogError(f"duplicate delegate id {did!r}")
        seen.add(did)
        if not isinstance(d.get("agent"), str) or not d.get("agent"):
            raise CatalogError(f"delegate {did!r} missing string agent")
        if not isinstance(d.get("model"), str) or not d.get("model"):
            raise CatalogError(f"delegate {did!r} missing string model")

    tasks = catalog.get("tasks", _MISSING)
    if tasks is _MISSING or not isinstance(tasks, dict):
        raise CatalogError("tasks must be an object of objects")
    for name, task in tasks.items():
        if not isinstance(task, dict):
            raise CatalogError(f"task {name!r} must be an object")
        weights = task.get("weights")
        if not isinstance(weights, dict):
            raise CatalogError(f"task {name!r} must have a weights object")

    if "min_quota_pct" in catalog and not _is_number(catalog["min_quota_pct"]):
        raise CatalogError("min_quota_pct must be a number")
    if "orchestrator_pool_penalty" in catalog and not _is_number(
            catalog["orchestrator_pool_penalty"]):
        raise CatalogError("orchestrator_pool_penalty must be a number")


_MISSING = object()


def _emit_warnings(warnings):
    global _warnings_emitted
    if not warnings or _warnings_emitted:
        return
    _warnings_emitted = True
    for w in warnings:
        print(f"{_WARN_PREFIX} {w}", file=sys.stderr)


def _read_object(path, label):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise CatalogError(f"{path}: invalid UTF-8 ({exc})") from exc
    except OSError as exc:
        raise CatalogError(f"{path}: cannot read ({exc})") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CatalogError(f"{path}: invalid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise CatalogError(f"{path}: must be a JSON object")
    return data


def _require_local_file(lp):
    """Return True if lp is absent (normal). Raise CatalogError if present but unusable."""
    if lp is None:
        return False
    if not lp.exists():
        return False
    if not lp.is_file():
        raise CatalogError(f"{lp}: not a readable file")
    return True


def load(base_path=None):
    """Return the catalog dict, merged with the personal overlay when present.

    A missing local file is normal: the base catalog alone is returned and no
    message is printed. A path that exists but is not a readable JSON file
    (directory, unreadable, invalid UTF-8/JSON) raises CatalogError. Warnings
    from a successful merge are printed once per process.
    """
    base_path = Path(base_path) if base_path is not None else Path(__file__).with_name("catalog.json")
    base = _read_object(base_path, "base")
    lp = local_path()
    if not _require_local_file(lp):
        return base
    local = _read_object(lp, "local")
    try:
        merged, _overrides, warnings = merge(base, local)
        validate(merged)
    except CatalogError as exc:
        msg = str(exc)
        if msg.startswith(str(lp)):
            raise
        raise CatalogError(f"{lp}: {msg}") from None
    _emit_warnings(warnings)
    return merged


def load_details(base_path=None):
    """Return (merged, info) where info has local, status, overrides, warnings.

    status is 'off' (env empty), 'missing' (path unused), or 'loaded'.
    """
    base_path = Path(base_path) if base_path is not None else Path(__file__).with_name("catalog.json")
    base = _read_object(base_path, "base")
    info = {"local": None, "status": "missing", "overrides": [], "warnings": [],
            "would_read": None}
    if "DELEGATE_CATALOG_LOCAL" in os.environ and os.environ["DELEGATE_CATALOG_LOCAL"] == "":
        info["status"] = "off"
        info["would_read"] = str(
            Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
            / "claude-orchestrator" / "catalog.local.json")
        return base, info
    lp = local_path()
    info["local"] = str(lp) if lp is not None else None
    info["would_read"] = str(lp) if lp is not None else None
    if lp is None:
        info["status"] = "off"
        return base, info
    if not lp.exists():
        info["status"] = "missing"
        return base, info
    if not lp.is_file():
        raise CatalogError(f"{lp}: not a readable file")
    local = _read_object(lp, "local")
    try:
        merged, overrides, warnings = merge(base, local)
        validate(merged)
    except CatalogError as exc:
        msg = str(exc)
        if not msg.startswith(str(lp)):
            raise CatalogError(f"{lp}: {msg}") from None
        raise
    info["status"] = "loaded"
    info["overrides"] = overrides
    info["warnings"] = warnings
    _emit_warnings(warnings)
    return merged, info
