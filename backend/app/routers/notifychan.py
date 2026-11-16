"""通知渠道接口：维护渠道配置，覆盖提交配置评审、启用渠道、停用渠道等动作。

另外提供SLA升级的消费入口：待发送清单、履约事件与发送动作都读取
``app.services.slafulfillment`` 的同一份SLA履约事件数据记录。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.notifychan import NotifychanService

router = APIRouter(prefix="/api/notifychan", tags=["通知渠道"])

service = NotifychanService()

LIST_FIELDS = ["渠道编号", "渠道类型", "接收地址", "消息模板", "发送频率", "静默时段开始", "静默时段结束", "消息优先级"]
STATUSES = ["草稿", "配置评审中", "已启用", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按渠道编号检索"),
    status: str | None = Query(default=None, description="草稿、配置评审中、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按渠道编号与状态过滤通知渠道列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出通知渠道清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "notifychan", "total": total, "items": items}


@router.get("/sla-escalations")
def list_sla_escalations(
    state: str | None = Query(default=None, description="待发送、已发送、已终止、渠道不可用"),
) -> dict[str, Any]:
    """SLA升级记录消费入口；与SLA监控详情指向同一条履约事件。"""
    return service.list_escalations(state=state)


@router.get("/sla-events")
def list_sla_events(
    current_only: bool = Query(default=True, description="只返回当前有效的履约事件"),
) -> dict[str, Any]:
    """SLA履约事件数据记录消费入口；默认只给当前有效事件。"""
    return service.list_fulfillment_events(current_only=current_only)


@router.post("/sla-escalations/{identifier}/send", response_model=ActionResult)
def send_sla_escalation(identifier: str) -> ActionResult:
    """发送一条SLA升级；规则关闭、版本过期或渠道失效时拒绝，不改发默认渠道。"""
    escalation, message = service.send_escalation(identifier)
    if escalation is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=escalation)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条渠道配置明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"渠道配置 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条渠道配置，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="渠道配置已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条渠道配置执行提交配置评审、启用渠道、停用渠道；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
