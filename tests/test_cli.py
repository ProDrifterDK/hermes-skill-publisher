import argparse
import json

from hermes_skill_publisher import plugin
from hermes_skill_publisher.cli import handle_cli, setup_cli


def parse(*parts):
    parser = argparse.ArgumentParser()
    setup_cli(parser)
    return parser.parse_args(parts)


def test_status_json_exit_zero(isolated_home, capsys):
    code = handle_cli(parse("status", "--json"))
    value = json.loads(capsys.readouterr().out)
    assert code == 0 and value["ok"]


def test_doctor_missing_middleware_exit_two(isolated_home, capsys, monkeypatch):
    monkeypatch.setattr(plugin, "_MIDDLEWARE_AVAILABLE", False)
    code = handle_cli(parse("doctor", "--json"))
    value = json.loads(capsys.readouterr().out)
    assert code == 2 and not value["ok"]
    assert "middleware_unavailable" in " ".join(value["errors"])


def test_doctor_ready_exit_zero(isolated_home, capsys, monkeypatch):
    monkeypatch.setattr(plugin, "_MIDDLEWARE_AVAILABLE", True)
    code = handle_cli(parse("doctor", "--json"))
    value = json.loads(capsys.readouterr().out)
    assert code == 0 and value["ok"]


def test_doctor_unknown_hidden_artifact_exit_two(isolated_home, capsys, monkeypatch):
    monkeypatch.setattr(plugin, "_MIDDLEWARE_AVAILABLE", True)
    hidden = isolated_home["shared"] / ".hermes-skill-publisher-stage-unknown"
    hidden.mkdir()
    code = handle_cli(parse("doctor", "--json"))
    value = json.loads(capsys.readouterr().out)
    assert code == 2 and any("unknown hidden" in error for error in value["errors"])


def test_mutation_missing_middleware_exit_two(isolated_home, capsys, monkeypatch):
    monkeypatch.setattr(plugin, "_MIDDLEWARE_AVAILABLE", False)
    code = handle_cli(parse("publish", "demo-skill", "--json"))
    assert code == 2
    assert "middleware_unavailable" in capsys.readouterr().out


def test_unexpected_failure_exit_one(isolated_home, capsys, monkeypatch):
    import hermes_skill_publisher.cli as cli
    monkeypatch.setattr(cli, "status_snapshot", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    code = handle_cli(parse("status", "--json"))
    assert code == 1
    assert "skill_publisher.status_failed:RuntimeError" in capsys.readouterr().out


def test_audit_limit_validation_exit_two(isolated_home, capsys):
    code = handle_cli(parse("audit", "--limit", "-1", "--json"))
    assert code == 2
    assert not json.loads(capsys.readouterr().out)["ok"]


def test_audit_limit_zero_returns_no_events(isolated_home, capsys):
    from hermes_skill_publisher.state import audit
    audit("one", result="success")
    audit("two", result="success")
    code = handle_cli(parse("audit", "--limit", "0", "--json"))
    value = json.loads(capsys.readouterr().out)
    assert code == 0 and value["ok"] and value["events"] == []


def test_doctor_write_approval_is_a_readiness_blocker(isolated_home, capsys, monkeypatch):
    monkeypatch.setattr(plugin, "_MIDDLEWARE_AVAILABLE", True)
    config = isolated_home["config"]
    config["skills"]["write_approval"] = True
    (isolated_home["hermes"] / "config.yaml").write_text(__import__("yaml").safe_dump(config))
    code = handle_cli(parse("doctor", "--json"))
    value = json.loads(capsys.readouterr().out)
    assert code == 2 and not value["ok"]
    assert any("write_approval" in error for error in value["errors"])


def test_write_approval_blocks_manual_publication_mutations(isolated_home, make_skill, capsys, monkeypatch):
    from hermes_skill_publisher.publisher import promote
    monkeypatch.setattr(plugin, "_MIDDLEWARE_AVAILABLE", True)
    source = make_skill(isolated_home["local"])
    config = isolated_home["config"]
    config["skills"]["write_approval"] = True
    (isolated_home["hermes"] / "config.yaml").write_text(__import__("yaml").safe_dump(config))
    assert handle_cli(parse("publish", "demo-skill", "--json")) == 2
    assert source.is_dir() and not (isolated_home["shared"] / "demo-skill").exists()
    capsys.readouterr()

    config["skills"]["write_approval"] = False
    (isolated_home["hermes"] / "config.yaml").write_text(__import__("yaml").safe_dump(config))
    promote(source)
    config["skills"]["write_approval"] = True
    (isolated_home["hermes"] / "config.yaml").write_text(__import__("yaml").safe_dump(config))
    assert handle_cli(parse("unpublish", "demo-skill", "--scope", "local", "--json")) == 2
    assert (isolated_home["shared"] / "demo-skill").is_dir()


def test_doctor_malformed_yaml_does_not_leak_snippet(isolated_home, capsys, monkeypatch):
    monkeypatch.setattr(plugin, "_MIDDLEWARE_AVAILABLE", True)
    sentinel = "DO_NOT_LOG_THIS_CONFIG_SECRET"
    (isolated_home["hermes"] / "config.yaml").write_text(f'skills:\n  leak: "{sentinel}\n')
    assert handle_cli(parse("doctor", "--json")) == 2
    assert sentinel not in capsys.readouterr().out


def test_doctor_malformed_registry_record_exit_two(isolated_home, capsys, monkeypatch):
    from hermes_skill_publisher.state import SCHEMA_VERSION, save_registry
    monkeypatch.setattr(plugin, "_MIDDLEWARE_AVAILABLE", True)
    record = {
        "canonical_path": str(isolated_home["shared"] / "demo-skill"),
        "source_relpath": "../escape/demo-skill",
        "digest": "sha256:" + "0" * 64,
        "scope": "shared",
        "adapter_links": {},
    }
    save_registry({"schema_version": SCHEMA_VERSION, "publications": {"demo-skill": record}})
    code = handle_cli(parse("doctor", "--json"))
    value = json.loads(capsys.readouterr().out)
    assert code == 2 and any("registry_invalid" in error for error in value["errors"])


def test_sentinel_never_appears_in_cli_output(isolated_home, capsys, monkeypatch):
    monkeypatch.setattr(plugin, "_MIDDLEWARE_AVAILABLE", True)
    sentinel = "DO_NOT_LOG_THIS_SECRET"
    package = isolated_home["local"] / "demo-skill"
    package.mkdir()
    package.joinpath("SKILL.md").write_text(f'---\nname: demo-skill\ndescription: t\nleak: "{sentinel}\n---\n')
    for parts in (("status",), ("doctor",), ("publish", "demo-skill"), ("audit", "--limit", "50")):
        handle_cli(parse(*parts))
        output = capsys.readouterr().out
        assert sentinel not in output
        code = handle_cli(parse(*parts, "--json"))
        assert sentinel not in capsys.readouterr().out
