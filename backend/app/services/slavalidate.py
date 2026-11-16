"""SLA监控业务规则：状态流转、字段校验与履约重算都收在这里。

关键约束（与需求一一对应）：

- 只有 ``已生效`` 规则能生成新预警；``已关闭`` / 评审中 / 草稿一律不产生新事件；
- 关闭规则立即生效：当前事件转历史，未发送升级停止，已发送记录原样保留；
- 恢复规则（生效规则）必须回到 ``规则评审中``，不能直接跳回 ``已生效``；
- 每次重算都以「完成截止时间 - 预警提前分钟」为准，旧超时状态只留痕不参与判定；
- 列表与详情都通过 ``slafulfillment.enrich_rule`` 输出同一份履约事件数据记录。
"""
from __future__ import annotations

from typing import Any

from app.services import slafulfillment
from app.store import store

MODULE = "slavalidate"
REQUIRED_FIELDS = ["规则编号", "适用作业", "完成截止时间"]
STATUS_ORDER = ["草稿", "规则评审中", "已生效", "已关闭"]
ACTION_RULES = {"提交规则评审": "规则评审中", "生效规则": "已生效", "关闭规则": "已关闭"}
NEGATIVE_ACTIONS = ["关闭规则"]

# 状态机：关闭后恢复必须重新走评审；已生效不能重复生效。
ACTION_SOURCES = {
    "提交规则评审": {"草稿", "规则评审中", "已关闭"},
    "生效规则": {"规则评审中"},
    "关闭规则": {"草稿", "规则评审中", "已生效"},
}


class SlavalidateService:
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
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        enriched = [slafulfillment.enrich_rule(dict(row)) for row in rows]
        total = len(enriched)
        start = max(page - 1, 0) * size
        return enriched[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return slafulfillment.enrich_rule(dict(entry), detail=True)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": store.next_id(MODULE)}
        for field in REQUIRED_FIELDS:
            entry[field] = values.get(field)
        for optional in ("预警提前分钟", "超时动作", "升级通知组", "每日频次", "容忍天数"):
            if optional in values:
                entry[optional] = values.get(optional)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["版本"] = 1
        entry["规则版本"] = 1
        entry["rule_version"] = 1
        rows.append(entry)
        return slafulfillment.enrich_rule(dict(entry), detail=True), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"SLA规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于SLA监控可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"

        with store.lock:
            current = str(entry.get("status") or STATUS_ORDER[0])
            if action == "生效规则":
                validation = slafulfillment.validate_rule(entry)
                blocking = [msg for msg in validation["errors"]]
                if blocking:
                    return None, "规则校验未通过，不能生效：" + "；".join(blocking)
                if current not in ACTION_SOURCES[action]:
                    if current == slafulfillment.STATUS_CLOSED:
                        return None, "已关闭规则需先提交规则评审，评审通过后才能重新生效"
                    return None, f"规则当前状态为{current}，不能直接生效"
            elif current not in ACTION_SOURCES[action]:
                return None, f"规则当前状态为{current}，不允许执行「{action}」"

            previous = current
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            # 版本随状态变更递增：恢复生效后旧履约事件自然失效，需要重新生成。
            entry["版本"] = slafulfillment.rule_version(entry) + 1
            entry["规则版本"] = entry["版本"]
            entry["rule_version"] = entry["版本"]

            if action == "关闭规则":
                slafulfillment.close_rule_fulfillment(entry, "规则已关闭")
                return (
                    slafulfillment.enrich_rule(dict(entry), detail=True),
                    f"SLA规则已关闭（{previous} → {target}），历史升级保留，未发送升级已停止",
                )

            if action == "生效规则":
                result = slafulfillment.recalc_rule(entry_id)
                note = result.get("message") or ""
                return (
                    slafulfillment.enrich_rule(dict(entry), detail=True),
                    f"SLA规则已生效（{previous} → {target}）{('；' + note) if note else ''}",
                )

            return slafulfillment.enrich_rule(dict(entry), detail=True), f"SLA规则已{action}"

    def recalculate(self, entry_id: int | None = None) -> dict[str, Any]:
        """按当前规则与运行水位重算履约事件；服务启动/手动触发都可以用。"""
        if entry_id is not None:
            result = slafulfillment.recalc_rule(entry_id)
            return {"results": [result], "events": slafulfillment.list_events()}
        results = slafulfillment.recalc_all()
        return {"results": results, "events": slafulfillment.list_events()}
