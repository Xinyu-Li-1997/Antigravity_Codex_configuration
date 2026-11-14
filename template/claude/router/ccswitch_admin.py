#!/usr/bin/env python3
"""ccswitch_admin.py - Pure stdlib CC Switch provider card, live projection, and prompt guard CLI.

Subcommands:
  * status          Read-only inspection of cc-switch.db, Claude settings.json, and prompt conflicts
  * prepare-cards   Atomically create or update Kimi, DeepSeek, and hybrid router cards (preserving keys)
  * switch-card     Safely project a target Claude card into settings.json using CC Switch floor/residue rules
  * disable-prompts Disable enabled prompts in cc-switch.db so stale rules never overwrite AGENTS.md/CLAUDE.md
  * verify          Validate card alignment, effort settings, prompt safety, and optional router health
"""
from __future__ import annotations

import argparse
import copy
from contextlib import closing
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MIN_SUPPORTED_SCHEMA = 23
TESTED_MAX_SCHEMA = 26

KIMI_PLACEHOLDER = "PASTE_KIMI_API_KEY_HERE"
DEEPSEEK_PLACEHOLDER = "PASTE_DEEPSEEK_API_KEY_HERE"

FLOOR_PREFIXES = ("ANTHROPIC_", "AWS_", "VERTEX_REGION_")
FLOOR_ENV_KEYS = {
    "CLAUDE_CODE_SUBAGENT_MODEL", "CLOUD_ML_REGION",
    "GOOGLE_APPLICATION_CREDENTIALS", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
    "CLAUDE_CODE_OAUTH_SCOPES", "CLAUDE_CODE_API_KEY_HELPER_TTL_MS",
}
PROTOCOL_SELECTORS = {
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_GATEWAY", "CLAUDE_CODE_USE_MANTLE", "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD",
}
EXCLUSIVE = {
    "CLAUDE_CODE_SUBAGENT_MODEL_FORCE",
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS", "CLAUDE_CODE_DISABLE_ARTIFACT", "ENABLE_TOOL_SEARCH",
    "CLAUDE_CODE_DISABLE_THINKING", "DISABLE_INTERLEAVED_THINKING", "CLAUDE_CODE_ALWAYS_ENABLE_EFFORT",
    "CLAUDE_CODE_EXTRA_BODY", "CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING", "CLAUDE_CODE_AUTO_MODE_SERVER",
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS", "CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    "CLAUDE_CODE_DISABLE_1M_CONTEXT", "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT",
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
}
FLOOR_TOP = {
    "apiKeyHelper", "apiBaseUrl", "primaryModel", "smallFastModel", "apiKey", "model", "fallbackModel",
    "modelOverrides", "modelPicker", "advisorModel", "awsAuthRefresh", "awsCredentialExport", "gcpAuthRefresh",
}
RESIDUE_ENV = {
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS": ("262144", "372000", "983616"),
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": ("262144", "372000", "1000000"),
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": ("131072",),
}


def default_router_port() -> int:
    return 8240 if os.name == "nt" else 8241


def floor_env(key: str) -> bool:
    return (
        key.startswith(FLOOR_PREFIXES)
        or key in FLOOR_ENV_KEYS
        or key in PROTOCOL_SELECTORS
        or (key.startswith("CLAUDE_CODE_SKIP_") and key.endswith("_AUTH"))
    )


def mask_token_state(raw_token: Any) -> str:
    if not isinstance(raw_token, str) or not raw_token.strip():
        return "missing"
    stripped = raw_token.strip()
    if stripped.lower() == "via-router":
        return "via-router"
    if stripped.upper().startswith("PASTE_"):
        return "placeholder"
    return "present"


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".claude-switch-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    atomic_write_bytes(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def backup_sqlite_database(db_path: Path, backup_path: Path) -> None:
    fd = os.open(str(backup_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)) as source:
            with closing(sqlite3.connect(str(backup_path), timeout=15)) as target:
                source.backup(target)
    except BaseException:
        if backup_path.exists():
            backup_path.unlink()
        raise


def _check_schema(con: sqlite3.Connection) -> Tuple[int, List[str]]:
    row = con.execute("PRAGMA user_version").fetchone()
    version = int(row[0]) if row else 0
    warnings: List[str] = []
    if version < MIN_SUPPORTED_SCHEMA:
        raise ValueError(f"Unsupported CC Switch schema {version} (< {MIN_SUPPORTED_SCHEMA})")
    if version > TESTED_MAX_SCHEMA:
        warnings.append(
            f"CC Switch schema {version} is newer than tested baseline {TESTED_MAX_SCHEMA}; verify column compatibility."
        )
    return version, warnings


def _table_exists(con: sqlite3.Connection, table_name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _build_card_templates(
    port: int,
    kimi_model: str = "k3.1-pro[1m]",
    deepseek_model: str = "deepseek-flash",
    max_context: str = "1048576",
    compact_window: str = "1000000",
) -> Dict[str, Tuple[Any, Optional[str], str]]:
    ds_base = deepseek_model.replace("[1m]", "").replace("[1M]", "")
    ds_main = ds_base if ds_base.endswith(("[1m]", "[1M]")) else f"{ds_base}[1M]"

    def kimi_env(token: str) -> Dict[str, str]:
        return {
            "ANTHROPIC_AUTH_TOKEN": token,
            "ANTHROPIC_BASE_URL": "https://api.kimi.com/coding/",
            "ANTHROPIC_DEFAULT_FABLE_MODEL": kimi_model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": kimi_model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": kimi_model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": kimi_model,
            "ANTHROPIC_MODEL": kimi_model,
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": compact_window,
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": max_context,
            "CLAUDE_CODE_SUBAGENT_MODEL": kimi_model,
            "ENABLE_TOOL_SEARCH": "true",
        }

    def deepseek_env(token: str) -> Dict[str, str]:
        return {
            "ANTHROPIC_AUTH_TOKEN": token,
            "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
            "ANTHROPIC_DEFAULT_FABLE_MODEL": ds_base,
            "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME": ds_base,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": ds_base,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME": ds_base,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": ds_base,
            "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME": ds_base,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": ds_base,
            "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME": ds_base,
            "ANTHROPIC_MODEL": ds_main,
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": compact_window,
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": max_context,
            "CLAUDE_CODE_SUBAGENT_MODEL": ds_base,
            "ENABLE_TOOL_SEARCH": "true",
        }

    def router_env() -> Dict[str, str]:
        return {
            "ANTHROPIC_AUTH_TOKEN": "via-router",
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
            "ANTHROPIC_DEFAULT_FABLE_MODEL": kimi_model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": kimi_model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": kimi_model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": ds_base,
            "ANTHROPIC_MODEL": kimi_model,
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": compact_window,
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": max_context,
            "CLAUDE_CODE_SUBAGENT_MODEL": ds_base,
            "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1",
            "ENABLE_TOOL_SEARCH": "true",
        }

    return {
        "Kimi": (kimi_env, KIMI_PLACEHOLDER, f"Kimi ({kimi_model}) direct connection."),
        "K3.1主+DS子代理": (
            router_env,
            None,
            f"Kimi ({kimi_model}) main + DeepSeek ({ds_base}) subagent via local router 127.0.0.1:{port}.",
        ),
        "DeepSeek": (deepseek_env, DEEPSEEK_PLACEHOLDER, f"DeepSeek ({ds_main}) direct connection."),
    }


def project_claude_settings(
    original: Dict[str, Any],
    previous: Dict[str, Any],
    target: Dict[str, Any],
) -> Dict[str, Any]:
    for name, config in (("settings", original), ("previous card", previous), ("target card", target)):
        if not isinstance(config, dict) or not isinstance(config.get("env", {}), dict):
            raise ValueError(f"{name} and its env must be JSON objects")
    target = copy.deepcopy(target)
    target_env = target.setdefault("env", {})
    bedrock = target_env.get("CLAUDE_CODE_USE_BEDROCK")
    if (
        bedrock is True
        or bedrock == 1
        or (isinstance(bedrock, str) and bedrock.strip().lower() in ("1", "true"))
    ) and "apiKey" in target:
        target_env.setdefault("AWS_BEARER_TOKEN_BEDROCK", target.pop("apiKey"))
    updated = copy.deepcopy(original)
    for key in FLOOR_TOP:
        updated.pop(key, None)
    for key, value in target.items():
        if key != "env" and key in FLOOR_TOP:
            updated[key] = copy.deepcopy(value)
    env = updated.setdefault("env", {})
    for key in list(env):
        if floor_env(key):
            env.pop(key, None)
    previous_env = previous.get("env", {})
    for key, value in previous_env.items():
        if key in EXCLUSIVE and env.get(key) == value:
            env.pop(key, None)
    for key, values in RESIDUE_ENV.items():
        if env.get(key) in (*values, *(int(value) for value in values)):
            env.pop(key, None)
    for key, value in target.get("env", {}).items():
        if floor_env(key) or key in EXCLUSIVE:
            env[key] = copy.deepcopy(value)
    return updated


def inspect_status(
    db_path: Path,
    settings_path: Optional[Path] = None,
    router_port: Optional[int] = None,
) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "db_path": str(db_path),
        "db_exists": db_path.is_file(),
        "schema_version": None,
        "cards": [],
        "current_claude_card": None,
        "enabled_prompts": [],
        "settings_exists": bool(settings_path and settings_path.is_file()),
        "settings_aligned": None,
        "effort_level": None,
        "locked_effort_env": False,
        "router_health": None,
        "warnings": [],
    }
    active_card_token: Optional[str] = None
    claude_current_count = 0
    if db_path.is_file():
        with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as con:
            schema_ver, warnings = _check_schema(con)
            report["schema_version"] = schema_ver
            report["warnings"].extend(warnings)
            if _table_exists(con, "providers"):
                rows = con.execute(
                    "SELECT id, app_type, name, settings_config, is_current FROM providers ORDER BY app_type, sort_index, name"
                ).fetchall()
                for row_id, app_type, name, raw_cfg, is_current in rows:
                    cfg_obj = {}
                    try:
                        cfg_obj = json.loads(raw_cfg) if raw_cfg else {}
                    except ValueError:
                        report["warnings"].append(f"Invalid JSON in provider card {app_type}:{name}")
                    env = cfg_obj.get("env") if isinstance(cfg_obj, dict) and isinstance(cfg_obj.get("env"), dict) else {}
                    token_raw = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
                    entry = {
                        "id": row_id,
                        "app_type": app_type,
                        "name": name,
                        "current": bool(is_current),
                        "base_url": env.get("ANTHROPIC_BASE_URL"),
                        "model": env.get("ANTHROPIC_MODEL"),
                        "subagent_model": env.get("CLAUDE_CODE_SUBAGENT_MODEL"),
                        "max_context": env.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS"),
                        "token": mask_token_state(token_raw) if app_type == "claude" else "n/a",
                    }
                    report["cards"].append(entry)
                    if app_type == "claude" and is_current:
                        claude_current_count += 1
                        report["current_claude_card"] = entry
                        active_card_token = token_raw if isinstance(token_raw, str) else None
                if claude_current_count > 1:
                    report["multiple_current_claude_cards"] = True
                    report["warnings"].append(f"Multiple Claude provider cards ({claude_current_count}) have is_current=1.")
            if _table_exists(con, "prompts"):
                prompt_cols = {r[1] for r in con.execute("PRAGMA table_info(prompts)").fetchall()}
                if {"id", "app_type", "name", "enabled"}.issubset(prompt_cols):
                    p_rows = con.execute(
                        "SELECT id, app_type, name FROM prompts WHERE enabled != 0 ORDER BY app_type, id"
                    ).fetchall()
                    report["enabled_prompts"] = [
                        {"id": r[0], "app_type": r[1], "name": r[2]} for r in p_rows
                    ]
                    if report["enabled_prompts"]:
                        report["warnings"].append(
                            "Enabled prompts exist in cc-switch.db and may overwrite AGENTS.md or CLAUDE.md if triggered."
                        )

    if settings_path and settings_path.is_file():
        settings_obj = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        env_obj = settings_obj.get("env") if isinstance(settings_obj.get("env"), dict) else {}
        report["effort_level"] = settings_obj.get("effortLevel")
        report["locked_effort_env"] = "CLAUDE_CODE_EFFORT_LEVEL" in env_obj
        if report["locked_effort_env"]:
            report["warnings"].append("CLAUDE_CODE_EFFORT_LEVEL is set in settings.json env, locking interactive /effort.")
        cur = report["current_claude_card"]
        if claude_current_count > 1:
            report["settings_aligned"] = False
        elif cur:
            settings_token = env_obj.get("ANTHROPIC_AUTH_TOKEN") or env_obj.get("ANTHROPIC_API_KEY")
            report["settings_aligned"] = (
                env_obj.get("ANTHROPIC_BASE_URL") == cur.get("base_url")
                and env_obj.get("ANTHROPIC_MODEL") == cur.get("model")
                and env_obj.get("CLAUDE_CODE_SUBAGENT_MODEL") == cur.get("subagent_model")
                and settings_token == active_card_token
            )
            if not report["settings_aligned"]:
                report["warnings"].append("Claude settings.json env differs from current active CC Switch card.")

    if router_port is not None:
        url = f"http://127.0.0.1:{router_port}/__router/health"
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                report["router_health"] = {
                    "ok": bool(resp.status == 200 and payload.get("ok") is True),
                    "port": payload.get("port"),
                }
        except (OSError, ValueError, urllib.error.URLError) as exc:
            report["router_health"] = {"ok": False, "port": router_port, "error": type(exc).__name__}

    return report


def prepare_cards(
    db_path: Path,
    port: int,
    kimi_model: str = "k3.1-pro[1m]",
    deepseek_model: str = "deepseek-flash",
    max_context: str = "1048576",
    compact_window: str = "1000000",
    dry_run: bool = False,
) -> Dict[str, Any]:
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    if not db_path.is_file():
        raise FileNotFoundError(f"Database not found: {db_path}")

    cards_spec = _build_card_templates(port, kimi_model, deepseek_model, max_context, compact_window)
    timestamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns()}"
    backup_path = Path(f"{db_path}.bak-before-cards-{timestamp}")

    mode_flag = "ro" if dry_run else "rw"
    con = sqlite3.connect(db_path.resolve().as_uri() + f"?mode={mode_flag}", uri=True, timeout=15)
    try:
        if not dry_run:
            con.execute("BEGIN IMMEDIATE")
        schema_ver, warnings = _check_schema(con)
        if not _table_exists(con, "providers"):
            raise ValueError("providers table missing in cc-switch.db")
        if not dry_run:
            backup_sqlite_database(db_path, backup_path)

        columns = [row[1] for row in con.execute("PRAGMA table_info(providers)").fetchall()]
        official = con.execute(
            "SELECT * FROM providers WHERE app_type='claude' AND id='claude-official'"
        ).fetchone()
        template = dict(zip(columns, official)) if official else {}
        next_sort = int(
            con.execute("SELECT COALESCE(MAX(sort_index), 0) FROM providers WHERE app_type='claude'").fetchone()[0]
        )

        results: List[Dict[str, Any]] = []
        for name, (env_factory, placeholder, notes) in cards_spec.items():
            rows = con.execute(
                "SELECT * FROM providers WHERE app_type='claude' AND name=?",
                (name,),
            ).fetchall()
            if len(rows) > 1:
                raise ValueError(f"Duplicate Claude card: {name}")
            current = dict(zip(columns, rows[0])) if rows else None
            token = placeholder
            if current:
                cfg = json.loads(current["settings_config"])
                if not isinstance(cfg, dict) or not isinstance(cfg.get("env", {}), dict):
                    raise ValueError(f"Card config and env must be objects: {name}")
                old_env = cfg.get("env", {})
                for key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
                    old_token = old_env.get(key)
                    if (
                        placeholder
                        and isinstance(old_token, str)
                        and old_token.strip()
                        and not old_token.strip().upper().startswith("PASTE_")
                        and old_token.strip().lower() != "via-router"
                    ):
                        token = old_token.strip()
                        break
            env = env_factory(token) if placeholder else env_factory()
            record = dict(current if current else template)
            record.update(
                {
                    "id": current["id"] if current else str(uuid.uuid4()),
                    "app_type": "claude",
                    "name": name,
                    "settings_config": json.dumps({"env": env}, ensure_ascii=False),
                    "website_url": None,
                    "category": "third_party",
                    "created_at": current["created_at"] if current else int(time.time() * 1000),
                    "notes": notes,
                    "icon": None,
                    "icon_color": None,
                    "meta": "{}",
                    "is_current": current["is_current"] if current else 0,
                    "in_failover_queue": 0,
                    "cost_multiplier": record.get("cost_multiplier") or "1.0",
                    "limit_daily_usd": None,
                    "limit_monthly_usd": None,
                    "provider_type": None,
                }
            )
            if not dry_run:
                if current:
                    update_pairs = [
                        ("settings_config", record["settings_config"]),
                        ("notes", notes),
                        ("category", "third_party"),
                        ("meta", "{}"),
                        ("in_failover_queue", 0),
                    ]
                    valid_pairs = [(col, val) for col, val in update_pairs if col in columns]
                    set_clause = ", ".join(f'"{col}"=?' for col, _ in valid_pairs)
                    con.execute(
                        f"UPDATE providers SET {set_clause} WHERE id=? AND app_type='claude'",
                        [val for _, val in valid_pairs] + [record["id"]],
                    )
                else:
                    next_sort += 1
                    record["sort_index"] = next_sort
                    for col in columns:
                        if col not in record or record[col] is None:
                            if col in (
                                "website_url", "category", "created_at", "sort_index", "notes",
                                "icon", "icon_color", "limit_daily_usd", "limit_monthly_usd", "provider_type",
                            ):
                                continue
                            if col == "meta":
                                record[col] = "{}"
                            elif col in ("is_current", "in_failover_queue"):
                                record[col] = 0
                            elif col == "cost_multiplier":
                                record[col] = "1.0"
                    insert_cols = [col for col in columns if col in record]
                    placeholders_sql = ",".join("?" for _ in insert_cols)
                    cols_sql = ",".join('"' + col.replace('"', '""') + '"' for col in insert_cols)
                    con.execute(
                        f"INSERT INTO providers ({cols_sql}) VALUES ({placeholders_sql})",
                        [record[col] for col in insert_cols],
                    )
            results.append(
                {
                    "name": name,
                    "action": "update" if current else "insert",
                    "current": bool(record["is_current"]),
                    "base_url": env.get("ANTHROPIC_BASE_URL"),
                    "model": env.get("ANTHROPIC_MODEL"),
                    "subagent": env.get("CLAUDE_CODE_SUBAGENT_MODEL"),
                    "context": env.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS"),
                    "token": mask_token_state(env.get("ANTHROPIC_AUTH_TOKEN")),
                }
            )
        if not dry_run:
            con.commit()
        return {
            "dry_run": dry_run,
            "schema_version": schema_ver,
            "backup": None if dry_run else str(backup_path),
            "cards": results,
            "warnings": warnings,
        }
    except BaseException:
        if not dry_run:
            con.rollback()
        raise
    finally:
        con.close()


def switch_card(
    db_path: Path,
    settings_path: Path,
    card_name: str,
    dry_run: bool = False,
) -> Dict[str, Any]:
    if not db_path.is_file():
        raise FileNotFoundError(f"Database not found: {db_path}")
    if not settings_path.is_file():
        raise FileNotFoundError(f"Settings file not found: {settings_path}")
    state_path = db_path.with_name("live-state.json")
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8-sig")).get("apps", {}).get("claude", {})
        if state.get("mode") == "proxy" or state.get("attached") or state.get("pending") is not None:
            raise RuntimeError("Claude must be in direct mode with no pending CC Switch operation")

    mode_flag = "ro" if dry_run else "rw"
    con = sqlite3.connect(db_path.resolve().as_uri() + f"?mode={mode_flag}", uri=True, timeout=15)
    try:
        if not dry_run:
            con.execute("BEGIN IMMEDIATE")
        schema_ver, warnings = _check_schema(con)
        rows = con.execute(
            "SELECT id, name, settings_config, is_current FROM providers WHERE app_type='claude'"
        ).fetchall()
        targets = [r for r in rows if r[1] == card_name]
        current = [r for r in rows if r[3]]
        if len(targets) != 1 or len(current) > 1:
            raise ValueError(f"Expected exactly one target card ({card_name!r}) and at most one current Claude card")
        target_id, _, raw_target, _ = targets[0]
        previous = json.loads(current[0][2]) if current else {}
        original_bytes = settings_path.read_bytes()
        updated = project_claude_settings(
            json.loads(original_bytes.decode("utf-8-sig")),
            previous,
            json.loads(raw_target),
        )
        if dry_run:
            return {
                "dry_run": True,
                "schema_version": schema_ver,
                "card": card_name,
                "previous": current[0][1] if current else None,
                "model": updated.get("env", {}).get("ANTHROPIC_MODEL"),
                "subagent": updated.get("env", {}).get("CLAUDE_CODE_SUBAGENT_MODEL"),
                "warnings": warnings,
            }
        timestamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns()}"
        db_backup = Path(f"{db_path}.bak-before-live-switch-{timestamp}")
        settings_backup = Path(f"{settings_path}.bak-before-live-switch-{timestamp}")
        backup_sqlite_database(db_path, db_backup)
        shutil.copy2(settings_path, settings_backup)
        if os.name == "posix":
            os.chmod(settings_backup, 0o600)
        published = False
        try:
            con.execute("UPDATE providers SET is_current=0 WHERE app_type='claude'")
            changed = con.execute(
                "UPDATE providers SET is_current=1 WHERE app_type='claude' AND id=?",
                (target_id,),
            )
            if changed.rowcount != 1:
                raise RuntimeError("Target card disappeared during switch")
            atomic_write_json(settings_path, updated)
            published = True
            con.commit()
        except BaseException:
            try:
                con.rollback()
            finally:
                if published:
                    atomic_write_bytes(settings_path, original_bytes)
            raise
        return {
            "dry_run": False,
            "schema_version": schema_ver,
            "card": card_name,
            "previous": current[0][1] if current else None,
            "model": updated.get("env", {}).get("ANTHROPIC_MODEL"),
            "subagent": updated.get("env", {}).get("CLAUDE_CODE_SUBAGENT_MODEL"),
            "resident_effort": updated.get("env", {}).get("CLAUDE_CODE_EFFORT_LEVEL"),
            "db_backup": str(db_backup),
            "settings_backup": str(settings_backup),
            "warnings": warnings,
        }
    finally:
        con.close()


def disable_prompts(
    db_path: Path,
    app_types: Optional[List[str]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    if not db_path.is_file():
        return {"db_exists": False, "dry_run": dry_run, "disabled": [], "backup": None, "warnings": []}

    mode_flag = "ro" if dry_run else "rw"
    con = sqlite3.connect(db_path.resolve().as_uri() + f"?mode={mode_flag}", uri=True, timeout=15)
    try:
        if not dry_run:
            con.execute("BEGIN IMMEDIATE")
        schema_ver, warnings = _check_schema(con)
        if not _table_exists(con, "prompts"):
            if not dry_run:
                con.rollback()
            return {
                "db_exists": True,
                "schema_version": schema_ver,
                "dry_run": dry_run,
                "disabled": [],
                "backup": None,
                "warnings": warnings,
            }
        if app_types:
            placeholders = ",".join("?" for _ in app_types)
            rows = con.execute(
                f"SELECT id, app_type, name FROM prompts WHERE enabled != 0 AND app_type IN ({placeholders}) ORDER BY app_type, id",
                list(app_types),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT id, app_type, name FROM prompts WHERE enabled != 0 ORDER BY app_type, id"
            ).fetchall()
        disabled = [{"id": r[0], "app_type": r[1], "name": r[2]} for r in rows]
        if not disabled or dry_run:
            if not dry_run:
                con.rollback()
            return {
                "db_exists": True,
                "schema_version": schema_ver,
                "dry_run": dry_run,
                "disabled": disabled,
                "backup": None,
                "warnings": warnings,
            }
        timestamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns()}"
        backup_path = Path(f"{db_path}.bak-before-disable-prompts-{timestamp}")
        backup_sqlite_database(db_path, backup_path)
        if app_types:
            placeholders = ",".join("?" for _ in app_types)
            con.execute(
                f"UPDATE prompts SET enabled = 0 WHERE enabled != 0 AND app_type IN ({placeholders})",
                list(app_types),
            )
        else:
            con.execute("UPDATE prompts SET enabled = 0 WHERE enabled != 0")
        con.commit()
        return {
            "db_exists": True,
            "schema_version": schema_ver,
            "dry_run": False,
            "disabled": disabled,
            "backup": str(backup_path),
            "warnings": warnings,
        }
    except BaseException:
        if not dry_run:
            con.rollback()
        raise
    finally:
        con.close()


def verify_alignment(
    db_path: Path,
    settings_path: Path,
    router_port: Optional[int] = None,
) -> Tuple[bool, Dict[str, Any]]:
    status = inspect_status(db_path, settings_path, router_port=router_port)
    errors: List[str] = []
    if not status["db_exists"]:
        errors.append("cc-switch.db does not exist")
    if not status["settings_exists"]:
        errors.append("Claude settings.json does not exist")
    if status["enabled_prompts"]:
        errors.append(f"cc-switch.db still has {len(status['enabled_prompts'])} enabled prompt(s)")
    if status["locked_effort_env"]:
        errors.append("CLAUDE_CODE_EFFORT_LEVEL is locked in settings.json env")
    if status["db_exists"] and status["current_claude_card"] is None:
        errors.append("cc-switch.db has no active Claude provider card (is_current=1)")
    if status.get("multiple_current_claude_cards"):
        errors.append("cc-switch.db has multiple active Claude provider cards (is_current=1)")
    if status["settings_aligned"] is not True and status["db_exists"] and status["settings_exists"]:
        errors.append("Claude settings.json env does not match current CC Switch card")
    if router_port is not None and not (status.get("router_health") or {}).get("ok"):
        errors.append(f"Local router on 127.0.0.1:{router_port} failed health check")
    status["ok"] = len(errors) == 0
    status["errors"] = errors
    return status["ok"], status


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CC Switch & Claude Code card/prompt management CLI")
    parser.add_argument("--db", default="~/.cc-switch/cc-switch.db", help="Path to cc-switch.db")
    parser.add_argument("--settings", default="~/.claude/settings.json", help="Path to Claude settings.json")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="Inspect CC Switch DB, Claude settings, and prompt status")
    p_status.add_argument("--check-router-port", type=int, default=None)

    p_prep = sub.add_parser("prepare-cards", help="Create or update Kimi, DeepSeek, and hybrid router cards")
    p_prep.add_argument("--port", type=int, default=default_router_port())
    p_prep.add_argument("--kimi-model", default="k3.1-pro[1m]")
    p_prep.add_argument("--deepseek-model", default="deepseek-flash")
    p_prep.add_argument("--max-context", default="1048576")
    p_prep.add_argument("--compact-window", default="1000000")
    p_prep.add_argument("--dry-run", action="store_true")

    p_switch = sub.add_parser("switch-card", help="Switch active Claude card and project into settings.json")
    p_switch.add_argument("card", help="Target Claude card name")
    p_switch.add_argument("--dry-run", action="store_true")

    p_prompts = sub.add_parser("disable-prompts", help="Disable enabled prompts in cc-switch.db")
    p_prompts.add_argument(
        "--app-type",
        action="append",
        choices=("claude", "codex", "agy", "opencode", "gemini"),
        help="Optional app_type filter (repeatable; default disables all enabled prompts)",
    )
    p_prompts.add_argument("--dry-run", action="store_true")

    p_verify = sub.add_parser("verify", help="Verify alignment and prompt safety")
    p_verify.add_argument("--check-router-port", type=int, default=None)

    args = parser.parse_args(argv)
    db_path = Path(args.db).expanduser()
    settings_path = Path(args.settings).expanduser()

    if args.cmd == "status":
        out = inspect_status(db_path, settings_path, router_port=args.check_router_port)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "prepare-cards":
        out = prepare_cards(
            db_path,
            port=args.port,
            kimi_model=args.kimi_model,
            deepseek_model=args.deepseek_model,
            max_context=args.max_context,
            compact_window=args.compact_window,
            dry_run=args.dry_run,
        )
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "switch-card":
        out = switch_card(db_path, settings_path, args.card, dry_run=args.dry_run)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "disable-prompts":
        out = disable_prompts(db_path, app_types=args.app_type, dry_run=args.dry_run)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "verify":
        ok, out = verify_alignment(db_path, settings_path, router_port=args.check_router_port)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
