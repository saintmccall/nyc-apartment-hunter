from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


def load_config(path: str | Path = "config.yaml") -> dict[str, Any]:
    with open(path) as f:
        cfg = yaml.safe_load(f)

    # Allow .env / shell environment to override sensitive config values
    # so credentials never need to live in config.yaml
    email = cfg.setdefault("notifications", {}).setdefault("email", {})
    _env_override(email, "address",      "HUNTER_EMAIL_ADDRESS")
    _env_override(email, "app_password", "HUNTER_EMAIL_APP_PASSWORD")

    scoring = cfg.setdefault("scoring", {})
    _env_override(scoring, "model", "HUNTER_SCORING_MODEL")

    if key := os.environ.get("ANTHROPIC_API_KEY"):
        os.environ.setdefault("ANTHROPIC_API_KEY", key)  # already set; no-op

    return cfg


def _env_override(section: dict, key: str, env_var: str) -> None:
    if value := os.environ.get(env_var):
        section[key] = value
