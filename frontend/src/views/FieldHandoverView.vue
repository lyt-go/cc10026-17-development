<template>
  <section class="page" data-module="field-handover">
    <header class="page-head">
      <div>
        <h2>格式字段映射交接</h2>
        <p class="page-desc">
          核对两版格式模板的来源列映射与现行过滤规则引用；别名冲突须先确认归属，
          未确认的引用不会触发生产规则改动。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="refresh-handover" type="button" @click="reloadAll">
          刷新交接表
        </button>
        <button class="btn" data-testid="export-handover" type="button" @click="downloadHandoverCsv">
          下载交接表CSV
        </button>
        <button class="btn" data-testid="export-rehearse" type="button" @click="downloadRehearseCsv">
          下载预演CSV
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">映射列总数</span>
        <strong class="stat-value">{{ summary?.total ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">更名列</span>
        <strong class="stat-value">{{ summary?.renamed_count ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">别名冲突组</span>
        <strong class="stat-value">{{ summary?.conflict_group_count ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">暂缓启用</span>
        <strong class="stat-value">{{ summary?.blocked_count ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已确认</span>
        <strong class="stat-value">{{ summary?.confirmed_count ?? 0 }}</strong>
      </article>
    </div>

    <p v-if="message" class="status-legend" data-testid="handover-message">{{ message }}</p>

    <table class="data-table" data-testid="handover-table">
      <thead>
        <tr>
          <th>交接编号</th>
          <th>来源列</th>
          <th>旧模板字段</th>
          <th>新模板字段</th>
          <th>模板版本</th>
          <th>版本映射</th>
          <th>更名</th>
          <th>别名冲突</th>
          <th>规则引用与出处</th>
          <th>启用判断</th>
          <th>确认状态</th>
          <th>交接操作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="row in rows"
          :key="row.handover_id"
          :data-testid="`handover-row-${row.handover_id}`"
          :data-decision="row.启用判断"
        >
          <td>{{ row.交接编号 }}</td>
          <td>{{ row.来源列 }}</td>
          <td>{{ row.旧模板字段 }}</td>
          <td>{{ row.新模板字段 }}</td>
          <td>{{ row.旧模板版本 }} → {{ row.新模板版本 }}</td>
          <td>{{ row.版本映射编号 }}</td>
          <td>{{ row.是否更名 }}</td>
          <td>
            <span v-if="row.别名冲突组" class="conflict-tag">
              {{ row.别名冲突组 }}（{{ row.别名冲突已确认 === '是' ? '归属已确认' : '待确认' }}）
            </span>
            <span v-else>—</span>
          </td>
          <td>
            <div v-if="row.规则引用.length" class="ref-list">
              <div v-for="ref in row.规则引用" :key="ref.规则编号" class="ref-item">
                <strong>{{ ref.规则编号 }}</strong>（{{ ref.规则状态 }}）
                原引用「{{ ref.原过滤字段 }}」<span v-if="ref.原过滤字段 !== ref.过滤字段">→ 现字段「{{ ref.过滤字段 }}」</span>
                条件「{{ ref.过滤条件 }}」
                <span class="ref-source">{{ ref.引用出处 }}</span>
                <span :class="['revision-tag', ref.修订状态 === '已修订' ? 'revised' : 'unrevised']">
                  {{ ref.修订状态 }}
                </span>
              </div>
            </div>
            <span v-else>无规则引用</span>
          </td>
          <td>
            <span :class="['decision-tag', row.启用判断 === '可启用' ? 'enabled' : 'blocked']">
              {{ row.启用判断 }}
            </span>
            <ul v-if="row.阻断原因.length" class="blocker-list">
              <li v-for="reason in row.阻断原因" :key="reason">{{ reason }}</li>
            </ul>
          </td>
          <td>
            {{ row.确认状态 }}
            <div v-if="row.确认人" class="ref-source">{{ row.确认人 }} {{ row.确认时间.slice(0, 10) }}</div>
          </td>
          <td class="row-actions">
            <button
              v-if="row.别名冲突组 && row.别名冲突已确认 !== '是'"
              class="link"
              type="button"
              :data-testid="`ack-${row.handover_id}`"
              @click="onAcknowledgeAlias(row)"
            >
              确认冲突归属
            </button>
            <button
              v-for="ref in row.规则引用.filter((item) => item.修订状态 !== '已修订')"
              :key="`rev-${ref.规则编号}`"
              class="link"
              type="button"
              :data-testid="`revise-${row.handover_id}-${ref.规则编号}`"
              @click="onReviseRule(row, ref.规则编号)"
            >
              修订引用 {{ ref.规则编号 }}
            </button>
            <button
              v-if="row.确认状态 !== '已确认'"
              class="link"
              type="button"
              :data-testid="`confirm-${row.handover_id}`"
              @click="onConfirm(row)"
            >
              确认交接行
            </button>
          </td>
        </tr>
      </tbody>
    </table>

    <h3 class="rehearse-title">原样本预演：启用新映射后通过过滤的行</h3>
    <p class="page-desc">同一批 {{ rehearse?.sample_size ?? 0 }} 条原样本，按结算单号核对三种情形的实际落点。</p>
    <table class="data-table" data-testid="rehearse-scenario-table">
      <thead>
        <tr><th>情形</th><th>参与规则</th><th>通过行数</th><th>通过结算单号</th></tr>
      </thead>
      <tbody>
        <tr
          v-for="(detail, name) in rehearse?.scenarios ?? {}"
          :key="name"
          :data-testid="`scenario-${name}`"
        >
          <td>{{ name }}</td>
          <td>{{ detail.参与规则.join('、') || '无' }}</td>
          <td>{{ detail.通过行数 }}</td>
          <td>{{ detail.通过结算单号.join('、') || '无' }}</td>
        </tr>
      </tbody>
    </table>

    <h3 class="rehearse-title">各规则落点核查</h3>
    <table class="data-table" data-testid="rule-trace-table">
      <thead>
        <tr>
          <th>规则编号</th>
          <th>规则状态</th>
          <th>当前过滤字段</th>
          <th>对应来源列</th>
          <th>修订后字段</th>
          <th>A 当前命中</th>
          <th>B 规则未改命中</th>
          <th>C 修订后命中</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="trace in rehearse?.rule_trace ?? []" :key="trace.规则编号">
          <td>{{ trace.规则编号 }}</td>
          <td>{{ trace.规则状态 }}</td>
          <td>{{ trace.当前过滤字段 }}</td>
          <td>{{ trace.对应来源列 }}</td>
          <td>{{ trace.修订后过滤字段 }}（{{ trace.修订状态 }}）</td>
          <td>{{ formatHits(trace.落点_A_当前.命中行数, trace.落点_A_当前.命中结算单号) }}</td>
          <td :class="{ 'risk-cell': trace.落点_B_未改规则.命中行数 !== trace.落点_A_当前.命中行数 }">
            {{ formatHits(trace.落点_B_未改规则.命中行数, trace.落点_B_未改规则.命中结算单号) }}
          </td>
          <td>{{ formatHits(trace.落点_C_修订后.命中行数, trace.落点_C_修订后.命中结算单号) }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  acknowledgeAlias,
  confirmHandoverRow,
  downloadHandoverCsv,
  downloadRehearseCsv,
  loadHandover,
  loadRehearse,
  reviseRule,
} from '@/api/field-handover'
import type { HandoverRow, HandoverSummary, RehearseReport } from '@/api/field-handover'

const REVIEWER = '数据过滤维护人'

const summary = ref<HandoverSummary | null>(null)
const rehearse = ref<RehearseReport | null>(null)
const message = ref('')
const rows = ref<HandoverRow[]>([])

function formatHits(count: number, ids: string[]): string {
  return `${count} 行：${ids.join('、') || '无'}`
}

async function reloadAll(): Promise<void> {
  message.value = ''
  try {
    const [handover, report] = await Promise.all([loadHandover(), loadRehearse()])
    summary.value = handover
    rows.value = handover.rows
    rehearse.value = report
  } catch (error) {
    message.value = error instanceof Error ? error.message : '交接数据读取失败'
  }
}

async function runAction(action: () => Promise<{ ok: boolean; message: string }>): Promise<void> {
  message.value = ''
  try {
    const result = await action()
    message.value = result.message
    await reloadAll()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '交接操作失败'
  }
}

function onAcknowledgeAlias(row: HandoverRow): void {
  void runAction(() => acknowledgeAlias(row.handover_id, REVIEWER))
}

function onReviseRule(row: HandoverRow, ruleNo: string): void {
  void runAction(() => reviseRule(row.handover_id, ruleNo, REVIEWER))
}

function onConfirm(row: HandoverRow): void {
  const hasConflict = Boolean(row.别名冲突组)
  void runAction(() => confirmHandoverRow(row.handover_id, REVIEWER, hasConflict))
}

onMounted(reloadAll)
</script>

<style scoped>
.conflict-tag {
  color: #b45309;
  font-weight: 600;
}

.decision-tag.enabled {
  color: #15803d;
  font-weight: 600;
}

.decision-tag.blocked {
  color: #b91c1c;
  font-weight: 600;
}

.blocker-list {
  margin: 4px 0 0;
  padding-left: 16px;
  color: #b91c1c;
  font-size: 12px;
}

.ref-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.ref-item {
  font-size: 12px;
}

.ref-source {
  color: #6b7280;
  font-size: 12px;
}

.revision-tag.revised {
  color: #15803d;
}

.revision-tag.unrevised {
  color: #b45309;
}

.rehearse-title {
  margin: 24px 0 8px;
}

.risk-cell {
  color: #b91c1c;
  font-weight: 600;
}
</style>
