"""SLA履约事件：SLA监控与通知渠道共用的唯一事实来源。

这里集中处理四件事，避免 SLA监控 和 通知渠道 各算一套：

1. 用「完成截止时间 - 预警提前分钟」算出应预警时间，再和运行水位比较；
2. 只允许 ``已生效`` 规则生成新的履约事件，关闭规则只保留历史；
3. 同一规则、同一版本、同一水位状态只落一条履约事件（并发扫描幂等）；
4. 升级动作只能由通知渠道基于「当前有效规则 + 当前履约事件」发出。

运行水位、履约事件、升级记录三张表放在 store 的内部表里，两个模块读到的
是同一个列表对象，不会出现各入口数据副本不一致的问题。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.store import store

SLA_MODULE = "slavalidate"
WATERMARK_MODULE = "sla_watermark"
FULFILLMENT_MODULE = "sla_fulfillment"
ESCALATION_MODULE = "sla_escalation"
NOTIFY_MODULE = "notifychan"

STATUS_DRAFT = "草稿"
STATUS_REVIEW = "规则评审中"
STATUS_EFFECTIVE = "已生效"
STATUS_CLOSED = "已关闭"

ALERT_STATES = ("预警", "超时")
STATE_NORMAL = "正常"
STATE_WARNING = "预警"
STATE_OVERDUE = "超时"
STATE_MET = "已达成"
STATE_INVALID = "规则无效"

ESC_PENDING = "待发送"
ESC_SENT = "已发送"
ESC_STOPPED = "已终止"
ESC_UNAVAILABLE = "渠道不可用"

TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
)


def parse_datetime(value: Any) -> datetime | None:
    """把业务里常见的几种时间写法解析成 naive datetime；解析不了返回 None。"""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


def parse_int(value: Any) -> int | None:
    """严格解析整数：``'30分钟'`` 这类脏数据不会被静默当成 30。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None
    return None


def format_time(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else None


def now_time() -> datetime:
    return datetime.now().replace(microsecond=0)


def rule_version(rule: dict[str, Any]) -> int:
    """规则版本；兼容历史数据里没有版本字段的规则，统一按 1 处理。"""
    version = parse_int(rule.get("版本") or rule.get("规则版本") or rule.get("rule_version"))
    return version if version and version > 0 else 1


def advance_minutes(rule: dict[str, Any]) -> int:
    value = parse_int(rule.get("预警提前分钟"))
    return value if value is not None and value >= 0 else 0


def is_effective(rule: dict[str, Any]) -> bool:
    return str(rule.get("status") or "") == STATUS_EFFECTIVE


def warning_time(rule: dict[str, Any]) -> datetime | None:
    """应预警时间 = 完成截止时间 - 预警提前分钟（提前分钟必须真正参与计算）。"""
    deadline = parse_datetime(rule.get("完成截止时间"))
    if deadline is None:
        return None
    return deadline - timedelta(minutes=advance_minutes(rule))


def latest_watermark(job_name: str) -> dict[str, Any] | None:
    """取该作业最新一条运行水位；同时间按 id 较大者为准。"""
    candidates: list[tuple[datetime, int, dict[str, Any]]] = []
    for row in store.rows(WATERMARK_MODULE):
        if str(row.get("作业名称") or "") != job_name:
            continue
        stamp = parse_datetime(row.get("水位时间"))
        if stamp is None:
            continue
        candidates.append((stamp, int(row.get("id", 0)), row))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[-1][2]


def resolve_channel(group: Any) -> dict[str, Any] | None:
    """按规则上的升级通知组解析渠道，绝不回退到“默认渠道”。"""
    text = str(group or "").strip()
    if not text:
        return None
    channel_id = parse_int(text)
    for row in store.rows(NOTIFY_MODULE):
        if str(row.get("渠道编号") or "").strip() == text:
            return row
        if channel_id is not None and int(row.get("id", 0)) == channel_id:
            return row
    return None


def validate_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """从 SLA 规则本身校验字段，供“生效规则”和“重算履约”共用。"""
    errors: list[str] = []
    warnings: list[str] = []

    if not str(rule.get("规则编号") or "").strip():
        errors.append("规则编号不能为空")
    if not str(rule.get("适用作业") or "").strip():
        errors.append("适用作业不能为空")
    deadline = parse_datetime(rule.get("完成截止时间"))
    if deadline is None:
        errors.append("完成截止时间无法解析，需形如 2026-11-16 18:00:00")
    if parse_int(rule.get("预警提前分钟")) is None or advance_minutes(rule) < 0:
        errors.append("预警提前分钟必须是非负整数")

    group = str(rule.get("升级通知组") or "").strip()
    if not group:
        warnings.append("未配置升级通知组，超时不会发送升级通知")
    else:
        channel = resolve_channel(group)
        if channel is None:
            warnings.append(f"升级通知组 {group} 未找到对应通知渠道，升级不会发送")
        elif str(channel.get("status") or "") != "已启用":
            warnings.append(f"通知渠道 {channel.get('渠道编号')} 未启用，升级不会发送")

    if not is_effective(rule):
        warnings.append("规则未生效，不生成新预警")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "version": rule_version(rule),
    }


def compute_state(
    rule: dict[str, Any],
    watermark: dict[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    """根据规则时间窗与最新运行水位重算履约状态。

    ``watermark`` 里可能带着节点上报的旧「超时状态」，这里只把它当作参考字段，
    真正的状态一律用 水位时间 / 应预警时间 / 完成截止时间 重算，避免旧状态
    把已经恢复的水位又判成超时。
    """
    deadline = parse_datetime(rule.get("完成截止时间"))
    warn_at = warning_time(rule)
    if deadline is None or warn_at is None:
        return {
            "state": STATE_INVALID,
            "warning_at": None,
            "deadline": None,
            "watermark_time": None,
            "reached": False,
            "reason": "完成截止时间或预警提前分钟无效",
        }

    wm_time = parse_datetime(watermark.get("水位时间")) if watermark else None
    if wm_time is not None and wm_time >= deadline:
        state = STATE_MET
        reason = "运行水位已达到完成截止时间"
    elif now >= deadline:
        state = STATE_OVERDUE
        reason = "已过完成截止时间且水位未达成"
    elif now >= warn_at:
        state = STATE_WARNING
        reason = "已进入预警提前分钟窗口且水位未达成"
    else:
        state = STATE_NORMAL
        reason = "未到预警时间"

    return {
        "state": state,
        "warning_at": warn_at,
        "deadline": deadline,
        "watermark_time": wm_time,
        "reached": state == STATE_MET,
        "reason": reason,
        # 原始上报状态只做留痕，不参与判定。
        "watermark_reported_state": str(watermark.get("超时状态") or "") if watermark else "",
    }


def event_fingerprint(rule: dict[str, Any], state: str, watermark: dict[str, Any] | None) -> str:
    """同一规则 + 版本 + 水位记录 + 状态视为同一份履约结果。

    并发扫描/重复扫描同一超时状态时指纹一致，落库前会被幂等挡住，
    因此只有第一条预警结果生效。
    """
    watermark_key = "none"
    if watermark is not None:
        watermark_key = str(watermark.get("id") or watermark.get("水位编号") or "none")
    return f"{rule.get('id')}:v{rule_version(rule)}:{watermark_key}:{state}"


def current_event(rule_id: int) -> dict[str, Any] | None:
    for row in store.rows(FULFILLMENT_MODULE):
        if int(row.get("规则id", 0)) == rule_id and row.get("是否当前"):
            return row
    return None


def events_for_rule(rule_id: int) -> list[dict[str, Any]]:
    return [row for row in store.rows(FULFILLMENT_MODULE) if int(row.get("规则id", 0)) == rule_id]


def escalations_for_rule(rule_id: int) -> list[dict[str, Any]]:
    return [row for row in store.rows(ESCALATION_MODULE) if int(row.get("规则id", 0)) == rule_id]


def _stop_pending_escalations(rule_id: int, reason: str, *, exclude_event_id: int | None = None) -> None:
    for row in store.rows(ESCALATION_MODULE):
        if int(row.get("规则id", 0)) != rule_id:
            continue
        if exclude_event_id is not None and int(row.get("履约事件id", 0)) == exclude_event_id:
            continue
        if row.get("升级状态") in (ESC_PENDING, ESC_UNAVAILABLE):
            row["升级状态"] = ESC_STOPPED
            row["终止原因"] = reason
            row["是否当前"] = False


def _create_escalation(rule: dict[str, Any], event: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    """为预警/超时事件生成唯一升级记录；同一事件已有记录时维护原记录。"""
    event_id = int(event["id"])
    for row in store.rows(ESCALATION_MODULE):
        if int(row.get("履约事件id", 0)) == event_id:
            row["是否当前"] = True
            return row

    group = str(rule.get("升级通知组") or "").strip()
    channel = resolve_channel(group)
    channel_ready = (
        channel is not None and str(channel.get("status") or "") == "已启用"
    )
    row = {
        "id": store.next_id(ESCALATION_MODULE),
        "升级记录编号": f"SLAE-{store.next_id(ESCALATION_MODULE):04d}",
        "履约事件id": event_id,
        "履约事件编号": event.get("履约事件编号"),
        "规则id": int(rule.get("id", 0)),
        "规则编号": rule.get("规则编号"),
        "规则版本": rule_version(rule),
        "升级通知组": group,
        "渠道id": int(channel.get("id", 0)) if channel else None,
        "渠道编号": channel.get("渠道编号") if channel else None,
        "接收地址": channel.get("接收地址") if channel else None,
        "升级动作": "SLA预警升级",
        "升级状态": ESC_PENDING if channel_ready else ESC_UNAVAILABLE,
        "是否当前": True,
        "生成时间": format_time(now),
        "发送时间": None,
        "终止原因": None if channel_ready else "规则未绑定可用通知渠道，未回退默认渠道",
        "消息内容": _message(rule, event),
    }
    store.rows(ESCALATION_MODULE).append(row)
    return row


def _message(rule: dict[str, Any], event: dict[str, Any]) -> str:
    return (
        f"SLA{event.get('预警状态')}：规则 {rule.get('规则编号')}，作业 "
        f"{rule.get('适用作业')}，完成截止 {rule.get('完成截止时间')}，"
        f"当前水位 {event.get('水位时间') or '无'}"
    )


def recalc_rule(
    rule_id: int,
    *,
    now: datetime | None = None,
    watermark_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """校验并重算单条规则的履约事件。

    整个过程在 store.lock 内完成：即使“关闭规则”与“新水位扫描”同时到达，
    后进入的扫描也会重新读取规则状态，已关闭规则不会再生效新的预警。
    """
    moment = now or now_time()
    with store.lock:
        rule = store.find(SLA_MODULE, rule_id)
        if rule is None:
            return {"ok": False, "message": f"SLA规则 {rule_id} 不存在或已归档", "event": None}

        if watermark_values:
            _append_watermark(rule, watermark_values, moment)

        validation = validate_rule(rule)
        if not is_effective(rule):
            return {
                "ok": False,
                "message": f"规则当前状态为{rule.get('status')}，只有已生效规则才能生成新预警",
                "event": None,
                "validation": validation,
            }
        if not validation["ok"]:
            return {
                "ok": False,
                "message": "SLA规则校验未通过：" + "；".join(validation["errors"]),
                "event": None,
                "validation": validation,
            }

        job = str(rule.get("适用作业") or "")
        watermark = latest_watermark(job)
        computed = compute_state(rule, watermark, moment)
        state = computed["state"]
        if state == STATE_INVALID:
            return {
                "ok": False,
                "message": f"SLA规则时间窗无效：{computed['reason']}",
                "event": None,
                "validation": validation,
            }

        fingerprint = event_fingerprint(rule, state, watermark)
        for existing in events_for_rule(rule_id):
            if existing.get("事件指纹") == fingerprint:
                # 并发/重复扫描命中同一状态：沿用已生效的那份履约事件。
                return {
                    "ok": True,
                    "message": "同一超时状态已有生效的SLA履约事件，未重复生成",
                    "event": existing,
                    "duplicate": True,
                    "validation": validation,
                }

        # 新水位带来新结论：旧事件转为历史，未发送的旧升级同时停止。
        for existing in events_for_rule(rule_id):
            if existing.get("是否当前"):
                existing["是否当前"] = False
                existing["记录状态"] = "历史"
        _stop_pending_escalations(rule_id, "被更新的运行水位履约事件取代")

        event = {
            "id": store.next_id(FULFILLMENT_MODULE),
            "履约事件编号": f"SLAF-{store.next_id(FULFILLMENT_MODULE):04d}",
            "数据记录编号": None,
            "规则id": rule_id,
            "规则编号": rule.get("规则编号"),
            "状态来源规则": rule.get("规则编号"),
            "规则版本": rule_version(rule),
            "适用作业": job,
            "预警状态": state,
            "严重级别": "严重" if state == STATE_OVERDUE else ("提醒" if state == STATE_WARNING else "正常"),
            "预警时间": format_time(computed["warning_at"]),
            "完成截止时间": rule.get("完成截止时间"),
            "预警提前分钟": advance_minutes(rule),
            "水位时间": format_time(computed["watermark_time"]),
            "水位记录id": int(watermark.get("id", 0)) if watermark else None,
            "水位原始状态": computed["watermark_reported_state"],
            "是否达成": computed["reached"],
            "重算说明": computed["reason"],
            "事件指纹": fingerprint,
            "是否当前": True,
            "记录状态": "生效中",
            "规则状态": rule.get("status"),
            "通知状态": "待通知" if state in ALERT_STATES else "无需通知",
            "生成时间": format_time(moment),
        }
        event["数据记录编号"] = event["履约事件编号"]
        store.rows(FULFILLMENT_MODULE).append(event)

        escalation = _create_escalation(rule, event, moment) if state in ALERT_STATES else None
        event["升级记录id"] = int(escalation["id"]) if escalation else None

        return {
            "ok": True,
            "message": f"SLA履约重算完成：{state}",
            "event": event,
            "escalation": escalation,
            "validation": validation,
            "duplicate": False,
        }


def _append_watermark(rule: dict[str, Any], values: dict[str, Any], moment: datetime) -> dict[str, Any] | None:
    job = str(values.get("作业名称") or rule.get("适用作业") or "").strip()
    if not job:
        return None
    stamp = str(values.get("水位时间") or format_time(moment)).strip()
    row = {
        "id": store.next_id(WATERMARK_MODULE),
        "水位编号": f"SLAW-{store.next_id(WATERMARK_MODULE):04d}",
        "作业名称": job,
        "运行编号": values.get("运行编号"),
        "水位时间": stamp,
        "超时状态": values.get("超时状态") or "未知",
        "记录时间": format_time(moment),
    }
    store.rows(WATERMARK_MODULE).append(row)
    return row


def recalc_all(*, now: datetime | None = None) -> list[dict[str, Any]]:
    moment = now or now_time()
    results: list[dict[str, Any]] = []
    for rule in list(store.rows(SLA_MODULE)):
        if not is_effective(rule):
            continue
        results.append(recalc_rule(int(rule.get("id", 0)), now=moment))
    return results


def close_rule_fulfillment(rule: dict[str, Any], reason: str = "规则已关闭") -> None:
    """规则关闭：历史事件与已发升级原样保留，只停掉未发送的升级、禁止新增。"""
    rule_id = int(rule.get("id", 0))
    for event in events_for_rule(rule_id):
        event["是否当前"] = False
        event["记录状态"] = "历史"
        if event.get("通知状态") == "待通知":
            event["通知状态"] = "已终止"
    _stop_pending_escalations(rule_id, reason)


def enrich_rule(rule: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    """给规则补上“是否生效 / 版本 / 当前履约事件”，列表和详情共用。"""
    version = rule_version(rule)
    rule["版本"] = version
    rule["规则版本"] = version
    rule["rule_version"] = version
    rule["生效状态"] = is_effective(rule)
    rule["有效规则"] = is_effective(rule)
    event = current_event(int(rule.get("id", 0)))
    rule["当前履约事件"] = event
    rule["当前履约事件编号"] = event.get("履约事件编号") if event else None
    if detail:
        rule["校验结果"] = validate_rule(rule)
        rule["履约事件"] = events_for_rule(int(rule.get("id", 0)))
        rule["升级记录"] = escalations_for_rule(int(rule.get("id", 0)))
    return rule


def list_events() -> list[dict[str, Any]]:
    return store.rows(FULFILLMENT_MODULE)


def list_escalations() -> list[dict[str, Any]]:
    return store.rows(ESCALATION_MODULE)


def find_escalation(identifier: Any) -> dict[str, Any] | None:
    text = str(identifier or "").strip()
    if not text:
        return None
    numeric = parse_int(text)
    for row in store.rows(ESCALATION_MODULE):
        if str(row.get("升级记录编号") or "") == text:
            return row
        if numeric is not None and int(row.get("id", 0)) == numeric:
            return row
    return None


def find_event(identifier: Any) -> dict[str, Any] | None:
    text = str(identifier or "").strip()
    if not text:
        return None
    numeric = parse_int(text)
    for row in store.rows(FULFILLMENT_MODULE):
        if str(row.get("履约事件编号") or "") == text:
            return row
        if numeric is not None and int(row.get("id", 0)) == numeric:
            return row
    return None


def send_escalation(identifier: Any, *, now: datetime | None = None) -> tuple[dict[str, Any] | None, str]:
    """通知渠道发送 SLA 升级。

    只有同时满足以下条件才允许发送，否则说明原因而不是改发默认渠道：
    - 升级记录存在且仍指向一份当前履约事件；
    - 履约事件来自当前 ``已生效`` 规则，且规则版本与事件版本一致；
    - 规则上的升级通知组能解析到一条已启用渠道。
    """
    moment = now or now_time()
    with store.lock:
        escalation = find_escalation(identifier)
        if escalation is None:
            return None, f"升级记录 {identifier} 不存在或已归档"
        if escalation.get("升级状态") == ESC_SENT:
            return escalation, "该SLA升级已发送，未重复发送"
        if escalation.get("升级状态") == ESC_STOPPED:
            return None, "该SLA升级已终止，不再发送"

        event = store.find(FULFILLMENT_MODULE, int(escalation.get("履约事件id", 0)))
        if event is None:
            return None, "升级记录指向的SLA履约事件不存在"
        rule = store.find(SLA_MODULE, int(escalation.get("规则id", 0)))
        if rule is None:
            return None, "升级记录指向的SLA规则不存在"

        if not is_effective(rule):
            return None, f"规则当前状态为{rule.get('status')}，已关闭规则不发送升级"
        if not event.get("是否当前"):
            return None, "该履约事件已被更新的运行水位取代，不再发送升级"
        if int(event.get("规则版本", 0)) != rule_version(rule):
            return None, "履约事件的规则版本已过期，需按当前有效规则重新生成"
        if str(event.get("状态来源规则") or "") not in ("", str(rule.get("规则编号"))):
            return None, "履约事件不是由当前有效规则生成的"

        channel = resolve_channel(rule.get("升级通知组"))
        if channel is None:
            return None, "规则未绑定有效通知渠道，已阻止发送（不会回退默认渠道）"
        if str(channel.get("status") or "") != "已启用":
            return None, f"通知渠道 {channel.get('渠道编号')} 未启用，已阻止发送"

        escalation["升级状态"] = ESC_SENT
        escalation["是否当前"] = True
        escalation["发送时间"] = format_time(moment)
        escalation["渠道id"] = int(channel.get("id", 0))
        escalation["渠道编号"] = channel.get("渠道编号")
        escalation["接收地址"] = channel.get("接收地址")
        escalation["规则版本"] = rule_version(rule)
        escalation["消息内容"] = _message(rule, event)
        escalation["终止原因"] = None
        event["通知状态"] = "已通知"
        return escalation, f"SLA升级已通过{channel.get('渠道类型')}渠道发送"
