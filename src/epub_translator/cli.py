"""Command line entry point. All the work happens in `pipeline`."""

from __future__ import annotations

import re
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
)

from .config import ConfigError, load_settings, make_provider
from .epub import EpubError
from .pipeline import DocReport, Report, translate_book
from .providers.base import ProviderError
from .translate import Issue

app = typer.Typer(add_completion=False, no_args_is_help=True)
console = Console(highlight=False)

REASONS = {
    "truncated": "输出被截断",
    "missing": "有段落没有返回",
    "invalid": "有段落的占位符不对",
}


@app.callback()
def main() -> None:
    """用 AI 翻译 EPUB 电子书，保持原书结构不变。"""


def parse_chapters(value: str | None) -> set[int] | None:
    """Turn '1-3,7' into {1, 2, 3, 7}."""
    if value is None:
        return None
    chosen: set[int] = set()
    for part in value.split(","):
        m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", part)
        first, last = (int(m.group(1)), int(m.group(2) or m.group(1))) if m else (0, 0)
        if not 1 <= first <= last:
            raise typer.BadParameter("序号从 1 开始，写法如 1-3,7", param_hint="--chapters")
        chosen.update(range(first, last + 1))
    return chosen


def _label(doc: DocReport) -> str:
    number = f"{doc.index:>3}" if doc.index else "  ·"
    title = doc.title if len(doc.title) <= 60 else doc.title[:59] + "…"
    return f"[dim]{number}[/] {escape(title)}"


def _grouped(issues: list[Issue]) -> list[tuple[Issue, int | None]]:
    """Merge runs of consecutive segments that share a problem: (first issue, last segment)."""
    groups: list[list] = []
    for issue in issues:
        if groups:
            first, last = groups[-1]
            if (
                issue.seg is not None
                and last is not None
                and issue.seg == last + 1
                and (issue.kind, issue.detail) == (first.kind, first.detail)
            ):
                groups[-1][1] = issue.seg
                continue
        groups.append([issue, issue.seg])
    return [(first, last) for first, last in groups]


def _describe(issue: Issue, last: int | None) -> str:
    if issue.seg is None:
        where = "整篇"
    elif last != issue.seg:
        where = f"第 {issue.seg}–{last} 段"
    else:
        where = f"第 {issue.seg} 段"
    if issue.kind == "placeholder":
        return f"{where}：占位符不对（{issue.detail}），已去掉行内格式，只保留译文文字"
    if issue.kind == "missing":
        reason = "输出被截断" if issue.detail == "truncated" else "模型没有返回这一段"
        return f"{where}：{reason}，保留原文"
    if issue.kind == "untranslated":
        return f"{where}：模型原样返回，可能没有翻译"
    if issue.kind == "request":
        return f"{where}：请求被拒绝，保留原文（{issue.detail}）"
    if issue.kind == "parse":
        return f"{where}：不是合法的 XML，原样保留（{issue.detail}）"
    return f"{where}：{issue.kind} {issue.detail}"


def print_report(report: Report) -> None:
    done = [d for d in report.docs if d.index and d.status in ("translated", "cached")]
    cached = sum(d.status == "cached" for d in done)
    minutes, seconds = divmod(int(report.seconds), 60)
    console.print(
        f"\n翻译了 {len(done)} 篇文档（其中 {cached} 篇来自缓存），"
        f"发出 {report.requests} 次请求，用时 {minutes} 分 {seconds} 秒"
    )
    flagged = [d for d in report.docs if d.issues]
    if flagged:
        console.print("\n[bold]需要留意的地方[/]")
        for doc in flagged:
            console.print(f"{_label(doc)}  [dim]{escape(doc.name)}[/]")
            for issue, last in _grouped(doc.issues):
                console.print(f"      {escape(_describe(issue, last))}")
    console.print(f"\n输出：{escape(str(report.output))}")


@app.command()
def translate(
    book: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True, help="要翻译的 EPUB 文件"
    ),
    to: str = typer.Option("zh-CN", "--to", help="目标语言代码"),
    provider_type: str | None = typer.Option(
        None, "--provider", help="覆盖 .env 里的 EPUBTR_PROVIDER：openai、google 或 claude"
    ),
    model: str | None = typer.Option(None, "--model", help="覆盖 .env 里的 EPUBTR_MODEL"),
    chapters: str | None = typer.Option(
        None, "--chapters", help="只翻译这些文档，按书脊顺序从 1 编号，如 1-3,7"
    ),
    concurrency: int = typer.Option(4, "--concurrency", min=1, help="同时翻译的文档数"),
    output: Path | None = typer.Option(
        None, "-o", "--output", help="输出文件，默认是原文件旁的 <书名>.<语言>.epub"
    ),
    env_file: Path | None = typer.Option(
        None, "--env-file", help="配置文件，默认是当前目录或项目目录下的 .env"
    ),
) -> None:
    """翻译一本 EPUB。中断后重跑同一条命令会接着上次的进度。"""
    selected = parse_chapters(chapters)
    out = output or book.with_name(f"{book.stem}.{to}.epub")
    try:
        settings = load_settings(env_file, provider=provider_type, model=model)
        provider = make_provider(settings)
    except ConfigError as exc:
        console.print(f"[red]配置有误：[/]{escape(str(exc))}")
        raise typer.Exit(2) from None

    console.print(
        f"{escape(book.name)} → {escape(out.name)}　"
        f"[dim]{escape(settings.model)}（{escape(settings.type)}）· {escape(to)} · 并发 {concurrency}[/]"
    )
    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    )
    task = progress.add_task("翻译中", total=None)

    def emit(kind: str, doc: DocReport | None = None, **data) -> None:
        if kind == "plan":
            remaining = data["translatable"] - data["cached"]
            progress.update(task, total=remaining, visible=remaining > 0)
            console.print(
                f"共 {data['documents']} 篇文档，{data['translatable']} 篇有可译文本"
                f"（{data['segments']} 段），其中 {data['cached']} 篇已有缓存"
            )
        elif kind == "retry":
            console.print(
                f"  [yellow]↻[/] {_label(doc)}　第 {data['attempt']} 次请求，"
                f"补 {data['pending']} 段（{REASONS.get(data['reason'], data['reason'])}）"
            )
        elif kind == "wait":
            console.print(
                f"  [yellow]…[/] {_label(doc)}　请求失败，{data['seconds']:.0f} 秒后重试"
                f"（{escape(data['error'][:200])}）"
            )
        elif kind == "done":
            mark = "[yellow]⚠[/]" if doc.issues else "[green]✓[/]"
            extra = f" · {doc.requests} 次请求" if doc.requests > 1 else ""
            console.print(
                f"  {mark} {_label(doc)}　[dim]{doc.segments} 段 · {doc.seconds:.0f}s{extra}[/]"
            )
            if doc.index:
                progress.advance(task)

    try:
        with progress:
            report = translate_book(
                book, out, provider,
                target=to, chapters=selected, concurrency=concurrency, emit=emit,
            )
    except KeyboardInterrupt:
        console.print("\n已中断。完成的文档已保存，重跑同一条命令会接着翻译。")
        raise typer.Exit(130) from None
    except (EpubError, ProviderError) as exc:
        console.print(f"\n[red]出错：[/]{escape(str(exc))}")
        if isinstance(exc, ProviderError):
            console.print("完成的文档已保存，解决问题后重跑同一条命令会接着翻译。")
        raise typer.Exit(2) from None

    print_report(report)
    if not report.ok:
        raise typer.Exit(1)
