# epub-translator

用大模型翻译 EPUB 电子书的命令行工具。译文替换原文，书的结构（目录、链接、脚注、图片、样式）保持不变。

支持 OpenAI 兼容接口、Google Gemini，以及通过 Claude Code 命令行（`claude -p`）调用 Claude。针对英文译简体中文做了调优，其他语言对也能用。

## 安装

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
```

有 `uv` 的话用 `uv sync` 也可以。需要 Python 3.12 以上。

## 配置

配置写在项目目录下的 `.env` 里。复制一份样例再改：

```sh
cp .env.example .env
```

```sh
EPUBTR_PROVIDER=google          # openai、google 或 claude
EPUBTR_MODEL=<模型名>
EPUBTR_API_KEY=<密钥>
```

三种类型：

| `EPUBTR_PROVIDER` | 含义 |
|---|---|
| `openai` | 任何 OpenAI 兼容接口，一般还要设 `EPUBTR_BASE_URL` |
| `google` | Google Gemini |
| `claude` | 本机已登录的 Claude Code 命令行，每次请求是一次不带工具的 `claude -p`。不需要密钥，`EPUBTR_MODEL` 填传给 `claude --model` 的值，如 `sonnet` |

`.env` 里有密钥，已列入 `.gitignore`。工具先找当前目录下的 `.env`，没有再找项目目录下的；也可以用 `--env-file` 指定。真实环境变量里的同名变量优先于文件。没写 `EPUBTR_API_KEY` 时，会改用环境变量 `GEMINI_API_KEY` 或 `OPENAI_API_KEY`。

可选项：

| 变量 | 作用 |
|---|---|
| `EPUBTR_BASE_URL` | 接口地址。`google` 类型也可以设，用于中转服务 |
| `EPUBTR_MAX_OUTPUT_TOKENS` | 单次输出上限。有些服务默认值很低，长文章会被截断，这时需要调大 |
| `EPUBTR_TEMPERATURE` | 采样温度，不设则用服务端默认值 |
| `EPUBTR_TIMEOUT` | 单次请求的超时秒数 |
| `EPUBTR_JSON_MODE` | 是否要求服务端输出 JSON，默认 `true`。`openai` 类型的服务端拒绝这个参数时会自动关闭 |
| `EPUBTR_DROP_CLASS` | 每次翻译都删掉带这些 class 的元素，逗号分隔。相当于默认带上 `--drop-class` |
| `EPUBTR_DROP_DOC` | 每次翻译都拿掉这些文档（写文件名），逗号分隔。书里没有这个文档时不报错 |
| `EPUBTR_EXTRA` | 一行 JSON，原样传给接口：`openai` 类型作为请求体的附加字段，`google` 类型作为生成配置的附加字段，`claude` 类型的每一项作为 `--名字 值` 追加到命令行 |

## 使用

```sh
epub-translate translate book.epub --to zh-CN
```

输出 `book.zh-CN.epub`，放在原文件旁边，原文件不会被改动。

| 选项 | 作用 |
|---|---|
| `--to` | 目标语言代码，默认 `zh-CN` |
| `--provider` | 覆盖 `.env` 里的 `EPUBTR_PROVIDER` |
| `--model` | 覆盖 `.env` 里的 `EPUBTR_MODEL` |
| `--env-file` | 用另一个配置文件 |
| `--chapters 1-3,7` | 只翻译这几篇文档，按书脊顺序从 1 编号。试译时用 |
| `--drop-class 名字` | 删掉带这个 class 的元素，不翻译。用来去掉下载站加的页脚之类；可以写多次 |
| `--drop-doc 文件名` | 把这个文档从书里拿掉，连同只有它用到的图片。用来去掉广告页；可以写多次。之后 `--chapters` 的编号按拿掉后的顺序算 |
| `--concurrency` | 同时翻译的文档数，默认 4 |
| `-o` | 输出文件路径 |

翻译进度保存在原文件旁的 `book.epubtr/` 目录里。中断后重跑同一条命令，已完成的文档不会再请求。全部翻完后这个目录可以删掉。

退出状态码：`0` 全部正常；`1` 译完了，但有段落没能按原样译好，详见结尾的报告；`2` 配置、文件或接口出错，没有产出；`130` 被 Ctrl-C 中断。

## 工作方式

- **整篇翻译**：书脊中的每个 XHTML 文档整篇作为一次请求，文档之间并行。不做分片，所选模型需要能装下书里最长的一篇。
- **行内标签用占位符保护**：`He said <em>no</em>` 发给模型时是 `He said <x1>no</x1>`，回写时还原成原来的标签和属性，模型接触不到真实的标签。
- **不翻译的内容**：`pre`、`code`、`script`、`style`、SVG、MathML，以及带 `translate="no"` 的元素。
- **校验与重试**：模型返回后逐段检查，缺失或占位符不对的段落会再请求，最多重试两次。重试时仍然带上整篇文档和已完成的译文，只要求输出有问题的段落。
- **重试后仍有问题**：占位符不对的段落去掉行内格式、只保留译文文字；没有返回的段落保留原文。这些情况会列在运行结束时的报告里，并且命令以状态码 1 退出。
- **同一段原文全书只用一种译法**：各文档独立翻译，同一个标题在正文、栏目页、目录里可能被译成不同的样子。回写时以它作为标题出现处的译法为准，统一替换。
- **目录和书名**：导航文档、`toc.ncx` 和书名一起翻译，能在正文里找到相同原文的条目直接复用正文的译法，其余的合成一次请求。用了 `--chapters` 时不发这次请求，也不改书名和语言标记。

没有术语表：不同文档之间，人名和术语的译法不保证一致。

## 限制

- 只处理无 DRM 的 EPUB 2 和 EPUB 3。
- 不是合法 XML 的文档会原样保留，并在报告中列出。
- 图片里的文字不翻译。

## 开发

```sh
.venv/bin/pip install pytest
.venv/bin/pytest
```

测试用假的翻译器跑完整流程，不需要密钥，也不发请求。两个供应商的测试对着本机起的假服务端跑真实的 SDK。

`tests/checks.py` 可以单独运行，对比译本和原书的结构（文件、书脊、标签、id、链接）：

```sh
.venv/bin/python tests/checks.py book.epub book.zh-CN.epub
```
