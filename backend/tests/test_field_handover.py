"""格式字段映射交接表专项测试。

覆盖换版交接的关键约束：
- 两版模板逐列对比、列关系可回溯到模板版本/版本映射/规则出处；
- 两个来源列在新模板同属「净额」引发别名冲突，两个有效规则引用都不得丢失；
- 未确认引用不得直接触发生产规则改动（修订接口是唯一入口且有前置门禁）；
- 原样本预演核查各规则落点，以及启用新映射后通过过滤的行；
- 交接表与预演结果均可导出 CSV。
"""
from __future__ import annotations

import io
import csv
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TEMP_DATA = tempfile.mkdtemp(prefix="cc_field_handover_")
os.environ["DATA_ROOT"] = TEMP_DATA

HANDOVER_IDS = [f"HND-SETTLE-01-R{i:02d}" for i in range(1, 8)]
R5 = "HND-SETTLE-01-R05"  # 优惠金额 -> 净额(优惠抵扣)，草稿规则 FTR-1002
R6 = "HND-SETTLE-01-R06"  # 实收净额 -> 净额(实收)，生效规则 FTR-1001
R7 = "HND-SETTLE-01-R07"  # 结算时间 -> 结算完成时间，单独改名、无规则引用


@pytest.fixture()
def service():
    """每个测试重建内存数据与交接服务，保证确认状态不串用。"""
    from app.seed import SEED_ROWS
    from app.store import store as s
    from app.services.field_handover import FieldHandoverService

    s._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    return FieldHandoverService()


def _row(handover, handover_id: str) -> dict:
    return next(r for r in handover["rows"] if r["handover_id"] == handover_id)


# ============================================================
# 1. 交接表生成与可回溯性
# ============================================================

class TestHandoverBuild:
    def test_covers_every_old_template_source_column(self, service):
        handover = service.build_handover()
        assert handover["ok"] is True
        assert handover["total"] == 7
        sources = {r["来源列"] for r in handover["rows"]}
        assert sources == {
            "结算单号", "商户编号", "门店名称", "结算金额",
            "优惠金额", "实收净额", "结算时间",
        }

    def test_row_traces_back_to_template_versions_and_compat(self, service):
        handover = service.build_handover()
        row = _row(handover, R6)
        assert row["旧模板编号"] == "FMT-SETTLE-V1"
        assert row["旧模板版本"] == "V1.0"
        assert row["新模板编号"] == "FMT-SETTLE-V2"
        assert row["新模板版本"] == "V2.0"
        assert row["版本映射编号"] == "VERS-SETTLE-01"
        assert row["旧模板字段"] == "实收净额"
        assert row["新模板字段"] == "净额(实收)"

    def test_renamed_and_unchanged_fields_classified(self, service):
        handover = service.build_handover()
        renamed = {r["来源列"] for r in handover["rows"] if r["是否更名"] == "是"}
        unchanged = {r["来源列"] for r in handover["rows"] if r["是否更名"] == "否"}
        assert renamed == {"优惠金额", "实收净额", "结算时间"}
        assert "结算单号" in unchanged

    def test_single_rename_without_reference_is_enabled(self, service):
        # 单独改名的「结算时间」没有规则引用，可先启用
        row = _row(service.build_handover(), R7)
        assert row["是否更名"] == "是"
        assert row["别名冲突组"] == ""
        assert row["启用判断"] == "可启用"
        assert row["阻断原因"] == []


# ============================================================
# 2. 别名冲突：两个净额引用都必须保留
# ============================================================

class TestAliasConflict:
    def test_two_net_amount_columns_share_one_conflict_group(self, service):
        handover = service.build_handover()
        assert handover["conflict_group_count"] == 1
        r5 = _row(handover, R5)
        r6 = _row(handover, R6)
        assert r5["别名冲突组"] == "CG-净额"
        assert r6["别名冲突组"] == "CG-净额"

    def test_conflict_keeps_both_valid_rule_references(self, service):
        # 关键约束：冲突不能抹掉任何一个有效引用
        handover = service.build_handover()
        r5_refs = [r["规则编号"] for r in _row(handover, R5)["规则引用"]]
        r6_refs = [r["规则编号"] for r in _row(handover, R6)["规则引用"]]
        assert r5_refs == ["FTR-1002"]
        assert r6_refs == ["FTR-1001"]

    def test_rule_reference_carries_source_trace(self, service):
        row = _row(service.build_handover(), R6)
        ref = row["规则引用"][0]
        assert ref["引用出处"] == "data_filter#4"
        assert ref["规则版本"] == "R1.0"
        assert ref["规则状态"] == "已生效"
        assert ref["过滤条件"] == "88"
        assert ref["修订状态"] == "未修订"

    def test_conflict_rows_blocked_until_acknowledged(self, service):
        handover = service.build_handover()
        for handover_id in (R5, R6):
            row = _row(handover, handover_id)
            assert row["启用判断"] == "暂缓启用"
            assert any("别名冲突" in b for b in row["阻断原因"])

    def test_acknowledge_alias_clears_conflict_blocker_but_keeps_rule_blocker(self, service):
        service.acknowledge_alias_conflict(R6, "维护人甲")
        row = _row(service.build_handover(), R6)
        assert not any("别名冲突" in b for b in row["阻断原因"])
        # 生效规则引用尚未修订，仍然阻断
        assert any("规则" in b for b in row["阻断原因"])

    def test_acknowledge_non_conflict_row_rejected(self, service):
        result = service.acknowledge_alias_conflict(R7, "维护人甲")
        assert result["ok"] is False


# ============================================================
# 3. 确认门禁与生产规则改动保护
# ============================================================

class TestConfirmationAndRevisionGuards:
    def test_confirm_conflict_row_without_alias_ack_rejected(self, service):
        result = service.confirm_row(R6, "维护人甲")
        assert result["ok"] is False
        assert "别名冲突" in result["message"]

    def test_confirm_row_with_unrevised_reference_rejected(self, service):
        service.acknowledge_alias_conflict(R6, "维护人甲")
        result = service.confirm_row(R6, "维护人甲", alias_acknowledged=True)
        assert result["ok"] is False
        assert "FTR-1001" in result["message"]

    def test_revision_blocked_before_alias_acknowledged(self, service):
        # 未确认别名归属前，禁止把规则改写到有歧义的新字段
        result = service.revise_rule_reference(R6, "FTR-1001", "维护人甲")
        assert result["ok"] is False
        rule = next(r for r in service._filter_rules() if r["规则编号"] == "FTR-1001")
        assert rule["过滤字段"] == "实收净额"  # 生产规则未被改动

    def test_revision_of_unregistered_rule_rejected(self, service):
        service.acknowledge_alias_conflict(R6, "维护人甲")
        result = service.revise_rule_reference(R6, "FTR-9999", "维护人甲")
        assert result["ok"] is False
        assert "未登记" in result["message"]

    def test_unknown_handover_row_rejected(self, service):
        result = service.revise_rule_reference("HND-NOPE-R99", "FTR-1001", "维护人甲")
        assert result["ok"] is False

    def test_revision_is_the_only_path_that_changes_production_rule(self, service):
        from app.store import store
        rule_before = next(r for r in store.rows("data_filter") if r["规则编号"] == "FTR-1001")
        assert rule_before["过滤字段"] == "实收净额"

        # 生成交接表、确认别名本身都不应改写生产规则
        service.build_handover()
        service.acknowledge_alias_conflict(R6, "维护人甲")
        rule_mid = next(r for r in store.rows("data_filter") if r["规则编号"] == "FTR-1001")
        assert rule_mid["过滤字段"] == "实收净额"
        assert "_handover_revision" not in rule_mid

        result = service.revise_rule_reference(R6, "FTR-1001", "维护人甲")
        assert result["ok"] is True
        rule_after = next(r for r in store.rows("data_filter") if r["规则编号"] == "FTR-1001")
        assert rule_after["过滤字段"] == "净额(实收)"
        assert rule_after["_handover_revision"] == "已修订"
        assert rule_after["_revised_from"] == "实收净额"
        assert rule_after["_revised_by"] == "维护人甲"

    def test_rule_reference_remains_traced_after_revision(self, service):
        # 回归：修订后过滤字段改名，交接行仍须挂住规则引用，不能丢失出处
        service.acknowledge_alias_conflict(R6, "维护人甲")
        service.revise_rule_reference(R6, "FTR-1001", "维护人甲")
        row = _row(service.build_handover(), R6)
        assert len(row["规则引用"]) == 1
        ref = row["规则引用"][0]
        assert ref["规则编号"] == "FTR-1001"
        assert ref["引用出处"] == "data_filter#4"
        assert ref["修订状态"] == "已修订"
        assert ref["原过滤字段"] == "实收净额"
        assert ref["过滤字段"] == "净额(实收)"
        assert ref["新过滤字段"] == "净额(实收)"
        assert "FTR-1001" in service.handover_csv()
        assert "原引用[实收净额]" in service.handover_csv()

    def test_full_revision_then_confirm_flow(self, service):
        service.acknowledge_alias_conflict(R5, "维护人甲")
        service.acknowledge_alias_conflict(R6, "维护人甲")
        assert service.revise_rule_reference(R6, "FTR-1001", "维护人甲")["ok"]
        assert service.revise_rule_reference(R5, "FTR-1002", "维护人乙")["ok"]

        c6 = service.confirm_row(R6, "维护人甲", alias_acknowledged=True)
        c5 = service.confirm_row(R5, "维护人乙", alias_acknowledged=True)
        assert c6["ok"] and c5["ok"]

        handover = service.build_handover()
        assert handover["confirmed_count"] == 2
        assert handover["blocked_count"] == 0
        for handover_id in (R5, R6):
            row = _row(handover, handover_id)
            assert row["启用判断"] == "可启用"
            assert row["确认状态"] == "已确认"
            assert row["阻断原因"] == []


# ============================================================
# 4. 原样本预演：规则落点与启用新映射后的通过行
# ============================================================

class TestRehearse:
    def test_scenario_a_current_production_baseline(self, service):
        report = service.rehearse()
        scenario_a = report["scenarios"]["A_当前生产_旧模板字段"]
        # 生效规则 FTR-1001 条件「88」命中 JS20261103005（实收净额 882.00）
        assert scenario_a["参与规则"] == ["FTR-1001"]
        assert scenario_a["通过行数"] == 1
        assert scenario_a["通过结算单号"] == ["JS20261103005"]

    def test_scenario_b_exposes_silent_breakage_if_mapping_enabled_first(self, service):
        report = service.rehearse()
        scenario_b = report["scenarios"]["B_直接启用新映射_规则未改"]
        # 规则仍读旧字段「实收净额」，但新模板输出已无该列 -> 空值排除 -> 0 行
        assert scenario_b["通过行数"] == 0
        assert scenario_b["通过结算单号"] == []

    def test_scenario_c_revision_restores_baseline_rows(self, service):
        service.acknowledge_alias_conflict(R6, "维护人甲")
        service.revise_rule_reference(R6, "FTR-1001", "维护人甲")
        report = service.rehearse()
        scenario_c = report["scenarios"]["C_修订后_新模板字段"]
        # 修订后落点与当前生产基线一致
        assert scenario_c["通过行数"] == 1
        assert scenario_c["通过结算单号"] == ["JS20261103005"]

    def test_rule_trace_covers_each_rule_landing_across_scenarios(self, service):
        report = service.rehearse()
        trace = {t["规则编号"]: t for t in report["rule_trace"]}

        ftr_1001 = trace["FTR-1001"]
        assert ftr_1001["对应来源列"] == "实收净额"
        assert ftr_1001["修订后过滤字段"] == "净额(实收)"
        assert ftr_1001["落点_A_当前"]["命中结算单号"] == ["JS20261103005"]
        assert ftr_1001["落点_B_未改规则"]["命中行数"] == 0
        assert ftr_1001["落点_C_修订后"]["命中结算单号"] == ["JS20261103005"]

        ftr_1002 = trace["FTR-1002"]
        assert ftr_1002["规则状态"] == "草稿"
        assert ftr_1002["对应来源列"] == "优惠金额"
        # 草稿规则条件「150」当前命中 1 行；换版后旧字段缺失按保留空值全放行（6 行）
        assert ftr_1002["落点_A_当前"]["命中结算单号"] == ["JS20261102003"]
        assert ftr_1002["落点_B_未改规则"]["命中行数"] == 6
        assert ftr_1002["落点_C_修订后"]["命中结算单号"] == ["JS20261102003"]

    def test_rehearse_baseline_stable_after_production_rule_revised(self, service):
        # 回归：规则修订后，三情形预演仍按交接登记的引用重建，结果与修订前一致
        before = service.rehearse()
        service.acknowledge_alias_conflict(R5, "维护人甲")
        service.acknowledge_alias_conflict(R6, "维护人甲")
        service.revise_rule_reference(R6, "FTR-1001", "维护人甲")
        service.revise_rule_reference(R5, "FTR-1002", "维护人乙")
        after = service.rehearse()

        for name in before["scenarios"]:
            assert after["scenarios"][name] == before["scenarios"][name]
        trace_before = {t["规则编号"]: t for t in before["rule_trace"]}
        trace_after = {t["规则编号"]: t for t in after["rule_trace"]}
        for rule_no in trace_before:
            for key in ("落点_A_当前", "落点_B_未改规则", "落点_C_修订后"):
                assert trace_after[rule_no][key] == trace_before[rule_no][key]
        # 实时规则字段已改，但预演登记的来源列与修订后字段保持完整
        assert trace_after["FTR-1001"]["当前过滤字段"] == "净额(实收)"
        assert trace_after["FTR-1001"]["修订状态"] == "已修订"
        assert trace_after["FTR-1001"]["对应来源列"] == "实收净额"

    def test_revised_production_filter_matches_rehearse_scenario_c(self, service):
        # 用真实 DataFilterService 复核：修订后在新模板列上得到的记录与预演一致
        from app.services.data_filter import DataFilterService
        from app.services.field_handover import HANDOVER_SAMPLE_ROWS

        service.acknowledge_alias_conflict(R6, "维护人甲")
        service.revise_rule_reference(R6, "FTR-1001", "维护人甲")

        handover = service.build_handover()
        new_mapping = {r["来源列"]: r["新模板字段"] for r in handover["rows"]}
        new_rows = [
            {new_mapping.get(k, k): v for k, v in row.items()} for row in HANDOVER_SAMPLE_ROWS
        ]
        passed = DataFilterService().apply_filters("商户结算明细查询", new_rows)
        assert [r["结算单号"] for r in passed] == ["JS20261103005"]


# ============================================================
# 5. CSV 导出
# ============================================================

class TestCsvExport:
    def test_handover_csv_contains_trace_columns_and_both_conflict_refs(self, service):
        content = service.handover_csv()
        reader = list(csv.reader(io.StringIO(content.lstrip("﻿"))))
        header = reader[0]
        for column in [
            "交接编号", "来源列", "旧模板字段", "新模板字段",
            "旧模板版本", "新模板版本", "版本映射编号", "规则引用", "启用判断",
        ]:
            assert column in header

        body = "\n".join(",".join(row) for row in reader[1:])
        assert "FTR-1001" in body and "FTR-1002" in body  # 两个冲突引用都在
        assert "CG-净额" in body
        assert "data_filter#4" in body

    def test_rehearse_csv_lists_three_scenarios(self, service):
        content = service.rehearse_csv()
        assert "A_当前生产_旧模板字段" in content
        assert "B_直接启用新映射_规则未改" in content
        assert "C_修订后_新模板字段" in content
        assert "JS20261103005" in content


# ============================================================
# 6. HTTP 接口
# ============================================================

class TestHttpApi:
    def test_full_workflow_over_http(self):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.seed import SEED_ROWS
        from app.store import store as s
        from app.services.field_handover import field_handover_service

        s._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
        field_handover_service.reset()
        client = TestClient(app)

        handover = client.get("/api/field-handover/handover").json()
        assert handover["total"] == 7
        assert handover["conflict_group_count"] == 1

        # 未确认别名直接修订 -> 422 业务失败（HTTP 200，ok=false）
        blocked = client.post(
            f"/api/field-handover/revise-rule",
            json={"handover_id": R6, "rule_no": "FTR-1001", "reviewer": "维护人甲"},
        ).json()
        assert blocked["ok"] is False

        # 别名确认 -> 修订 -> 交接行确认
        ack = client.post(
            f"/api/field-handover/alias-conflict/acknowledge?handover_id={R6}",
            json={"reviewer": "维护人甲"},
        ).json()
        assert ack["ok"] is True
        revised = client.post(
            "/api/field-handover/revise-rule",
            json={"handover_id": R6, "rule_no": "FTR-1001", "reviewer": "维护人甲"},
        ).json()
        assert revised["ok"] is True
        assert revised["new_field"] == "净额(实收)"

        confirmed = client.post(
            f"/api/field-handover/handover/{R6}/confirm",
            json={"reviewer": "维护人甲", "alias_acknowledged": True},
        ).json()
        assert confirmed["ok"] is True

        # 预演接口与导出
        rehearse = client.get("/api/field-handover/rehearse").json()
        assert rehearse["scenarios"]["C_修订后_新模板字段"]["通过结算单号"] == ["JS20261103005"]

        export_resp = client.get("/api/field-handover/handover/export")
        assert export_resp.status_code == 200
        assert "text/csv" in export_resp.headers["content-type"]
        assert "FTR-1001" in export_resp.content.decode("utf-8")

        rehearse_resp = client.get("/api/field-handover/rehearse/export")
        assert rehearse_resp.status_code == 200
        assert "B_直接启用新映射_规则未改" in rehearse_resp.content.decode("utf-8")

    def test_router_registered_before_wildcard_resource_route(self):
        # 固定前缀路由不能被 /api/{module} 抢占：直接请求验证命中交接路由
        # （若被资源路由抢占，会返回未知模块 404 而不是交接表 JSON）
        from fastapi.testclient import TestClient
        from app.main import app
        from app.seed import SEED_ROWS
        from app.store import store as s
        from app.services.field_handover import field_handover_service

        s._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
        field_handover_service.reset()
        response = TestClient(app).get("/api/field-handover/handover")
        assert response.status_code == 200
        assert response.json()["handover_no"] == "HND-SETTLE-01"
