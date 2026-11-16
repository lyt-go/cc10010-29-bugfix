"""SLA监控接口：维护SLA规则，覆盖提交规则评审、生效规则、关闭规则等动作。

除常规CRUD外，这里还暴露履约事件与重算入口；所有入口都读取
``app.services.slafulfillment`` 里的同一份履约事件数据记录。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services import slafulfillment
from app.services.slavalidate import SlavalidateService

router = APIRouter(prefix="/api/slavalidate", tags=["SLA监控"])

service = SlavalidateService()

LIST_FIELDS = ["规则编号", "适用作业", "完成截止时间", "预警提前分钟", "超时动作", "升级通知组", "每日频次", "容忍天数"]
STATUSES = ["草稿", "规则评审中", "已生效", "已关闭"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、规则评审中、已生效、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤SLA监控列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出SLA监控清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "slavalidate", "total": total, "items": items}


@router.get("/events")
def list_fulfillment_events(
    rule_id: int | None = Query(default=None, description="只看某条规则"),
    state: str | None = Query(default=None, description="预警、超时、正常、已达成"),
    current_only: bool = Query(default=False, description="只看当前生效事件"),
) -> dict[str, Any]:
    """SLA履约事件数据记录：监控详情与通知渠道共用这一份。"""
    rows = list(slafulfillment.list_events())
    if rule_id is not None:
        rows = [row for row in rows if int(row.get("规则id", 0)) == rule_id]
    if state:
        rows = [row for row in rows if row.get("预警状态") == state]
    if current_only:
        rows = [row for row in rows if row.get("是否当前")]
    return {"module": "sla_fulfillment", "total": len(rows), "items": rows}


@router.get("/events/{event_id}")
def get_fulfillment_event(event_id: str) -> dict[str, Any]:
    """按履约事件编号或 id 读取单条SLA履约事件数据记录。"""
    event = slafulfillment.find_event(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail=f"SLA履约事件 {event_id} 不存在或已归档")
    return event


@router.get("/escalations")
def list_escalations(rule_id: int | None = Query(default=None, description="只看某条规则")) -> dict[str, Any]:
    """SLA升级记录：关闭规则后历史记录保留，未发送的会标记为已终止。"""
    rows = list(slafulfillment.list_escalations())
    if rule_id is not None:
        rows = [row for row in rows if int(row.get("规则id", 0)) == rule_id]
    return {"module": "sla_escalation", "total": len(rows), "items": rows}


@router.post("/recalculate", response_model=dict)
def recalculate_all() -> dict[str, Any]:
    """按当前运行水位重算所有已生效规则的履约事件；已关闭规则不会被重新激活。"""
    result = service.recalculate()
    return {"ok": True, "message": "SLA履约事件已按运行水位重算", **result}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条SLA规则明细（含当前履约事件与升级记录）；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"SLA规则 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/fulfillment")
def get_rule_fulfillment(entry_id: int) -> dict[str, Any]:
    """读取某条规则的履约事件与升级记录，与详情页指向同一条数据记录。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"SLA规则 {entry_id} 不存在或已归档")
    return {
        "rule_id": entry_id,
        "rule_status": entry.get("status"),
        "effective": entry.get("生效状态"),
        "current_event": entry.get("当前履约事件"),
        "events": entry.get("履约事件", []),
        "escalations": entry.get("升级记录", []),
    }


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条SLA规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="SLA规则已登记", entry=entry)


@router.post("/{entry_id}/recalculate", response_model=ActionResult)
def recalculate_entry(entry_id: int) -> ActionResult:
    """按SLA规则与运行水位校验并重算单条规则；非已生效规则不会产生新预警。"""
    result = slafulfillment.recalc_rule(entry_id)
    entry = service.get_entry(entry_id)
    return ActionResult(ok=bool(result.get("ok")), message=str(result.get("message")), entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条SLA规则执行提交规则评审、生效规则、关闭规则；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
