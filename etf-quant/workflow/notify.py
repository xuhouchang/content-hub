"""
通知接口（飞书/邮件预留）。

当前阶段：
- 日报通过 reports/daily.py 写入本地 Markdown 文件
- 后续通过此模块推送至飞书群/webhook

使用方式（Phase 2+）：
    from workflow.notify import Notifier
    notifier = FeishuNotifier(webhook_url="...")
    notifier.send_markdown(report_content)
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class Notifier(ABC):
    """通知抽象接口。"""

    @abstractmethod
    def send_message(self, title: str, content: str) -> bool:
        """发送消息。返回 True 表示成功。"""
        ...

    @abstractmethod
    def send_markdown(self, markdown: str) -> bool:
        """发送 Markdown 格式内容。"""
        ...


class ConsoleNotifier(Notifier):
    """本地终端通知（用于调试/测试）。"""

    def send_message(self, title: str, content: str) -> bool:
        print(f"[{title}] {content}")
        return True

    def send_markdown(self, markdown: str) -> bool:
        print(markdown)
        return True


class FeishuNotifier(Notifier):
    """飞书 Webhook 通知（预留）。"""

    def __init__(self, webhook_url: str | None = None):
        self._webhook_url = webhook_url

    def send_message(self, title: str, content: str) -> bool:
        if not self._webhook_url:
            return False
        # TODO(phase2): 实现飞书消息卡片发送
        return False

    def send_markdown(self, markdown: str) -> bool:
        # TODO(phase2): 实现飞书 Markdown 消息推送
        return False


def get_default_notifier() -> Notifier:
    """返回当前可用的通知器。"""
    return ConsoleNotifier()
