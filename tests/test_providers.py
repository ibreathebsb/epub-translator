"""The two providers against a local HTTP server that speaks their wire formats.

Nothing leaves the machine. The real SDKs run end to end, so these tests
break when an SDK changes the calls or the response shapes we rely on.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from epub_translator.providers.base import Cancelled, ProviderError
from epub_translator.providers.gemini import GeminiProvider
from epub_translator.providers.openai_compat import OpenAICompatProvider


class Server:
    """Answers every POST with whatever `respond(path, body)` returns."""

    def __init__(self):
        self.requests: list[tuple[str, dict, dict]] = []
        self.respond = lambda path, body: (200, [])
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                headers = {key.lower(): value for key, value in self.headers.items()}
                owner.requests.append((self.path, body, headers))
                status, payload, *rest = owner.respond(self.path, body)
                headers = rest[0] if rest else {}
                if status != 200:
                    data = json.dumps(payload).encode()
                    self.send_response(status)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(data)))
                    for key, value in headers.items():
                        self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                try:
                    for event in payload:
                        line = event if isinstance(event, str) else json.dumps(event)
                        self.wfile.write(f"data: {line}\n\n".encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def server():
    srv = Server()
    yield srv
    srv.close()


# -- OpenAI compatible -------------------------------------------------------


def chunk(content=None, finish=None, **delta):
    if content is not None:
        delta["content"] = content
    return {
        "id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def openai_error(message, code=None):
    return {"error": {"message": message, "type": "invalid_request_error", "code": code}}


def openai(server, **options) -> OpenAICompatProvider:
    return OpenAICompatProvider(model="m", api_key="sk-test", base_url=server.url + "/v1", **options)


def test_openai_streams_text(server):
    server.respond = lambda path, body: (
        200,
        [chunk(role="assistant"), chunk('{"a"'), chunk(': "译文"}'), chunk(finish="stop"), "[DONE]"],
    )
    result = openai(server).complete("system text", "user text")
    assert result.text == '{"a": "译文"}'
    assert not result.truncated and result.finish_reason == "stop"

    path, body, headers = server.requests[0]
    assert path == "/v1/chat/completions"
    assert headers["authorization"] == "Bearer sk-test"
    assert body["model"] == "m" and body["stream"] is True
    assert body["messages"] == [
        {"role": "system", "content": "system text"},
        {"role": "user", "content": "user text"},
    ]
    assert body["response_format"] == {"type": "json_object"}
    assert "max_tokens" not in body and "temperature" not in body


def test_openai_options_reach_the_request(server):
    server.respond = lambda path, body: (200, [chunk("ok"), chunk(finish="stop"), "[DONE]"])
    provider = openai(
        server, json_mode=False, max_output_tokens=8192, temperature=0.3,
        extra={"enable_thinking": False},
    )
    provider.complete("s", "u")
    body = server.requests[0][1]
    assert "response_format" not in body
    assert body["max_tokens"] == 8192
    assert body["temperature"] == 0.3
    assert body["enable_thinking"] is False


def test_openai_reports_truncation(server):
    server.respond = lambda path, body: (200, [chunk("half an ans"), chunk(finish="length"), "[DONE]"])
    result = openai(server).complete("s", "u")
    assert result.text == "half an ans" and result.truncated


def test_openai_ignores_reasoning_and_empty_chunks(server):
    events = [
        {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "m", "choices": []},
        chunk(reasoning_content="let me think"),
        chunk("answer"),
        chunk(finish="stop"),
        "[DONE]",
    ]
    server.respond = lambda path, body: (200, events)
    assert openai(server).complete("s", "u").text == "answer"


def test_openai_drops_json_mode_when_the_server_refuses_it(server):
    def respond(path, body):
        if "response_format" in body:
            return 400, openai_error("response_format is not supported by this model")
        return 200, [chunk("plain"), chunk(finish="stop"), "[DONE]"]

    server.respond = respond
    provider = openai(server)
    assert provider.complete("s", "u").text == "plain"
    assert provider.complete("s", "u").text == "plain"
    # refused once, then never sent again
    assert ["response_format" in body for _, body, _ in server.requests] == [True, False, False]


def test_openai_switches_to_max_completion_tokens(server):
    def respond(path, body):
        if "max_tokens" in body:
            return 400, openai_error(
                "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens' instead."
            )
        return 200, [chunk("ok"), chunk(finish="stop"), "[DONE]"]

    server.respond = respond
    provider = openai(server, max_output_tokens=4000)
    assert provider.complete("s", "u").text == "ok"
    assert server.requests[-1][1]["max_completion_tokens"] == 4000
    assert server.requests[-1][1]["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize(
    "status,payload,kind",
    [
        (401, openai_error("Incorrect API key provided"), "fatal"),
        (402, openai_error("Insufficient Balance"), "fatal"),
        (404, openai_error("The model does not exist"), "fatal"),
        (429, openai_error("Rate limit reached"), "retry"),
        (429, openai_error("You exceeded your current quota", "insufficient_quota"), "fatal"),
        (500, openai_error("Internal error"), "retry"),
        (503, openai_error("Overloaded"), "retry"),
        (400, openai_error("This model's maximum context length is 8192 tokens"), "request"),
    ],
)
def test_openai_errors_are_classified(server, status, payload, kind):
    server.respond = lambda path, body: (status, payload)
    with pytest.raises(ProviderError) as info:
        openai(server).complete("s", "u")
    assert info.value.kind == kind
    assert str(status) in str(info.value) and payload["error"]["message"] in str(info.value)
    assert len(server.requests) == 1  # the SDK does not retry behind our back


def test_openai_reads_retry_after(server):
    server.respond = lambda path, body: (429, openai_error("slow down"), {"retry-after": "7"})
    with pytest.raises(ProviderError) as info:
        openai(server).complete("s", "u")
    assert info.value.retry_after == 7


def test_openai_connection_failure_is_transient():
    provider = OpenAICompatProvider(model="m", api_key="k", base_url="http://127.0.0.1:9/v1")
    with pytest.raises(ProviderError) as info:
        provider.complete("s", "u")
    assert info.value.kind == "retry"


def test_openai_empty_stream_is_transient(server):
    server.respond = lambda path, body: (200, ["[DONE]"])
    with pytest.raises(ProviderError) as info:
        openai(server).complete("s", "u")
    assert info.value.kind == "retry"


def test_openai_content_filter_refuses_the_request(server):
    server.respond = lambda path, body: (200, [chunk(finish="content_filter"), "[DONE]"])
    with pytest.raises(ProviderError) as info:
        openai(server).complete("s", "u")
    assert info.value.kind == "request"


def test_openai_cancel_stops_reading(server):
    server.respond = lambda path, body: (200, [chunk("a"), chunk("b"), chunk(finish="stop"), "[DONE]"])
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        openai(server).complete("s", "u", cancel=cancel)


# -- Gemini ------------------------------------------------------------------


def part(text=None, finish=None, thought=False):
    candidate: dict = {"index": 0}
    if text is not None:
        piece = {"text": text}
        if thought:
            piece["thought"] = True
        candidate["content"] = {"role": "model", "parts": [piece]}
    if finish:
        candidate["finishReason"] = finish
    return {"candidates": [candidate]}


def gemini_error(code, status, message):
    return {"error": {"code": code, "message": message, "status": status}}


def gemini(server, **options) -> GeminiProvider:
    return GeminiProvider(model="g-model", api_key="g-key", base_url=server.url, **options)


def test_gemini_streams_text(server):
    server.respond = lambda path, body: (
        200,
        [part("thinking about it", thought=True), part('{"a"'), part(': "译文"}', finish="STOP")],
    )
    result = gemini(server).complete("system text", "user text")
    assert result.text == '{"a": "译文"}'
    assert not result.truncated and result.finish_reason == "STOP"

    path, body, headers = server.requests[0]
    assert path.startswith("/v1beta/models/g-model:streamGenerateContent")
    assert headers["x-goog-api-key"] == "g-key"
    assert body["contents"] == [{"parts": [{"text": "user text"}], "role": "user"}]
    assert body["systemInstruction"]["parts"] == [{"text": "system text"}]
    assert body["generationConfig"] == {"responseMimeType": "application/json"}


def test_gemini_options_reach_the_request(server):
    server.respond = lambda path, body: (200, [part("ok", finish="STOP")])
    provider = gemini(
        server, json_mode=False, max_output_tokens=9000, temperature=0.2,
        extra={"thinking_config": {"thinking_budget": 0}},
    )
    provider.complete("s", "u")
    config = server.requests[0][1]["generationConfig"]
    # the SDK writes nested settings with either spelling; the API reads both
    thinking = config.pop("thinkingConfig")
    assert thinking in ({"thinkingBudget": 0}, {"thinking_budget": 0})
    assert config == {"maxOutputTokens": 9000, "temperature": 0.2}


def test_gemini_reports_truncation(server):
    server.respond = lambda path, body: (200, [part("half an ans", finish="MAX_TOKENS")])
    result = gemini(server).complete("s", "u")
    assert result.text == "half an ans" and result.truncated


@pytest.mark.parametrize(
    "status,payload,kind",
    [
        (400, gemini_error(400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key."), "fatal"),
        (403, gemini_error(403, "PERMISSION_DENIED", "Permission denied"), "fatal"),
        (404, gemini_error(404, "NOT_FOUND", "models/g-model is not found"), "fatal"),
        (429, gemini_error(429, "RESOURCE_EXHAUSTED", "Quota exceeded"), "retry"),
        (500, gemini_error(500, "INTERNAL", "Internal error"), "retry"),
        (503, gemini_error(503, "UNAVAILABLE", "The model is overloaded"), "retry"),
        (400, gemini_error(400, "INVALID_ARGUMENT", "The input token count exceeds the maximum"), "request"),
    ],
)
def test_gemini_errors_are_classified(server, status, payload, kind):
    server.respond = lambda path, body: (status, payload)
    with pytest.raises(ProviderError) as info:
        gemini(server).complete("s", "u")
    assert info.value.kind == kind
    assert payload["error"]["message"] in str(info.value)
    assert len(server.requests) == 1


def test_gemini_blocked_answer_refuses_the_request(server):
    server.respond = lambda path, body: (200, [part(finish="SAFETY")])
    with pytest.raises(ProviderError) as info:
        gemini(server).complete("s", "u")
    assert info.value.kind == "request" and "SAFETY" in str(info.value)


def test_gemini_blocked_prompt_refuses_the_request(server):
    server.respond = lambda path, body: (200, [{"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}])
    with pytest.raises(ProviderError) as info:
        gemini(server).complete("s", "u")
    assert info.value.kind == "request" and "PROHIBITED_CONTENT" in str(info.value)


def test_gemini_connection_failure_is_transient():
    provider = GeminiProvider(model="g", api_key="k", base_url="http://127.0.0.1:9")
    with pytest.raises(ProviderError) as info:
        provider.complete("s", "u")
    assert info.value.kind == "retry"


def test_gemini_cancel_stops_reading(server):
    server.respond = lambda path, body: (200, [part("a"), part("b", finish="STOP")])
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        gemini(server).complete("s", "u", cancel=cancel)
