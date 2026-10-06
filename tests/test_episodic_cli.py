import json
from galet_memory.examples import episodic_cli


def run(path, capsys, *args, account="junwin"):
    code = episodic_cli.main(["--db", str(path), "--account", account, *args])
    out = capsys.readouterr()
    return code, json.loads(out.out) if code == 0 else out.err


def test_cli_real_user_message_and_exchange_lifecycle(tmp_path, capsys):
    path = tmp_path / "chat.sqlite"
    code, s = run(path, capsys, "create", "--session-id", "s")
    assert code == 0 and "agent_name" not in s
    _, user = run(
        path, capsys, "append", "s", "Hello", "--actor", "junwin", "--correlation", "c"
    )
    _, response = run(
        path,
        capsys,
        "append",
        "s",
        "Hi",
        "--role",
        "assistant",
        "--actor",
        "peace",
        "--correlation",
        "c",
    )
    _, page = run(
        path, capsys, "recent", "s", "--count", "10", "--kind", "user_message"
    )
    assert [e["content"] for e in page["events"]] == ["Hello"]
    assert page["events"][0]["correlation_ids"] == ["c"]
    _, single = run(path, capsys, "event", "s", user["event_id"])
    assert single["content"] == "Hello"
    _, exchange = run(path, capsys, "exchange", "s", "c")
    assert [e["event_id"] for e in exchange["events"]] == [
        user["event_id"],
        response["event_id"],
    ]
    _, sessions = run(path, capsys, "list")
    assert sessions["items"][0]["session_id"] == "s"
    _, result = run(path, capsys, "invalidate", "s", "c")
    assert result["event_count"] == 2
    _, empty = run(path, capsys, "recent", "s")
    assert empty["events"] == []
    assert run(path, capsys, "show", "s", account="another")[0] == 2
    assert run(path, capsys, "show", "missing")[0] == 2
