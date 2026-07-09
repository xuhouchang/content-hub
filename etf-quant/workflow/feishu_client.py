"""
飞书文档 API 客户端。

使用 lark-cli (user identity) 追加内容到飞书文档。
"""

from __future__ import annotations

import subprocess
import logging

logger = logging.getLogger(__name__)


def append_to_doc(doc_token: str, markdown_content: str) -> None:
    """通过 lark-cli user 身份向飞书文档 append 内容。

    Args:
        doc_token: 文档 token
        markdown_content: Markdown 内容

    Raises:
        RuntimeError: lark-cli 返回失败
    """
    cmd = [
        "lark-cli", "docs", "+update",
        "--api-version", "v2",
        "--doc", doc_token,
        "--command", "append",
        "--doc-format", "markdown",
        "--content", markdown_content,
    ]

    preview = markdown_content.strip()[:60].replace("\n", " ")
    logger.info("追加到飞书文档 %s: %s...", doc_token, preview)

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"lark-cli 进程异常终止 (rc={result.returncode}): "
            f"{result.stderr[:200]}"
        )

    data = result.stdout.strip()
    if '"result": "success"' not in data:
        # Extract warning from JSON
        import json
        try:
            parsed = json.loads(data)
            warnings = parsed.get("warnings", [])
            error = parsed.get("error")
            msg = f"rc={parsed.get('result', '?')}"
            if warnings:
                msg += f"; warnings={warnings}"
            if error:
                msg += f"; error={error}"
            logger.warning("飞书文档追加未成功: %s", msg)
            raise RuntimeError(f"飞书文档追加失败: {msg}")
        except (json.JSONDecodeError, KeyError):
            raise RuntimeError(f"飞书文档追加失败: {data[:200]}")

    logger.info("飞书文档追加成功")
