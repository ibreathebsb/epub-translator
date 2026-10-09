from __future__ import annotations

import zipfile

import pytest
import typer
from fakes import FakeProvider, answer, mark
from typer.testing import CliRunner

from epub_translator import cli

runner = CliRunner()


@pytest.fixture
def setup(epub, tmp_path, monkeypatch):
    """A .env in the working directory, and a fake provider behind it."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("EPUBTR_PROVIDER=openai\nEPUBTR_MODEL=fake-model\n")
    provider = FakeProvider()
    monkeypatch.setattr(cli, "make_provider", lambda settings: provider)
    return epub, provider


def test_translate(setup):
    epub, _ = setup
    result = runner.invoke(cli.app, ["translate", str(epub)])
    assert result.exit_code == 0, result.output
    out = epub.with_name("book.zh-CN.epub")
    assert out.is_file()
    with zipfile.ZipFile(out) as z:
        assert "译：Chapter One" in z.read("OEBPS/ch1.xhtml").decode()
    assert "共 3 篇文档，3 篇有可译文本（11 段），其中 0 篇已有缓存" in result.output
    assert "翻译了 3 篇文档（其中 0 篇来自缓存），发出 4 次请求" in result.output
    assert "book.zh-CN.epub" in result.output
    assert "需要留意的地方" not in result.output


def test_output_and_language_options(setup, tmp_path):
    epub, _ = setup
    out = tmp_path / "elsewhere" / "ja.epub"
    out.parent.mkdir()
    result = runner.invoke(cli.app, ["translate", str(epub), "--to", "ja", "-o", str(out), "--chapters", "2"])
    assert result.exit_code == 0, result.output
    with zipfile.ZipFile(out) as z:
        assert 'lang="ja"' in z.read("OEBPS/ch2.xhtml").decode()
        assert 'lang="en"' in z.read("OEBPS/ch1.xhtml").decode()


def test_problems_are_reported_and_set_the_exit_code(setup):
    epub, provider = setup

    def script(document, wanted, number):
        out = {i: mark(document[i]) for i in wanted if "morning" not in document[i]}
        return answer({i: ("见<x1>下一章" if "next chapter" in document[i] else t) for i, t in out.items()})

    provider.script = script
    result = runner.invoke(cli.app, ["translate", str(epub)])
    assert result.exit_code == 1, result.output
    assert "需要留意的地方" in result.output
    assert "第 4 段：占位符不对" in result.output and "已去掉行内格式" in result.output
    assert "第 3 段：模型没有返回这一段，保留原文" in result.output
    assert "OEBPS/ch1.xhtml" in result.output and "OEBPS/ch2.xhtml" in result.output
    assert epub.with_name("book.zh-CN.epub").is_file()


def test_report_merges_runs_of_segments(setup):
    from epub_translator.providers.base import Completion

    epub, provider = setup
    cut = Completion('{"translations": [{"id": 1, "text": "标题"}, {"id": 2, "te', truncated=True)
    provider.script = lambda document, wanted, number: cut if 1 in wanted else Completion("", truncated=True)
    result = runner.invoke(cli.app, ["translate", str(epub), "--chapters", "1"])
    assert result.exit_code == 1, result.output
    assert "第 2–5 段：输出被截断，保留原文" in result.output
    assert "补 4 段（输出被截断）" in result.output


def test_standing_drops_from_the_env_file(setup, tmp_path):
    epub, provider = setup
    with (tmp_path / ".env").open("a") as f:
        f.write("EPUBTR_DROP_CLASS=gone\nEPUBTR_DROP_DOC=notes.xhtml,not-in-this-book.xhtml\n")
    result = runner.invoke(cli.app, ["translate", str(epub)])
    assert result.exit_code == 0, result.output
    with zipfile.ZipFile(epub.with_name("book.zh-CN.epub")) as z:
        assert "OEBPS/notes.xhtml" not in z.namelist()
        assert "notes.xhtml" not in z.read("OEBPS/nav.xhtml").decode()
        assert "notes.xhtml" not in z.read("OEBPS/toc.ncx").decode()


def test_missing_config(epub, tmp_path):
    result = runner.invoke(cli.app, ["translate", str(epub), "--env-file", str(tmp_path / "nope.env")])
    assert result.exit_code == 2 and "找不到配置文件" in result.output
    empty = tmp_path / "empty.env"
    empty.write_text("")
    result = runner.invoke(cli.app, ["translate", str(epub), "--env-file", str(empty)])
    assert result.exit_code == 2
    assert "EPUBTR_PROVIDER=google" in result.output  # the example is shown


def test_missing_key(epub, tmp_path, monkeypatch):
    config = tmp_path / "my.env"
    config.write_text("EPUBTR_PROVIDER=openai\nEPUBTR_MODEL=m\n")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("EPUBTR_API_KEY", raising=False)
    result = runner.invoke(cli.app, ["translate", str(epub), "--env-file", str(config)])
    assert result.exit_code == 2 and "EPUBTR_API_KEY" in result.output


def test_fatal_provider_error(setup):
    from epub_translator.providers.base import ProviderError

    epub, provider = setup

    def script(document, wanted, number):
        raise ProviderError("HTTP 401: Incorrect API key", kind="fatal")

    provider.script = script
    result = runner.invoke(cli.app, ["translate", str(epub)])
    assert result.exit_code == 2
    assert "HTTP 401: Incorrect API key" in result.output
    assert not epub.with_name("book.zh-CN.epub").exists()


def test_not_an_epub(setup, tmp_path):
    bad = tmp_path / "bad.epub"
    bad.write_bytes(b"not a zip")
    result = runner.invoke(cli.app, ["translate", str(bad)])
    assert result.exit_code == 2 and "无法打开" in result.output


def test_parse_chapters():
    assert cli.parse_chapters(None) is None
    assert cli.parse_chapters("1-3,7") == {1, 2, 3, 7}
    assert cli.parse_chapters(" 2 , 5-6 ") == {2, 5, 6}
    for bad in ("a", "0", "3-1", "1-", ""):
        with pytest.raises(typer.BadParameter):
            cli.parse_chapters(bad)
