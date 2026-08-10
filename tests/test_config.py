from pathlib import Path

import pytest
import yaml

from hermes_skill_publisher.config import ConfigError, load_config, require_classification_policy


def write_config(env, entry, *, external=None):
    config = env["config"]
    config["plugins"]["entries"]["hermes-skill-publisher"] = entry
    if external is not None:
        config["skills"]["external_dirs"] = external
    (env["hermes"] / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def test_loads_exact_config(isolated_home):
    config = load_config()
    assert config.shared_root == isolated_home["shared"]
    assert config.adapter_roots == {"other": isolated_home["adapter"]}
    assert config.plugin_enabled


def test_defaults_are_strict(isolated_home):
    default_shared = isolated_home["home"] / ".agents" / "skills"
    write_config(isolated_home, {}, external=[str(default_shared)])
    config = load_config()
    assert config.shared_root == default_shared
    assert not config.require_classification
    assert config.adapter_roots == {}


@pytest.mark.parametrize("entry, message", [
    ({"require_classification": "yes"}, "boolean"),
    ({"shared_root": ""}, "non-empty"),
    ({"adapter_roots": []}, "mapping"),
    ({"adapter_roots": {"x": 2}}, "path string"),
])
def test_wrong_types_block(isolated_home, entry, message):
    write_config(isolated_home, entry)
    with pytest.raises(ConfigError, match=message):
        load_config(validate_roots=False, require_authorized=False)


def test_relative_paths_resolve_from_hermes_home(isolated_home):
    relative = isolated_home["hermes"] / "shared"
    relative.mkdir()
    write_config(isolated_home, {"shared_root": "shared", "adapter_roots": {}}, external=["shared"])
    assert load_config().shared_root == relative


def test_symlink_root_rejected(isolated_home, tmp_path):
    link = tmp_path / "shared-link"
    link.symlink_to(isolated_home["shared"], target_is_directory=True)
    write_config(isolated_home, {"shared_root": str(link), "adapter_roots": {}}, external=[str(link)])
    with pytest.raises(ConfigError, match="symlink|resolve"):
        load_config()


def test_overlap_rejected(isolated_home):
    nested = isolated_home["shared"] / "adapter"
    nested.mkdir()
    write_config(isolated_home, {"shared_root": str(isolated_home["shared"]), "adapter_roots": {"x": str(nested)}})
    with pytest.raises(ConfigError, match="overlaps"):
        load_config()


def test_external_dir_authorization_is_exact(isolated_home, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    write_config(isolated_home, {"shared_root": str(isolated_home["shared"]), "adapter_roots": {}}, external=[str(other)])
    with pytest.raises(ConfigError, match="skills.external_dirs"):
        load_config()


def test_unknown_keys_are_warnings_not_values(isolated_home):
    write_config(isolated_home, {"shared_root": str(isolated_home["shared"]), "adapter_roots": {}, "future": True})
    assert load_config().unknown_keys == ("future",)


def test_policy_reader_fails_closed(isolated_home):
    write_config(isolated_home, {"require_classification": "false"})
    with pytest.raises(ConfigError):
        require_classification_policy()
