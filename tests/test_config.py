from __future__ import annotations

import pytest

from epub_translator import config as cfg
from epub_translator.config import ConfigError, load_settings, make_provider, parse_env
from epub_translator.providers.gemini import GeminiProvider
from epub_translator.providers.openai_compat import OpenAICompatProvider

ENV = """
# a comment
EPUBTR_PROVIDER=openai
EPUBTR_MODEL = "deepseek-model"
export EPUBTR_API_KEY='sk-from-file'
EPUBTR_BASE_URL=https://api.deepseek.com   # trailing comment
EPUBTR_MAX_OUTPUT_TOKENS=8192
EPUBTR_TEMPERATURE=0.3
EPUBTR_JSON_MODE=false
EPUBTR_EXTRA={"thinking_config": {"thinking_level": "low"}}
"""


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    """No stray settings from the real environment, the project or the working directory."""
    import os

    for name in list(os.environ):
        if name.startswith("EPUBTR_") or name in ("OPENAI_API_KEY", "GEMINI_API_KEY"):
            monkeypatch.delenv(name)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    monkeypatch.setattr(cfg, "PROJECT_DIR", empty)


@pytest.fixture
def env(tmp_path):
    path = tmp_path / ".env"
    path.write_text(ENV, encoding="utf-8")
    return path


def test_parse_env():
    assert parse_env("A=1\n# no\n\nexport B = 'two words'\nC=\"x # y\"\nD=3 # note\nrubbish\n") == {
        "A": "1", "B": "two words", "C": "x # y", "D": "3",
    }


def test_settings_from_file(env):
    s = load_settings(env)
    assert (s.type, s.model, s.api_key) == ("openai", "deepseek-model", "sk-from-file")
    assert s.base_url == "https://api.deepseek.com"
    assert s.max_output_tokens == 8192 and s.temperature == 0.3
    assert s.json_mode is False
    assert s.extra == {"thinking_config": {"thinking_level": "low"}}
    assert s.source == env


def test_overrides(env, monkeypatch):
    monkeypatch.setenv("EPUBTR_MODEL", "from-environment")
    assert load_settings(env).model == "from-environment"  # the real environment wins
    s = load_settings(env, provider="google", model="from-flag")
    assert (s.type, s.model) == ("google", "from-flag")


def test_file_is_found_in_the_working_directory_then_the_project(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("EPUBTR_PROVIDER=claude\nEPUBTR_MODEL=in-project\n")
    monkeypatch.setattr(cfg, "PROJECT_DIR", project)
    assert load_settings().model == "in-project"
    (tmp_path / "empty" / ".env").write_text("EPUBTR_PROVIDER=claude\nEPUBTR_MODEL=in-cwd\n")
    assert load_settings().model == "in-cwd"


def test_key_falls_back_to_the_usual_variable(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("EPUBTR_PROVIDER=google\nEPUBTR_MODEL=m\n")
    assert load_settings(path).api_key is None
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    assert load_settings(path).api_key == "g-key"


@pytest.mark.parametrize(
    "text,message",
    [
        ("", "EPUBTR_PROVIDER"),
        ("EPUBTR_PROVIDER=anthropic\nEPUBTR_MODEL=m\n", "必须是"),
        ("EPUBTR_PROVIDER=openai\n", "EPUBTR_MODEL"),
        ("EPUBTR_PROVIDER=openai\nEPUBTR_MODEL=m\nEPUBTR_TIMEOUT=soon\n", "数字"),
        ("EPUBTR_PROVIDER=openai\nEPUBTR_MODEL=m\nEPUBTR_EXTRA={oops\n", "JSON"),
    ],
)
def test_bad_settings(tmp_path, text, message):
    path = tmp_path / ".env"
    path.write_text(text)
    with pytest.raises(ConfigError, match=message):
        load_settings(path)


def test_no_file_at_all():
    with pytest.raises(ConfigError, match="EPUBTR_PROVIDER"):
        load_settings()
    with pytest.raises(ConfigError, match="找不到"):
        load_settings("nowhere.env")


def test_make_provider(env):
    one = make_provider(load_settings(env))
    assert isinstance(one, OpenAICompatProvider) and one.model == "deepseek-model"
    two = make_provider(load_settings(env, provider="google", model="gemini-model"))
    assert isinstance(two, GeminiProvider) and two.model == "gemini-model"


def test_missing_api_key(tmp_path):
    path = tmp_path / ".env"
    path.write_text("EPUBTR_PROVIDER=google\nEPUBTR_MODEL=m\n")
    with pytest.raises(ConfigError, match="EPUBTR_API_KEY"):
        make_provider(load_settings(path))


def test_claude_needs_no_key(tmp_path, monkeypatch):
    from epub_translator.providers.claude_cli import ClaudeCliProvider

    path = tmp_path / ".env"
    path.write_text("EPUBTR_PROVIDER=claude\nEPUBTR_MODEL=sonnet\n")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    provider = make_provider(load_settings(path))
    assert isinstance(provider, ClaudeCliProvider) and provider.model == "sonnet"
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(ConfigError, match="claude"):
        make_provider(load_settings(path))


def test_example_file_is_valid():
    from pathlib import Path

    example = Path(__file__).parent.parent / ".env.example"
    values = parse_env(example.read_text(encoding="utf-8"))
    assert values["EPUBTR_PROVIDER"] in cfg.TYPES and values["EPUBTR_MODEL"]
