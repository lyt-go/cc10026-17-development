"""格式字段映射交接表接口。

路由固定在通用资源路由 /api/{module} 之前注册，避免被动态路径抢占。
所有生产规则改动只能通过 /revise-rule 显式触发，交接表生成与确认本身不改规则。
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.services.field_handover import field_handover_service

router = APIRouter(prefix="/api/field-handover", tags=["格式字段映射交接"])


class ConfirmPayload(BaseModel):
    reviewer: str = Field(min_length=1, description="确认人，用于交接留痕")
    alias_acknowledged: bool = Field(default=False, description="是否显式确认别名冲突归属")


class RevisePayload(BaseModel):
    handover_id: str = Field(min_length=1)
    rule_no: str = Field(min_length=1)
    reviewer: str = Field(min_length=1, description="规则修订人，写入规则修订留痕")


class AliasAckPayload(BaseModel):
    reviewer: str = Field(min_length=1)


@router.get("/handover")
def get_handover() -> dict:
    """生成（或刷新）字段映射交接表，含别名冲突与规则引用核查结果。"""
    return field_handover_service.build_handover()


@router.get("/handover/export")
def export_handover() -> StreamingResponse:
    """导出格式字段映射交接表 CSV，供数据过滤维护人下载核对。"""
    content = field_handover_service.handover_csv()
    return StreamingResponse(
        iter([content]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="field_handover.csv"'},
    )


@router.post("/handover/{handover_id}/confirm")
def confirm_handover_row(handover_id: str, payload: ConfirmPayload) -> dict:
    """确认交接行；阻断未解除（冲突未确认/引用未修订）时拒绝确认。"""
    return field_handover_service.confirm_row(
        handover_id, payload.reviewer, payload.alias_acknowledged
    )


@router.post("/alias-conflict/acknowledge")
def acknowledge_alias(payload: AliasAckPayload, handover_id: str) -> dict:
    """显式确认别名冲突归属，不修改任何生产规则。"""
    return field_handover_service.acknowledge_alias_conflict(handover_id, payload.reviewer)


@router.post("/revise-rule")
def revise_rule(payload: RevisePayload) -> dict:
    """修订已登记引用的过滤规则字段；这是生产规则改动的唯一入口。"""
    return field_handover_service.revise_rule_reference(
        payload.handover_id, payload.rule_no, payload.reviewer
    )


@router.get("/rehearse")
def rehearse() -> dict:
    """用原样本预演当前/直接换版/修订后三种情形的规则落点与通过行。"""
    return field_handover_service.rehearse()


@router.get("/rehearse/export")
def export_rehearse() -> StreamingResponse:
    """导出样本预演结果 CSV。"""
    content = field_handover_service.rehearse_csv()
    return StreamingResponse(
        iter([content]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="field_handover_rehearse.csv"'},
    )
