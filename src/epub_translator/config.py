"""Settings, read from a `.env` file in the project directory.

Variables already set in the real environment win over the file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .providers.base import Provider

PREFIX = "EPUBTR_"
ENV_FILE = ".env"
PROJECT_DIR = Path(__file__).resolve().parents[2]
# Where the key is looked for when EPUBTR_API_KEY is not set.
KEY_FALLBACK = {"openai": "OPENAI_API_KEY", "google": "GEMINI_API_KEY"}
TYPES = ("openai", "google", "claude")

EXAMPLE = """\
# openai（任何 OpenAI 兼容接口）、google（Gemini）或 claude（本机的 claude -p，不需要密钥）
EPUBTR_PROVIDER=google
EPUBTR_MODEL=<模型名>
EPUBTR_API_KEY=<密钥>
"""


class ConfigError(Exception):
    pass


@dataclass
class Settings:
    type: str
    model: str
    api_key: str | None = None
    base_url: str | None = None
    json_mode: bool = True
    max_output_tokens: int | None = None
    temperature: float | None = None
    timeout: float | None = None
    extra: dict = field(default_factory=dict)
    source: Path | None = None  # the file the settings came from, if any


def parse_env(text: str) -> dict[str, str]:
    """Read KEY=VALUE lines. Blank lines and # comments are skipped, quotes are optional."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] in ("'", '"') and value.endswith(value[0]):
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key.strip()] = value
    return values


def find_env_file(path: Path | None = None) -> Path | None:
    """The given file, else `.env` in the current directory, else in the project directory."""
    if path is not None:
        if not Path(path).is_file():
            raise ConfigError(f"找不到配置文件 {path}")
        return Path(path)
    for folder in (Path.cwd(), PROJECT_DIR):
        if (folder / ENV_FILE).is_file():
            return folder / ENV_FILE
    return None


def _number(values: dict[str, str], name: str, kind: type):
    raw = values.get(PREFIX + name)
    if not raw:
        return None
    try:
        return kind(raw)
    except ValueError:
        raise ConfigError(f"{PREFIX}{name} 应该是数字，现在是 {raw!r}") from None


def load_settings(
    path: Path | None = None, *, provider: str | None = None, model: str | None = None
) -> Settings:
    source = find_env_file(path)
    values: dict[str, str] = {}
    if source is not None:
        try:
            values = parse_env(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(f"无法读取 {source}：{exc}") from exc
    values.update({k: v for k, v in os.environ.items() if v})

    kind = (provider or values.get(PREFIX + "PROVIDER") or "").strip().lower()
    if not kind:
        where = source or PROJECT_DIR / ENV_FILE
        raise ConfigError(
            f"没有设置 {PREFIX}PROVIDER。请在 {where} 里写上配置，例如：\n\n{EXAMPLE}"
        )
    if kind not in TYPES:
        raise ConfigError(f"{PREFIX}PROVIDER 必须是 {'、'.join(TYPES)} 之一，现在是 {kind!r}")
    model = model or values.get(PREFIX + "MODEL")
    if not model:
        raise ConfigError(f"没有设置 {PREFIX}MODEL")

    extra: dict = {}
    if values.get(PREFIX + "EXTRA"):
        try:
            extra = json.loads(values[PREFIX + "EXTRA"])
        except ValueError as exc:
            raise ConfigError(f"{PREFIX}EXTRA 不是合法的 JSON：{exc}") from exc
        if not isinstance(extra, dict):
            raise ConfigError(f"{PREFIX}EXTRA 必须是一个 JSON 对象")
    json_mode = values.get(PREFIX + "JSON_MODE", "true").strip().lower()
    return Settings(
        type=kind,
        model=model,
        api_key=values.get(PREFIX + "API_KEY") or values.get(KEY_FALLBACK.get(kind, "")),
        base_url=values.get(PREFIX + "BASE_URL") or None,
        json_mode=json_mode not in ("0", "false", "no", "off"),
        max_output_tokens=_number(values, "MAX_OUTPUT_TOKENS", int),
        temperature=_number(values, "TEMPERATURE", float),
        timeout=_number(values, "TIMEOUT", float),
        extra=extra,
        source=source,
    )


def make_provider(settings: Settings) -> Provider:
    # Imported on demand: the SDKs are slow to load and only one is needed.
    if settings.type == "claude":
        from .providers.base import ProviderError
        from .providers.claude_cli import ClaudeCliProvider

        try:
            return ClaudeCliProvider(
                model=settings.model, timeout=settings.timeout, extra=settings.extra
            )
        except ProviderError as exc:
            raise ConfigError(str(exc)) from exc
    if not settings.api_key:
        raise ConfigError(
            f"没有设置 API 密钥：请在 .env 里写 {PREFIX}API_KEY"
            f"（或设置环境变量 {KEY_FALLBACK[settings.type]}）"
        )
    options = {
        "model": settings.model,
        "api_key": settings.api_key,
        "base_url": settings.base_url,
        "json_mode": settings.json_mode,
        "max_output_tokens": settings.max_output_tokens,
        "temperature": settings.temperature,
        "timeout": settings.timeout,
        "extra": settings.extra,
    }
    if settings.type == "openai":
        from .providers.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(**options)
    from .providers.gemini import GeminiProvider

    return GeminiProvider(**options)
