import json

from galet_memory.examples import episodic_cli


def _run(path, capsys, *args):
    result = episodic_cli.main(["--db", str(path), *args])
    captured = capsys.readouterr()
    return result, captured


def test_basic_episodic_cli_road_test(tmp_path, capsys):
    path = tmp_path / "chat2.sqlite"

    result, captured = _run(
        path,
        capsys,
        "create",
        "--account",
        "demo",
        "--agent",
        "lucy",
        "--session-id",
        "road-test",
        "--friendly-name",
        "Road test",
    )
    assert result == 0
    assert json.loads(captured.out)["session_id"] == "road-test"

    result, captured = _run(
        path,
        capsys,
        "add",
        "road-test",
        "Hello episodic memory",
    )
    assert result == 0
    assert json.loads(captured.out)["content"] == "Hello episodic memory"

    result, captured = _run(path, capsys, "show", "road-test")
    assert result == 0
    assert json.loads(captured.out)["events"][0]["content"] == (
        "Hello episodic memory"
    )

    result, captured = _run(
        path,
        capsys,
        "list",
        "--account",
        "demo",
    )
    assert result == 0
    assert json.loads(captured.out)[0]["friendly_name"] == "Road test"


def test_archive_command_demonstrates_scoped_reads(tmp_path, capsys):
    path = tmp_path / "chat2.sqlite"
    _run(
        path,
        capsys,
        "create",
        "--account",
        "demo",
        "--agent",
        "lucy",
        "--session-id",
        "road-test",
    )
    _run(path, capsys, "add", "road-test", "old event")

    result, captured = _run(
        path,
        capsys,
        "archive",
        "road-test",
        "The earlier conversation was summarized.",
        "--account",
        "demo",
        "--idempotency-key",
        "road-test-archive",
    )
    assert result == 0
    assert json.loads(captured.out)["action"] == "archive"

    _run(path, capsys, "add", "road-test", "new event")
    _, active = _run(path, capsys, "show", "road-test")
    _, archived = _run(
        path, capsys, "show", "road-test", "--scope", "archived"
    )
    _, complete = _run(
        path, capsys, "show", "road-test", "--scope", "all"
    )

    assert [event["content"] for event in json.loads(active.out)["events"]] == [
        "The earlier conversation was summarized.",
        "new event",
    ]
    assert [
        event["content"] for event in json.loads(archived.out)["events"]
    ] == ["old event"]
    assert [event["content"] for event in json.loads(complete.out)["events"]] == [
        "old event",
        "The earlier conversation was summarized.",
        "new event",
    ]


def test_show_missing_session_returns_nonzero(tmp_path, capsys):
    result, captured = _run(
        tmp_path / "chat2.sqlite", capsys, "show", "missing"
    )
    assert result == 2
    assert "Session not found" in captured.err
