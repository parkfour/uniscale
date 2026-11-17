import io
import json

from uniscale import ai


def test_chat_is_sent_as_real_turns_and_reports_truncation(tmp_path, monkeypatch):
    sent = {}

    def fake_query_chat(messages, endpoint, model, timeout, max_tokens, thinking=None):
        sent.update(messages=messages, max_tokens=max_tokens, thinking=thinking)
        return "Lower the dose of", True

    monkeypatch.setattr(ai, "query_chat", fake_query_chat)
    history = [{"role": "user", "content": "Review my stack"},
               {"role": "assistant", "content": "First point."},
               {"role": "user", "content": "And magnesium?"}]
    out = ai.chat_with_agent(history, db_path=str(tmp_path / "m.jsonl"))

    roles = [m["role"] for m in sent["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    assert "No greeting" in sent["messages"][0]["content"]
    assert sent["messages"][-1]["content"] == "And magnesium?"
    assert sent["max_tokens"] == ai.CHAT_MAX_TOKENS
    assert sent["thinking"] is False  # off unless [ai] thinking = true
    assert out["reply"] == "Lower the dose of" and out["truncated"] is True

    ai.chat_with_agent(history + [{"role": "assistant", "content": "Lower the dose of"}],
                       db_path=str(tmp_path / "m.jsonl"), continue_reply=True)
    assert sent["messages"][-2] == {"role": "assistant", "content": "Lower the dose of"}
    assert sent["messages"][-1] == {"role": "user", "content": ai.CONTINUE_INSTRUCTION}


def test_query_chat_reads_finish_reason(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen["payload"] = json.loads(req.data)
        seen["url"] = req.full_url
        body = {"choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]}
        return io.BytesIO(json.dumps(body).encode())

    monkeypatch.setattr(ai.urllib.request, "urlopen", fake_urlopen)
    text, truncated = ai.query_chat([{"role": "user", "content": "hi"}], endpoint="http://llm:8080/v1", max_tokens=99)
    assert (text, truncated) == ("partial", True)
    assert seen["url"] == "http://llm:8080/v1/chat/completions" and seen["payload"]["max_tokens"] == 99
    assert "chat_template_kwargs" not in seen["payload"]  # server default unless asked
    ai.query_chat([{"role": "user", "content": "hi"}], endpoint="http://llm:8080", thinking=False)
    assert seen["payload"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert ai.query_llm("hi", endpoint="http://llm:8080") == "partial"


def test_long_conversations_drop_oldest_turns():
    system = {"role": "system", "content": "s" * 7000}
    turns = []
    for i in range(20):
        turns += [{"role": "user", "content": f"q{i} " + "x" * 2000}, {"role": "assistant", "content": f"a{i} " + "y" * 2000}]
    turns.append({"role": "user", "content": "latest question"})
    fitted = ai._fit_context([system] + turns)
    assert fitted[0] is system and fitted[-1]["content"] == "latest question"
    assert fitted[1]["role"] == "user"                        # still starts on a user turn
    used = sum(len(m["content"]) for m in fitted) / ai.CHARS_PER_TOKEN
    assert used <= ai.CHAT_CONTEXT_TOKENS - ai.CHAT_MAX_TOKENS
    short = [system, {"role": "user", "content": "hi"}]
    assert ai._fit_context(short) == short
