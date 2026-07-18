"""Config back-compat for the 9.7 default-folder fields."""

from __future__ import annotations

import json

from core import config as C


def test_old_config_without_the_new_fields_loads_defaults(tmp_path):
    """A settings file written before 9.7 lacks the folder fields; load() fills
    them with defaults rather than crashing (the flat-schema contract)."""
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"output_dir": "X:/rips"}), encoding="utf-8")

    cfg = C.load(path)

    assert cfg.output_dir == "X:/rips"                 # preserved
    assert cfg.default_source_dir == ""                # new field -> default
    assert cfg.default_output_dir == ""
    assert cfg.source_post_album_policy == "keep"
    assert cfg.output_post_album_policy == "keep"


def test_the_new_fields_round_trip_through_save_and_load(tmp_path):
    path = tmp_path / "settings.json"
    cfg = C.Config(default_source_dir="A:/src", default_output_dir="B:/out",
                   source_post_album_policy="clear", output_post_album_policy="reset")
    C.save(cfg, path)

    back = C.load(path)
    assert back.default_source_dir == "A:/src"
    assert back.default_output_dir == "B:/out"
    assert back.source_post_album_policy == "clear"
    assert back.output_post_album_policy == "reset"
