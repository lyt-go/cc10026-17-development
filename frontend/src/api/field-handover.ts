import { request, fetchJson } from './client'

export interface HandoverRef {
  规则编号: string
  规则版本: string
  规则状态: string
  过滤字段: string
  原过滤字段: string
  过滤条件: string
  空值处理: string
  引用出处: string
  修订状态: string
  新过滤字段: string
  修订人: string
  修订时间: string
}

export interface HandoverRow {
  handover_id: string
  交接编号: string
  关联API: string
  来源列: string
  旧模板字段: string
  新模板字段: string
  旧模板编号: string
  旧模板版本: string
  新模板编号: string
  新模板版本: string
  版本映射编号: string
  是否更名: string
  别名冲突组: string
  规则引用: HandoverRef[]
  启用判断: string
  阻断原因: string[]
  别名冲突已确认: string
  确认状态: string
  确认人: string
  确认时间: string
}

export interface HandoverSummary {
  ok: boolean
  handover_no: string
  api: string
  old_template: string
  new_template: string
  total: number
  renamed_count: number
  conflict_group_count: number
  referenced_count: number
  blocked_count: number
  confirmed_count: number
  rows: HandoverRow[]
}

export interface RuleTrace {
  规则编号: string
  规则状态: string
  当前过滤字段: string
  对应来源列: string
  修订后过滤字段: string
  修订状态: string
  落点_A_当前: { 过滤字段: string; 命中行数: number; 命中结算单号: string[] }
  落点_B_未改规则: { 过滤字段: string; 命中行数: number; 命中结算单号: string[] }
  落点_C_修订后: { 过滤字段: string; 命中行数: number; 命中结算单号: string[] }
}

export interface RehearseReport {
  ok: boolean
  handover_no: string
  api: string
  sample_size: number
  sample_id_field: string
  scenarios: Record<string, { 参与规则: string[]; 通过行数: number; 通过结算单号: string[] }>
  rule_trace: RuleTrace[]
}

export interface ApiResult {
  ok: boolean
  message: string
  row?: HandoverRow
  old_field?: string
  new_field?: string
}

async function postJson(path: string, body: Record<string, unknown>): Promise<ApiResult> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  return (await response.json()) as ApiResult
}

export async function loadHandover(): Promise<HandoverSummary> {
  return fetchJson<HandoverSummary>('/api/field-handover/handover')
}

export async function loadRehearse(): Promise<RehearseReport> {
  return fetchJson<RehearseReport>('/api/field-handover/rehearse')
}

export async function acknowledgeAlias(handoverId: string, reviewer: string): Promise<ApiResult> {
  const params = new URLSearchParams({ handover_id: handoverId })
  return postJson(`/api/field-handover/alias-conflict/acknowledge?${params}`, { reviewer })
}

export async function reviseRule(
  handoverId: string,
  ruleNo: string,
  reviewer: string,
): Promise<ApiResult> {
  return postJson('/api/field-handover/revise-rule', {
    handover_id: handoverId,
    rule_no: ruleNo,
    reviewer,
  })
}

export async function confirmHandoverRow(
  handoverId: string,
  reviewer: string,
  aliasAcknowledged: boolean,
): Promise<ApiResult> {
  return postJson(`/api/field-handover/handover/${handoverId}/confirm`, {
    reviewer,
    alias_acknowledged: aliasAcknowledged,
  })
}

function triggerDownload(path: string, filename: string): void {
  request(path)
    .then(async (response) => {
      const url = URL.createObjectURL(await response.blob())
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = filename
      document.body.appendChild(anchor)
      anchor.click()
      document.body.removeChild(anchor)
      URL.revokeObjectURL(url)
    })
    .catch(() => undefined)
}

export function downloadHandoverCsv(): void {
  triggerDownload('/api/field-handover/handover/export', 'field_handover.csv')
}

export function downloadRehearseCsv(): void {
  triggerDownload('/api/field-handover/rehearse/export', 'field_handover_rehearse.csv')
}
