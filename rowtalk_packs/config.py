"""Loads config/packs.json — the ONE file holding every content decision."""

import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(REPO_ROOT, "config", "packs.json")


def load_config(path=None):
    with open(path or CONFIG_PATH, "r", encoding="utf-8") as handle:
        cfg = json.load(handle)
    if cfg.get("config_version") != 1:
        raise ValueError("unsupported config_version %r" % cfg.get("config_version"))
    for lang, lang_cfg in cfg["wiki"]["languages"].items():
        tiers = lang_cfg["tiers"]
        # Extended is a superset of Essential (I4): its target must be larger.
        if "essential" in tiers and "extended" in tiers and tiers["essential"] > tiers["extended"]:
            raise ValueError("%s: essential tier larger than extended" % lang)
    return cfg
