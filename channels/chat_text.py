"""把"给终端写"的回复改成聊天软件能看的样子。

Agent 的回复文本最初是给终端 + 飞书写的，里面混着几样在 QQ / 微信 / Telegram 里纯属噪声的东西：

* ```` ```json … ``` ```` 计划块 —— 那是给程序解析的，玩家不需要看（而且一次就上千字符）；
* `### 标题` / `**加粗**` / `` `代码` `` / `----` 分隔线 —— 聊天软件不渲染 Markdown；
* 连续空行、行尾空格。

实测：一条审批消息（推理正文 + JSON + 审批屏）会被切成 4 条 QQ 消息，还把某一行劈成两半。
这里只做"显示层"的纯文本化，不动任何业务逻辑 —— 原始文本照样完整写进
`memory/chat_context.json`，所以信息不会丢。
"""

import re

# ```lang\n...\n``` 整块（含围栏）；行内 `code`
FENCE_RE = re.compile(r"```[^\n]*\n.*?```|```.*?```", re.DOTALL)
# 代码块前面的 Markdown 标题（"### 🤖 BGI 执行指令"）—— 块都删了，标题留着就是一句废话
LABELED_FENCE_RE = re.compile(
    r"^[ \t]{0,3}#{1,6}[^\n]*\n+```[^\n]*\n.*?```", re.DOTALL | re.MULTILINE
)
PLACEHOLDER_RE = re.compile(r"\x00CODE\d+\x00")
INLINE_CODE_RE = re.compile(r"`([^`\n]*)`")
# **加粗** / __加粗__ / *斜体*（单个 * 不动，可能是列表符号或乘号）
BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*|__([^_\n]+)__")
ITALIC_RE = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*(.+?)\s*#*\s*$", re.MULTILINE)
RULE_RE = re.compile(r"^\s*(?:[-=*_]\s*){3,}$", re.MULTILINE)
LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
MULTI_BLANK_RE = re.compile(r"\n{3,}")


def to_plain_text(text, keep_code=False):
    """纯文本化：去 Markdown、去代码块、压空行。

    `keep_code=True` 时保留 ``` 代码块（调试/排查用）—— 注意代码块要先"藏起来"再处理，
    否则里面 JSON 的 `"key": "**值**"`、`# 注释` 会被后面的规则一起改掉。
    """
    body = str(text or "").replace("\r\n", "\n").replace("\r", "\n")

    if not keep_code:
        body = LABELED_FENCE_RE.sub("", body)

    blocks = []

    def stash(match):
        blocks.append(match.group(0))
        return f"\x00CODE{len(blocks) - 1}\x00"

    body = FENCE_RE.sub(stash, body)

    body = LINK_RE.sub(r"\1（\2）", body)
    body = HEADING_RE.sub(r"\1", body)
    body = BOLD_RE.sub(lambda m: m.group(1) or m.group(2) or "", body)
    body = ITALIC_RE.sub(r"\1", body)
    body = INLINE_CODE_RE.sub(r"\1", body)
    body = RULE_RE.sub("", body)

    if keep_code:
        for index, block in enumerate(blocks):
            body = body.replace(f"\x00CODE{index}\x00", block)
    else:
        body = PLACEHOLDER_RE.sub("", body)

    lines = [line.rstrip() for line in body.split("\n")]
    body = "\n".join(lines)
    body = MULTI_BLANK_RE.sub("\n\n", body)
    return body.strip()


def summarize(text, limit=160):
    """一句话摘要（超长就截断并加省略号）——用于"这条消息太长，看电脑端"这类提示。"""
    body = to_plain_text(text).replace("\n", " ")
    body = re.sub(r"\s+", " ", body).strip()
    if len(body) <= limit:
        return body
    return body[: limit - 1].rstrip() + "…"
