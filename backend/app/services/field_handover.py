"""格式字段映射交接领域服务。

解决模板换版时缺少交接文件的问题：
- 对比同一 API 的旧版/新版格式模板（result_format）的「字段映射」，
  逐条输出来源列 -> 旧模板字段 -> 新模板字段的映射交接表；
- 扫描现行数据过滤规则（data_filter），把仍在使用的旧模板字段引用挂到交接行上，
  使每条列关系都能回溯到模板版本、原字段与规则出处；
- 检测新模板中的别名冲突（两个不同来源列在新模板同名），冲突行不得抹掉任何一个
  有效规则引用；
- 未确认的引用不得直接触发生产规则改动：只有维护者在交接表上逐条确认后，
  才能调用规则修订把过滤字段改写到新模板字段；
- 提供原样本预演，分别核查当前规则落点、直接启用新映射的风险、修订后落点，
  维护者用同一批样本核对实际通过过滤的记录。
"""
from __future__ import annotations

import csv
import io
from typing import Any

from app.services.data_manager import write_module_snapshot
from app.store import store

# 交接场景固定锚定的 API（种子数据中的换版样例）
HANDOVER_API = "商户结算明细查询"
HANDOVER_NO = "HND-SETTLE-01"

# 交接表 CSV 列：列关系可回溯到新旧模板版本、原字段和规则出处
HANDOVER_COLUMNS = [
    "交接编号",
    "来源列",
    "旧模板字段",
    "新模板字段",
    "旧模板编号",
    "旧模板版本",
    "新模板编号",
    "新模板版本",
    "版本映射编号",
    "是否更名",
    "别名冲突组",
    "规则引用",
    "启用判断",
    "阻断原因",
    "别名冲突已确认",
    "确认状态",
    "确认人",
    "确认时间",
]

# 原样本：维护者核对过滤落点用的同一批数据（来源列键名即上游实际列名）
HANDOVER_SAMPLE_ROWS: list[dict[str, Any]] = [
    {"结算单号": "JS20261101001", "商户编号": "M001", "门店名称": "华东一号店",
     "结算金额": "1200.00", "优惠金额": "32.00", "实收净额": "1168.00", "结算时间": "2026-11-01"},
    {"结算单号": "JS20261101002", "商户编号": "M002", "门店名称": "华南二号店",
     "结算金额": "860.00", "优惠金额": "0.00", "实收净额": "860.00", "结算时间": "2026-11-01"},
    {"结算单号": "JS20261102003", "商户编号": "M001", "门店名称": "华东三号店",
     "结算金额": "2400.00", "优惠金额": "150.00", "实收净额": "2250.00", "结算时间": "2026-11-02"},
    {"结算单号": "JS20261102004", "商户编号": "M003", "门店名称": "华北一号店",
     "结算金额": "530.00", "优惠金额": "0.00", "实收净额": "530.00", "结算时间": "2026-11-02"},
    {"结算单号": "JS20261103005", "商户编号": "M002", "门店名称": "华南五号店",
     "结算金额": "970.00", "优惠金额": "88.00", "实收净额": "882.00", "结算时间": "2026-11-03"},
    {"结算单号": "JS20261103006", "商户编号": "M004", "门店名称": "西南一号店",
     "结算金额": "980.00", "优惠金额": "20.00", "实收净额": "960.00", "结算时间": "2026-11-03"},
]


def _parse_mapping(mapping_text: str) -> dict[str, str]:
    """解析模板「字段映射」文本（来源列:输出字段，逗号分隔）。"""
    result: dict[str, str] = {}
    for pair in (mapping_text or "").split(","):
        pair = pair.strip()
        if ":" not in pair:
            continue
        source, target = pair.split(":", 1)
        result[source.strip()] = target.strip()
    return result


class FieldHandoverService:
    """格式字段映射交接表的构建、确认门禁与样本预演。"""

    MODULE = "field_handover"
    ENTITY = "格式字段映射交接表"

    def __init__(self) -> None:
        # 交接行确认状态：handover_id -> 确认信息；规则修订状态挂在 data_filter 行上
        self._confirmations: dict[str, dict[str, str]] = {}
        self._last_handover: list[dict[str, Any]] | None = None

    def reset(self) -> None:
        """清空交接确认状态（规则修订留痕在 store 重建时自然清除）。"""
        self._confirmations.clear()
        self._last_handover = None

    # ------------------------------------------------------------------
    # 基础读取
    # ------------------------------------------------------------------
    def _templates(self) -> list[dict[str, Any]]:
        return [
            row for row in store.rows("result_format")
            if row.get("关联API") == HANDOVER_API
        ]

    def _filter_rules(self) -> list[dict[str, Any]]:
        """仍在生效或待启用的规则才纳入引用核查；已停用规则不再影响换版。"""
        return [
            row for row in store.rows("data_filter")
            if row.get("关联API") == HANDOVER_API and row.get("status") != "已停用"
        ]

    def _version_compat(self) -> dict[str, Any] | None:
        for row in store.rows("version_compat"):
            if row.get("关联API") == HANDOVER_API:
                return row
        return None

    # ------------------------------------------------------------------
    # 交接表构建
    # ------------------------------------------------------------------
    def build_handover(self) -> dict[str, Any]:
        """对比两版模板并关联现行规则，生成可回溯的字段映射交接表。"""
        templates = self._templates()
        old_template = next((t for t in templates if t.get("模板版本") == "V1.0"), None)
        new_template = next((t for t in templates if t.get("模板版本") == "V2.0"), None)
        if old_template is None or new_template is None:
            return {"ok": False, "message": "缺少旧版或新版格式模板，无法生成交接表", "rows": []}

        old_mapping = _parse_mapping(old_template.get("字段映射", ""))
        new_mapping = _parse_mapping(new_template.get("字段映射", ""))
        compat = self._version_compat()

        # 新模板输出字段 -> 来源列列表，用于发现别名冲突
        new_targets: dict[str, list[str]] = {}
        for source, target in new_mapping.items():
            new_targets.setdefault(target, []).append(source)

        # 别名冲突按基础名归并：净额(优惠抵扣)/净额(实收) 的基础名都是「净额」，
        # 括号只是人工消歧后缀，两个来源列在新模板语义上仍同名
        base_name_sources: dict[str, list[str]] = {}
        for target, sources in new_targets.items():
            base = target.split("(", 1)[0]
            base_name_sources.setdefault(base, []).extend(sources)

        rules = self._filter_rules()
        handover_rows: list[dict[str, Any]] = []
        # 新模板字段 -> 来源列：规则修订后过滤字段已改成新名，
        # 仍须通过来源列把规则引用挂回原交接行，不能因改名丢失出处
        new_field_sources = {field: source for source, field in new_mapping.items()}
        for index, source in enumerate(old_mapping, start=1):
            old_field = old_mapping[source]
            new_field = new_mapping.get(source, "")
            renamed = bool(new_field) and new_field != old_field
            base_name = new_field.split("(", 1)[0] if new_field else ""
            conflict_sources = base_name_sources.get(base_name, []) if base_name else []
            conflict_group = ""
            if len(conflict_sources) > 1:
                # 冲突组按新模板基础名命名，组内两个来源列的引用都必须保留
                conflict_group = f"CG-{base_name}"

            refs = self._collect_refs(
                source, old_field, new_field, rules, new_field_sources
            )
            prior_ack = self._confirmations.get(f"{HANDOVER_NO}-R{index:02d}", {}).get("alias_ack") == "是"
            blockers = self._evaluate_blockers(bool(conflict_group), refs, prior_ack)
            decision = "可启用" if not blockers else "暂缓启用"
            confirmation = self._confirmations.get(f"{HANDOVER_NO}-R{index:02d}", {})

            handover_rows.append({
                "handover_id": f"{HANDOVER_NO}-R{index:02d}",
                "交接编号": f"{HANDOVER_NO}-R{index:02d}",
                "关联API": HANDOVER_API,
                "来源列": source,
                "旧模板字段": old_field,
                "新模板字段": new_field,
                "旧模板编号": old_template.get("模板编号", ""),
                "旧模板版本": old_template.get("模板版本", ""),
                "新模板编号": new_template.get("模板编号", ""),
                "新模板版本": new_template.get("模板版本", ""),
                "版本映射编号": compat.get("映射编号", "") if compat else "",
                "是否更名": "是" if renamed else "否",
                "别名冲突组": conflict_group,
                "规则引用": refs,
                "启用判断": decision,
                "阻断原因": blockers,
                "别名冲突已确认": confirmation.get("alias_ack", "否"),
                "确认状态": confirmation.get("status", "未确认"),
                "确认人": confirmation.get("reviewer", ""),
                "确认时间": confirmation.get("confirmed_at", ""),
            })

        self._last_handover = handover_rows
        conflict_rows = [r for r in handover_rows if r["别名冲突组"]]
        referenced_rows = [r for r in handover_rows if r["规则引用"]]
        summary = {
            "ok": True,
            "handover_no": HANDOVER_NO,
            "api": HANDOVER_API,
            "old_template": old_template.get("模板编号", ""),
            "new_template": new_template.get("模板编号", ""),
            "total": len(handover_rows),
            "renamed_count": sum(1 for r in handover_rows if r["是否更名"] == "是"),
            "conflict_group_count": len({r["别名冲突组"] for r in conflict_rows}),
            "referenced_count": len(referenced_rows),
            "blocked_count": sum(1 for r in handover_rows if r["启用判断"] == "暂缓启用"),
            "confirmed_count": sum(1 for r in handover_rows if r["确认状态"] == "已确认"),
            "rows": handover_rows,
        }
        write_module_snapshot(self.MODULE, self.domain_snapshot(summary))
        return summary

    def _collect_refs(
        self,
        source: str,
        old_field: str,
        new_field: str,
        rules: list[dict[str, Any]],
        new_field_sources: dict[str, str],
    ) -> list[dict[str, Any]]:
        """把仍关联该来源列的过滤规则挂到交接行。

        未修订规则按旧模板字段匹配；已修订规则的过滤字段已是新模板字段，
        通过新模板字段回溯来源列匹配，确保别名冲突下两个引用都不丢失。
        """
        refs: list[dict[str, Any]] = []
        for rule in rules:
            filter_field = rule.get("过滤字段", "")
            matched = filter_field == old_field
            if not matched and rule.get("_handover_revision") == "已修订":
                matched = (
                    new_field_sources.get(filter_field) == source
                    and rule.get("_revised_field") == new_field
                )
            if not matched:
                continue
            refs.append({
                "规则编号": rule.get("规则编号", ""),
                "规则版本": rule.get("规则版本", ""),
                "规则状态": rule.get("status", ""),
                "过滤字段": filter_field,
                "原过滤字段": rule.get("_revised_from", filter_field),
                "过滤条件": rule.get("过滤条件", ""),
                "空值处理": rule.get("空值处理", ""),
                "引用出处": f"data_filter#{rule.get('id')}",
                "修订状态": rule.get("_handover_revision", "未修订"),
                "新过滤字段": rule.get("_revised_field", ""),
                "修订人": rule.get("_revised_by", ""),
                "修订时间": rule.get("_revised_at", ""),
            })
        return refs

    def _evaluate_blockers(
        self,
        has_conflict: bool,
        refs: list[dict[str, Any]],
        alias_acknowledged: bool = False,
    ) -> list[str]:
        blockers: list[str] = []
        active_refs = [r for r in refs if r["规则状态"] == "已生效"]
        unrevised = [r for r in refs if r["修订状态"] != "已修订"]
        if has_conflict and not alias_acknowledged:
            blockers.append("新模板别名冲突：与另一来源列同名，须确认两个有效引用的归属")
        if active_refs and unrevised:
            blockers.append("现行生效规则仍引用旧字段，须先完成规则修订再启用映射")
        elif refs and unrevised:
            blockers.append("待启用规则仍引用旧字段，启用前须修订或确认保留")
        return blockers

    # ------------------------------------------------------------------
    # 确认门禁与规则修订
    # ------------------------------------------------------------------
    def get_row(self, handover_id: str) -> dict[str, Any] | None:
        if self._last_handover is None:
            self.build_handover()
        assert self._last_handover is not None
        return next((r for r in self._last_handover if r["handover_id"] == handover_id), None)

    def confirm_row(
        self, handover_id: str, reviewer: str, alias_acknowledged: bool = False
    ) -> dict[str, Any]:
        """维护者确认交接行。存在未解除阻断时拒绝确认，且不会改动任何生产规则。"""
        handover = self.build_handover()
        row = next((r for r in handover["rows"] if r["handover_id"] == handover_id), None)
        if row is None:
            return {"ok": False, "message": f"交接行 {handover_id} 不存在"}

        if row["别名冲突组"] and not alias_acknowledged:
            return {"ok": False, "message": "存在别名冲突，须显式确认冲突组内两个引用的归属后才能确认"}

        unrevised_refs = [r for r in row["规则引用"] if r["修订状态"] != "已修订"]
        if unrevised_refs:
            labels = "、".join(r["规则编号"] for r in unrevised_refs)
            return {
                "ok": False,
                "message": f"规则 {labels} 的旧字段引用尚未修订，确认前不得启用该映射",
            }

        from datetime import datetime
        from app.services.data_manager import TZ
        confirmation = {
            "status": "已确认",
            "reviewer": reviewer,
            "confirmed_at": datetime.now(TZ).isoformat(),
            "alias_ack": "是" if row["别名冲突组"] else "不适用",
        }
        self._confirmations[handover_id] = confirmation
        self.build_handover()
        write_module_snapshot(self.MODULE, self.domain_snapshot())
        return {"ok": True, "message": f"交接行 {handover_id} 已确认，映射可纳入启用批次", "row": self.get_row(handover_id)}

    def revise_rule_reference(
        self, handover_id: str, rule_no: str, reviewer: str
    ) -> dict[str, Any]:
        """把指定过滤规则的过滤字段从旧模板字段修订为新模板字段。

        硬性约束：
        - 交接行必须存在；
        - 规则必须确实挂在该交接行的引用清单上（禁止修订未登记的引用）；
        - 别名冲突行须先显式确认别名归属，避免把两个净额引用改写到同一列；
        - 本接口是生产规则改动的唯一入口，未确认引用不会被任何流程自动改写。
        """
        handover = self.build_handover()
        row = next((r for r in handover["rows"] if r["handover_id"] == handover_id), None)
        if row is None:
            return {"ok": False, "message": f"交接行 {handover_id} 不存在，拒绝修订"}

        ref = next((r for r in row["规则引用"] if r["规则编号"] == rule_no), None)
        if ref is None:
            return {
                "ok": False,
                "message": f"规则 {rule_no} 未登记在交接行 {handover_id} 的引用清单，不得修订",
            }

        if row["别名冲突组"] and self._confirmations.get(handover_id, {}).get("alias_ack") != "是":
            return {
                "ok": False,
                "message": "别名冲突归属尚未确认，禁止把规则引用改写到存在歧义的新字段",
            }

        rule = next(
            (r for r in store.rows("data_filter") if r.get("规则编号") == rule_no), None
        )
        if rule is None:
            return {"ok": False, "message": f"过滤规则 {rule_no} 不存在"}

        old_field = rule.get("过滤字段", "")
        new_field = row["新模板字段"]
        if old_field == new_field:
            return {"ok": False, "message": f"规则 {rule_no} 的过滤字段已是新模板字段，无需修订"}

        from datetime import datetime
        from app.services.data_manager import TZ
        rule["过滤字段"] = new_field
        rule["_handover_revision"] = "已修订"
        rule["_revised_field"] = new_field
        rule["_revised_from"] = old_field
        rule["_revised_by"] = reviewer
        rule["_revised_at"] = datetime.now(TZ).isoformat()

        self.build_handover()
        write_module_snapshot(self.MODULE, self.domain_snapshot())
        return {
            "ok": True,
            "message": f"规则 {rule_no} 过滤字段已由「{old_field}」修订为「{new_field}」",
            "rule_no": rule_no,
            "old_field": old_field,
            "new_field": new_field,
        }

    def acknowledge_alias_conflict(self, handover_id: str, reviewer: str) -> dict[str, Any]:
        """显式确认别名冲突归属（不修改任何规则，仅解除修订前置门禁）。"""
        row = self.get_row(handover_id)
        if row is None:
            return {"ok": False, "message": f"交接行 {handover_id} 不存在"}
        if not row["别名冲突组"]:
            return {"ok": False, "message": f"交接行 {handover_id} 不存在别名冲突"}
        self._confirmations.setdefault(handover_id, {})["alias_ack"] = "是"
        self._confirmations[handover_id]["reviewer"] = reviewer
        self.build_handover()
        return {"ok": True, "message": f"别名冲突 {row['别名冲突组']} 归属已由 {reviewer} 确认"}

    # ------------------------------------------------------------------
    # 原样本预演
    # ------------------------------------------------------------------
    def rehearse(self) -> dict[str, Any]:
        """用同一批原样本核查三种情形下各规则的落点与通过过滤的行。

        三种情形全部从交接行登记的引用（来源列/旧字段/新字段/条件）推导，
        不依赖可能已被修订的实时规则，因此修订前后基线都可重建：
        - A 当前生产：生效规则按旧模板字段作用在原样本来源列上；
        - B 直接启用新映射、规则未改：样本已切到新模板列，规则仍读旧字段；
        - C 修订后：规则改读新模板字段，在新模板输出上执行。
        """
        handover = self.build_handover()
        rows = handover["rows"]
        new_mapping = {r["来源列"]: r["新模板字段"] for r in rows}
        renamed_rows = [
            {new_mapping.get(k, k): v for k, v in row.items()} for row in HANDOVER_SAMPLE_ROWS
        ]

        # 从交接引用构造规则规格：保留条件/空值处理/状态等原始出处信息
        ref_specs = self._reference_specs(rows)

        scenarios = {
            "A_当前生产_旧模板字段": self._run_scenario(
                HANDOVER_SAMPLE_ROWS, ref_specs, active_only=True, field_pick="old_field",
            ),
            "B_直接启用新映射_规则未改": self._run_scenario(
                renamed_rows, ref_specs, active_only=True, field_pick="old_field",
            ),
            "C_修订后_新模板字段": self._run_scenario(
                renamed_rows, ref_specs, active_only=True, field_pick="new_field",
            ),
        }

        # 每条登记规则在三种情形下的落点核查（含草稿规则，供启用前评估）
        rule_trace = []
        live_status = {r.get("规则编号"): r for r in self._filter_rules()}
        for spec in ref_specs:
            rule_no = spec["规则编号"]
            live = live_status.get(rule_no, {})
            rule_trace.append({
                "规则编号": rule_no,
                "规则状态": spec["规则状态"],
                "当前过滤字段": live.get("过滤字段", spec["old_field"]),
                "对应来源列": spec["source"],
                "修订后过滤字段": spec["new_field"],
                "修订状态": live.get("_handover_revision", "未修订"),
                "落点_A_当前": self._trace_spec(spec, HANDOVER_SAMPLE_ROWS, spec["old_field"]),
                "落点_B_未改规则": self._trace_spec(spec, renamed_rows, spec["old_field"]),
                "落点_C_修订后": self._trace_spec(spec, renamed_rows, spec["new_field"]),
            })

        report = {
            "ok": True,
            "handover_no": HANDOVER_NO,
            "api": HANDOVER_API,
            "sample_size": len(HANDOVER_SAMPLE_ROWS),
            "sample_id_field": "结算单号",
            "scenarios": scenarios,
            "rule_trace": rule_trace,
        }
        write_module_snapshot(self.MODULE, {"module": self.MODULE, "rehearse": report})
        return report

    def _reference_specs(self, handover_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把交接行上的规则引用摊平成预演规格，别名冲突下两个引用都会出现。"""
        specs: list[dict[str, Any]] = []
        for row in handover_rows:
            for ref in row["规则引用"]:
                specs.append({
                    "规则编号": ref["规则编号"],
                    "规则状态": ref["规则状态"],
                    "过滤条件": ref["过滤条件"],
                    "空值处理": ref["空值处理"],
                    "source": row["来源列"],
                    "old_field": row["旧模板字段"],
                    "new_field": row["新模板字段"],
                })
        return specs

    def _run_scenario(
        self,
        sample_rows: list[dict[str, Any]],
        specs: list[dict[str, Any]],
        *,
        active_only: bool,
        field_pick: str,
    ) -> dict[str, Any]:
        effective = [s for s in specs if not active_only or s["规则状态"] == "已生效"]
        passed = []
        for row in sample_rows:
            keep = True
            for spec in effective:
                # A/C 取实际存在的列；B 取旧字段名，新样本中该列缺失，由空值处理决定
                field = spec[field_pick]
                if not self._spec_matches(spec, row, field):
                    keep = False
                    break
            if keep:
                passed.append(row.get("结算单号", ""))
        return {
            "参与规则": [s["规则编号"] for s in effective],
            "通过行数": len(passed),
            "通过结算单号": passed,
        }

    def _trace_spec(
        self, spec: dict[str, Any], sample_rows: list[dict[str, Any]], field: str
    ) -> dict[str, Any]:
        matched = [
            row.get("结算单号", "") for row in sample_rows
            if self._spec_matches(spec, row, field)
        ]
        return {"过滤字段": field, "命中行数": len(matched), "命中结算单号": matched}

    @staticmethod
    def _spec_matches(spec: dict[str, Any], row: dict[str, Any], field: str) -> bool:
        """与 DataFilterService.apply_filters 保持同一套匹配语义。"""
        condition = spec.get("过滤条件", "")
        null_handling = spec.get("空值处理", "")
        value = row.get(field)
        if value is None or value == "":
            if null_handling == "排除空值":
                return False
            if null_handling == "保留空值":
                return True
        if condition and value:
            return condition.casefold() in str(value).casefold()
        return True

    # ------------------------------------------------------------------
    # 导出与快照
    # ------------------------------------------------------------------
    def handover_csv(self) -> str:
        handover = self.build_handover()
        output = io.StringIO()
        output.write("﻿")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(HANDOVER_COLUMNS)
        for row in handover["rows"]:
            refs = row["规则引用"]
            ref_text = "；".join(
                f"{r['规则编号']}({r['规则状态']})原引用[{r['原过滤字段']}]"
                f"现字段[{r['过滤字段']}]条件[{r['过滤条件']}]"
                f"出处{r['引用出处']}/{r['修订状态']}"
                for r in refs
            ) or "无规则引用"
            writer.writerow([
                row["交接编号"], row["来源列"], row["旧模板字段"], row["新模板字段"],
                row["旧模板编号"], row["旧模板版本"], row["新模板编号"], row["新模板版本"],
                row["版本映射编号"], row["是否更名"], row["别名冲突组"] or "无",
                ref_text, row["启用判断"], "；".join(row["阻断原因"]) or "无",
                row["别名冲突已确认"], row["确认状态"], row["确认人"], row["确认时间"],
            ])
        return output.getvalue()

    def rehearse_csv(self) -> str:
        report = self.rehearse()
        output = io.StringIO()
        output.write("﻿")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(["情形", "参与规则", "通过行数", "通过结算单号"])
        for name, detail in report["scenarios"].items():
            writer.writerow([
                name, "、".join(detail["参与规则"]) or "无",
                detail["通过行数"], "、".join(detail["通过结算单号"]) or "无",
            ])
        writer.writerow([])
        writer.writerow(["规则编号", "规则状态", "当前过滤字段", "对应来源列",
                         "修订后过滤字段", "修订状态",
                         "A命中", "B命中(规则未改)", "C命中(修订后)"])
        for trace in report["rule_trace"]:
            writer.writerow([
                trace["规则编号"], trace["规则状态"], trace["当前过滤字段"],
                trace["对应来源列"], trace["修订后过滤字段"], trace["修订状态"],
                "、".join(trace["落点_A_当前"]["命中结算单号"]) or "无",
                "、".join(trace["落点_B_未改规则"]["命中结算单号"]) or "无",
                "、".join(trace["落点_C_修订后"]["命中结算单号"]) or "无",
            ])
        return output.getvalue()

    def domain_snapshot(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if payload is not None:
            return payload
        handover = self.build_handover()
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "handover_no": HANDOVER_NO,
            "total": handover["total"],
            "blocked_count": handover["blocked_count"],
            "confirmed_count": handover["confirmed_count"],
        }


# 单例：路由与测试共享同一份确认状态
field_handover_service = FieldHandoverService()
