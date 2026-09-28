from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from mcp_router.config import RouterConfig, load_config, parse_config


VALID_CONFIG = {
    "name": "my-router",
    "instructions": "unified server",
    "backends": {
        "notes": {"command": "python3", "args": ["examples/notes_server.py"]},
        "calculator": {"command": "python3", "args": ["examples/calculator_server.py"], "env": {"FOO": "bar"}},
    },
}


def test_parse_valid_config_dict():
    config = parse_config(VALID_CONFIG)
    assert isinstance(config, RouterConfig)
    assert config.name == "my-router"
    assert set(config.backends) == {"notes", "calculator"}
    assert config.backends["calculator"].env == {"FOO": "bar"}


def test_load_valid_json_config_from_disk(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps(VALID_CONFIG))
    config = load_config(p)
    assert config.name == "my-router"
    assert config.backends["notes"].command == "python3"


def test_load_example_config_shipped_with_the_package():
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "examples" / "router_config.json"
    config = load_config(example)
    assert set(config.backends) == {"notes", "calculator"}


def test_default_name_and_empty_backends():
    config = parse_config({})
    assert config.name == "mcp-router"
    assert config.backends == {}


@pytest.mark.parametrize(
    "bad_config",
    [
        {"backends": {"notes": {"args": ["x"]}}},  # missing required 'command'
        {"backends": {"": {"command": "python3"}}},  # empty backend name
        {"backends": {"has.dot": {"command": "python3"}}},  # reserved separator
        {"backends": {"router": {"command": "python3"}}},  # reserved meta name
        {"backends": {"notes": {"command": ""}}},  # blank command
        {"unexpected_field": True},  # extra="forbid"
    ],
)
def test_invalid_configs_are_rejected(bad_config):
    with pytest.raises(ValidationError):
        parse_config(bad_config)


def test_load_config_with_malformed_json_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json")
    with pytest.raises(json.JSONDecodeError):
        load_config(p)
