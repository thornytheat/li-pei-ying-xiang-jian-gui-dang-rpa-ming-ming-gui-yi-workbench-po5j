<template>
  <div class="slip">
    <div class="panel-head">
      <h3>归档目录 × 批次台账对账</h3>
      <button type="button" class="btn btn--ghost btn--small" :disabled="loading" @click="runs.loadReconcile()">
        {{ loading ? '对账中…' : '开始逐件对账' }}
      </button>
    </div>
    <div class="panel-body stack">
      <p class="tiny note-line">
        只读核对：台账里每件已归档材料都应在归档目录有同名文件且 SHA-256 一致；不修改真实目录与台账。
      </p>

      <template v-if="report">
        <dl class="reconcile-summary">
          <div :class="{ 'is-ok': report.summary.clean }">
            <dt>账实一致</dt>
            <dd class="mono">{{ report.summary.matched }}</dd>
          </div>
          <div>
            <dt>台账有目录缺</dt>
            <dd class="mono bad-line">{{ report.summary.ledger_only }}</dd>
          </div>
          <div>
            <dt>内容不一致</dt>
            <dd class="mono bad-line">{{ report.summary.content_mismatch }}</dd>
          </div>
          <div>
            <dt>命名冲突</dt>
            <dd class="mono bad-line">{{ report.summary.name_mismatch }}</dd>
          </div>
          <div>
            <dt>目录有台账缺</dt>
            <dd class="mono bad-line">{{ report.summary.orphan_file }}</dd>
          </div>
        </dl>
        <p class="tiny">
          台账归档 {{ report.summary.ledger_archived }} 件 · 归档目录 {{ report.summary.disk_files }} 个文件 ·
          <span :class="report.summary.clean ? 'ok-line' : 'bad-line'">
            {{ report.summary.clean ? '全部账实相符' : `发现 ${report.summary.issue_total} 处差异` }}
          </span>
        </p>

        <div class="table-scroll">
          <table class="ledger reconcile-table">
            <thead>
              <tr>
                <th>对账结果</th>
                <th>档案名</th>
                <th>原名 / 说明</th>
                <th>SHA-256</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in report.rows" :key="`${row.kind}-${row.stored_name}`" :class="rowClass(row.status)">
                <td>
                  <span class="tag" :class="tagClass(row.status)">{{ statusLabel(row.status) }}</span>
                </td>
                <td class="mono small-cell">{{ row.stored_name }}</td>
                <td class="small-cell">
                  <span v-if="row.original_name" class="mono">{{ row.original_name }}</span>
                  <span class="tiny detail-line">{{ row.detail }}</span>
                </td>
                <td class="mono hash-cell">
                  <template v-if="row.status === 'content_mismatch'">
                    账 {{ row.ledger_sha256.slice(0, 10) }}<br />盘 {{ row.disk_sha256.slice(0, 10) }}
                  </template>
                  <template v-else>{{ (row.disk_sha256 || row.ledger_sha256).slice(0, 12) }}</template>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
      <p v-else class="tiny muted">尚未对账。点击右上角按钮，对归档目录与批次台账做一次逐件核对。</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { storeToRefs } from 'pinia'
import { RECONCILE_STATUS_LABELS, type ReconcileStatusKey } from '../../constants/enums'
import { useRunsStore } from '../../stores/runsStore'

const runs = useRunsStore()
const { reconcileReport: report, reconcileLoading: loading } = storeToRefs(runs)

function statusLabel(status: string): string {
  return RECONCILE_STATUS_LABELS[status as ReconcileStatusKey] ?? status
}

function tagClass(status: string): string {
  if (status === 'matched') return 'tag--green'
  return 'tag--red'
}

function rowClass(status: string): Record<string, boolean> {
  return {
    'row--issue': status !== 'matched',
    'row--matched': status === 'matched',
  }
}
</script>

<style scoped>
.reconcile-summary {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(9em, 1fr));
  gap: 8px;
  margin: 0;
}

.reconcile-summary div {
  border: 1px solid var(--line);
  padding: 8px 10px;
  background: var(--paper-2);
}

.reconcile-summary dt {
  font-size: 0.68rem;
  letter-spacing: 0.06em;
  color: var(--ink-3);
}

.reconcile-summary dd {
  margin: 2px 0 0;
  font-size: var(--step-1);
}

.reconcile-summary .is-ok {
  border-color: var(--seal-green);
}

.table-scroll {
  max-height: 340px;
  overflow: auto;
  border: 1px solid var(--line);
}

.reconcile-table {
  font-size: 0.72rem;
}

.small-cell {
  max-width: 24ch;
  word-break: break-all;
}

.hash-cell {
  font-size: 0.66rem;
  color: var(--ink-2);
}

.detail-line {
  display: block;
  color: var(--ink-3);
}

.row--issue {
  background: oklch(48% 0.148 27 / 0.07);
}

.ok-line {
  color: var(--seal-green);
}

.bad-line {
  color: var(--seal-red);
}
</style>
