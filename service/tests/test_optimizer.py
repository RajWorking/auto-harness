import json

from openai.types.chat import ChatCompletionMessage, ChatCompletionMessageFunctionToolCall

from service import optimizer


def test_prompt_reuses_program_template_and_lists_tables():
    assert "- Did it verify its solution?" in optimizer.SYSTEM_PROMPT
    assert "Enforced TODO planning" in optimizer.SYSTEM_PROMPT
    assert "iterations(job_id UUID, index INTEGER, commit VARCHAR(40), parent VARCHAR(40), result JSONB)" in optimizer.SYSTEM_PROMPT
    assert "meta_messages" not in optimizer.SYSTEM_PROMPT
    assert "read the agent's code and its control flow" in optimizer.SYSTEM_PROMPT


def test_bash_runs_in_checkout_without_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    out = optimizer.bash(tmp_path, "pwd; env")
    assert str(tmp_path.resolve()) in out
    assert "secret" not in out and "SERVICE_DATABASE_URL" not in out
    assert out.endswith("[exit code 0]")


def test_bash_output_is_storable(tmp_path):
    assert optimizer.bash(tmp_path, r"printf 'a\377\000b'") == "a\ufffdb[exit code 0]"


def test_sql_is_read_only_and_single_statement(client):
    rows = json.loads(optimizer.sql("SELECT 'a%' LIKE '%' AS ok, '{\"a\":1}'::jsonb AS doc"))
    assert rows == [{"ok": True, "doc": {"a": 1}}]
    for query in ["DELETE FROM jobs", "COMMIT; DELETE FROM jobs", "SELECT 1; SELECT 2"]:
        try:
            optimizer.sql(query)
        except Exception as e:
            assert "read-only" in str(e) or "multiple commands" in str(e), query
        else:
            raise AssertionError(f"{query!r} ran")


def test_sql_reads_only_allowed_tables(client):
    assert json.loads(optimizer.sql("SELECT count(*) AS n FROM jobs JOIN iterations ON iterations.job_id = jobs.id")) == [{"n": 0}]
    try:
        optimizer.sql("SELECT * FROM meta_messages")
    except Exception as e:
        assert "permission denied" in str(e)
    else:
        raise AssertionError("meta_messages was readable")


def test_sql_output_is_whole(client):
    call = type("Call", (), {"function": type("F", (), {
        "name": "sql", "arguments": json.dumps({"query": "SELECT repeat('x', 100) AS v FROM generate_series(1, 200)"}),
    })})
    rows = json.loads(optimizer.MetaAgent._tool(None, call))
    assert rows == [{"v": "x" * 100}] * 200


def _reply(content=None, command=None):
    calls = None
    if command is not None:
        calls = [ChatCompletionMessageFunctionToolCall(
            id="call_1", type="function", function={"name": "bash", "arguments": json.dumps({"command": command})},
        )]
    message = ChatCompletionMessage(role="assistant", content=content, tool_calls=calls)
    return type("Reply", (), {"choices": [type("Choice", (), {"message": message})]})


def test_meta_agent_keeps_1_conversation_across_iterations(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    replies = iter([_reply(command="echo hi > note.txt"), _reply(content="first"), _reply(content="second")])
    recorded = []
    agent = optimizer.MetaAgent(tmp_path, recorded.append)
    agent.client = type("Client", (), {"chat": type("Chat", (), {"completions": type("C", (), {
        "create": staticmethod(lambda **kwargs: next(replies)),
    })})})

    assert agent.run("iteration 1") == "first"
    assert (tmp_path / "note.txt").read_text() == "hi\n"
    assert agent.run("iteration 2") == "second"
    roles = [m["role"] for m in agent.messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "user", "assistant"]
    assert agent.messages[3]["content"] == "[exit code 0]"
    assert recorded == agent.messages  # every message was recorded as it was added
