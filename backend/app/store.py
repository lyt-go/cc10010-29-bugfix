"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

SLA监控与通知渠道共用 ``SLA_FULFILLMENT_TABLES`` 里的三张内部表：
运行水位、SLA履约事件与升级记录。这些表不作为独立业务模块出现在运营概览里，
但两边路由读到的始终是同一份数据对象，避免各存一份副本导致状态不一致。
"""
from __future__ import annotations

import threading
from typing import Any

from app.seed import SEED_ROWS

# 内部共享表：只给SLA监控/通知渠道消费，不计入业务模块看板。
SLA_FULFILLMENT_TABLES = ("sla_watermark", "sla_fulfillment", "sla_escalation")
INTERNAL_MODULES = frozenset(SLA_FULFILLMENT_TABLES)


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 履约事件是跨模块共享的可变状态：扫描、关闭规则、发送升级都在这把锁里完成，
        # 保证“关闭规则”和“重新扫描”同时到达时只有一个确定结果。
        self.lock = threading.RLock()

    def module_names(self) -> list[str]:
        """业务模块名；内部共享表不算独立模块。"""
        return sorted(name for name in self._tables if name not in INTERNAL_MODULES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
