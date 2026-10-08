"""Claude through the Claude Code command line (`claude -p`).

Uses whatever account the `claude` command is logged in with, so no API key
is involved. Every request starts a fresh, tool-less, single-turn session.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading

from .base import Cancelled, Completion, ProviderError, kind_for_status


class ClaudeCliProvider:
    def __init__(
        self,
        *,
        model: str,
        command: str = "claude",
        timeout: float | None = None,
        extra: dict | None = None,
        **_unused,
    ):
        self.model = model
        self._command = command
        self._timeout = timeout if timeout is not None else 1800.0
        self._extra = extra or {}
        if shutil.which(command) is None:
            raise ProviderError(f"找不到命令 {command!r}，请先安装并登录 Claude Code")

    def _argv(self, system: str) -> list[str]:
        argv = [
            self._command, "-p",
            "--model", self.model,
            "--system-prompt", system,
            "--output-format", "json",
            "--tools", "",  # a translator needs no tools
            "--no-session-persistence",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--setting-sources", "",
        ]
        for name, value in self._extra.items():
            argv += [f"--{name}", str(value)]
        return argv

    def complete(
        self, system: str, user: str, *, cancel: threading.Event | None = None
    ) -> Completion:
        try:
            process = subprocess.Popen(
                self._argv(system),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8",
            )
        except OSError as exc:
            raise ProviderError(f"无法运行 {self._command}：{exc}") from exc

        # communicate() in a thread, so that a cancelled run can kill the child at once.
        output: dict = {}

        def talk() -> None:
            try:
                output["out"], output["err"] = process.communicate(user)
            except Exception as exc:  # noqa: BLE001
                output["error"] = exc

        thread = threading.Thread(target=talk, daemon=True)
        thread.start()
        waited = 0.0
        while thread.is_alive():
            thread.join(0.2)
            waited += 0.2
            if cancel is not None and cancel.is_set():
                process.kill()
                raise Cancelled()
            if waited > self._timeout:
                process.kill()
                raise ProviderError(f"claude 超过 {self._timeout:.0f} 秒没有完成", kind="retry")

        out, err = output.get("out") or "", output.get("err") or ""
        try:
            data = json.loads(out)
        except ValueError:
            detail = (err or out).strip()[:500] or f"退出码 {process.returncode}"
            raise ProviderError(f"claude 运行失败：{detail}", kind="retry") from None
        if isinstance(data, list):  # some versions print every event; the result is the last
            data = next((d for d in reversed(data) if d.get("type") == "result"), {})

        text = data.get("result") or ""
        if data.get("is_error"):
            status = data.get("api_error_status")
            kind = kind_for_status(status) if isinstance(status, int) else "retry"
            if "login" in text.lower() or "authenticat" in text.lower():
                kind = "fatal"
            raise ProviderError(f"claude 报错：{text[:500] or data.get('subtype')}", kind=kind)
        stop = data.get("stop_reason")
        if not text and stop == "refusal":
            raise ProviderError("模型拒绝了这次请求", kind="request")
        if not text:
            raise ProviderError("claude 没有返回任何内容", kind="retry")
        return Completion(text, truncated=stop == "max_tokens", finish_reason=stop)
