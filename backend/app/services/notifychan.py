"""通知渠道业务规则：状态流转、字段校验与SLA升级发送都收在这里。

通知渠道不自己维护一份 SLA履约事件副本：待发送清单和发送动作都调用
``app.services.slafulfillment``，读到的与SLA监控详情完全同一份数据记录。
发送前还会复核规则是否仍然有效，避免关闭规则后仍向默认渠道推送。
"""
from __future__ import annotations

from typing import Any

from app.services import slafulfillment
from app.store import store

MODULE = "notifychan"
REQUIRED_FIELDS = ["渠道编号", "渠道类型", "接收地址"]
STATUS_ORDER = ["草稿", "配置评审中", "已启用", "已停用"]
ACTION_RULES = {"提交配置评审": "配置评审中", "启用渠道": "已启用", "停用渠道": "已停用"}
NEGATIVE_ACTIONS = ["停用渠道"]


class NotifychanService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("渠道编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": store.next_id(MODULE)}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"渠道配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于通知渠道可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        with store.lock:
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"渠道配置已{action}"

    # ---- SLA升级：只消费 slafulfillment 的履约事件，不做第二份数据 ----

    def list_escalations(self, *, state: str | None = None) -> dict[str, Any]:
        rows = list(slafulfillment.list_escalations())
        if state:
            rows = [row for row in rows if row.get("升级状态") == state]
        return {"module": "sla_escalation", "total": len(rows), "items": rows}

    def list_fulfillment_events(self, *, current_only: bool = True) -> dict[str, Any]:
        rows = list(slafulfillment.list_events())
        if current_only:
            rows = [row for row in rows if row.get("是否当前")]
        return {"module": "sla_fulfillment", "total": len(rows), "items": rows}

    def send_escalation(self, identifier: str) -> tuple[dict[str, Any] | None, str]:
        """按升级记录发出SLA升级；规则已关闭/版本过期/无有效渠道时拒绝发送。"""
        return slafulfillment.send_escalation(identifier)
