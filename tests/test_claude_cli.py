"""The claude provider against a stand-in `claude` script."""

from __future__ import annotations

import json
import stat
import threading

import pytest

from epub_translator.providers.base import Cancelled, ProviderError
from epub_translator.providers.claude_cli import ClaudeCliProvider


def fake_claude(tmp_path, body: str):
    """An executable that records its arguments and stdin, then runs `body`."""
    script = tmp_path / "claude"
    script.write_text(
        "#!/usr/bin/env python3\nimport json, sys, time\n"
        f"open({str(tmp_path / 'call.json')!r}, 'w').write("
        "json.dumps({'argv': sys.argv[1:], 'stdin': sys.stdin.read()}))\n" + body
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return ClaudeCliProvider(model="sonnet", command=str(script))


def result(**fields) -> str:
    data = {"type": "result", "is_error": False, "stop_reason": "end_turn", **fields}
    return f"print({json.dumps(json.dumps(data))})\n"


def test_request_and_answer(tmp_path):
    provider = fake_claude(tmp_path, result(result='{"a": "译文"}'))
    done = provider.complete("system text", "user text")
    assert done.text == '{"a": "译文"}' and not done.truncated
    call = json.loads((tmp_path / "call.json").read_text())
    argv = call["argv"]
    assert call["stdin"] == "user text"
    assert argv[0] == "-p"
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--system-prompt") + 1] == "system text"
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--output-format") + 1] == "json"


def test_truncation(tmp_path):
    provider = fake_claude(tmp_path, result(result="half", stop_reason="max_tokens"))
    assert provider.complete("s", "u").truncated


@pytest.mark.parametrize(
    "body,kind",
    [
        (result(is_error=True, result="API Error: overloaded", api_error_status=529), "retry"),
        (result(is_error=True, result="rate limited", api_error_status=429), "retry"),
        (result(is_error=True, result="Invalid API key · Please run /login"), "fatal"),
        (result(result=""), "retry"),
        (result(result="", stop_reason="refusal"), "request"),
        ("sys.stderr.write('boom'); sys.exit(1)\n", "retry"),
    ],
)
def test_errors(tmp_path, body, kind):
    with pytest.raises(ProviderError) as info:
        fake_claude(tmp_path, body).complete("s", "u")
    assert info.value.kind == kind


def test_cancel_kills_the_process(tmp_path):
    provider = fake_claude(tmp_path, "time.sleep(30)\n")
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    with pytest.raises(Cancelled):
        provider.complete("s", "u", cancel=cancel)
