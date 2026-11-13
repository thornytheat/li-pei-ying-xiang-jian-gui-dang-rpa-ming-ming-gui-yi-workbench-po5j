<template>
  <div class="slip">
    <div class="panel-head">
      <h3>断点与逐件进度</h3>
      <button type="button" class="btn btn--ghost btn--small" :disabled="loading" @click="reload">
        {{ loading ? '加载中…' : '刷新明细' }}
      </button>
    </div>
    <div class="panel-body stack">
      <p class="tiny note-line">
        批次停在扫描序号
        <span class="mono">{{ String(checkpoint?.checkpoint_seq ?? 0).padStart(3, '0') }}</span>
        之后；逐件结果来自批次 checkpoint，续跑时已落盘件按「已恢复复用」跳过、不重复写盘。
      </p>

      <ul v-if="tallyRows.length" class="tally-list">
        <li v-for="row in tallyRows" :key="row.key" :class="`tally--${row.key}`">
          <span class="tally__dot" aria-hidden="true"></span>
          <span class="tiny">{{ row.label }}</span>
          <span class="mono">{{ row.count }}</span>
        </li>
      </ul>

      <div class="table-scroll">
        <table class="ledger checkpoint-table">
          <thead>
            <tr>
              <th>序号</th>
              <th>原名</th>
              <th>归档名 / 处理结果</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="item in checkpoint?.items ?? []"
              :key="item.attachment_id"
              :class="{ 'row--untouched': item.outcome === 'untouched', 'row--reused': item.outcome === 'reused' || item.outcome === 'repaired' }"
            >
              <td class="mono">{{ String(item.scan_seq).padStart(3, '0') }}</td>
              <td class="mono small-cell">{{ item.original_name }}</td>
              <td class="small-cell">
                <span class="mono">{{ item.stored_name || '—' }}</span>
                <span class="tiny outcome-line">{{ item.outcome_label }}</span>
              </td>
              <td>
                <span class="tag" :class="outcomeTagClass(item.outcome)">{{ item.outcome_label }}</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="!checkpoint && !loading" class="tiny">选择一个批次后查看逐件进度。</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { RUN_ITEM_OUTCOME_LABELS, type RunItemOutcomeKey } from '../../constants/enums'
import { useRunsStore } from '../../stores/runsStore'

const runs = useRunsStore()
const { checkpoint, checkpointLoading: loading, selected } = storeToRefs(runs)

const TALLY_ORDER: RunItemOutcomeKey[] = [
  'archived',
  'reused',
  'repaired',
  'duplicate',
  'pending',
  'unknown',
  'unchanged',
  'untouched',
]

const tallyRows = computed(() => {
  const tally = checkpoint.value?.outcome_tally ?? {}
  return TALLY_ORDER.filter((key) => (tally[key] ?? 0) > 0).map((key) => ({
    key,
    label: RUN_ITEM_OUTCOME_LABELS[key],
    count: tally[key] ?? 0,
  }))
})

function outcomeTagClass(outcome: string): string {
  if (outcome === 'archived' || outcome === 'reused' || outcome === 'repaired') return 'tag--green'
  if (outcome === 'duplicate' || outcome === 'untouched') return 'tag--red'
  if (outcome === 'pending' || outcome === 'unknown') return 'tag--warn'
  return ''
}

async function reload() {
  if (selected.value) await runs.loadCheckpoint(selected.value.id)
}

watch(
  () => selected.value?.id,
  async (id) => {
    if (id) await runs.loadCheckpoint(id)
  },
  { immediate: true },
)
</script>

<style scoped>
.tally-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 8px 14px;
}

.tally-list li {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.tally__dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--ink-3);
}

.tally--archived .tally__dot,
.tally--reused .tally__dot,
.tally--repaired .tally__dot {
  background: var(--seal-green);
}

.tally--duplicate .tally__dot,
.tally--untouched .tally__dot {
  background: var(--seal-red);
}

.tally--pending .tally__dot,
.tally--unknown .tally__dot {
  background: var(--warn);
}

.table-scroll {
  max-height: 320px;
  overflow: auto;
  border: 1px solid var(--line);
}

.checkpoint-table {
  font-size: 0.72rem;
}

.small-cell {
  max-width: 22ch;
  word-break: break-all;
}

.outcome-line {
  display: block;
  color: var(--ink-3);
}

.row--untouched {
  background: oklch(62% 0.118 68 / 0.08);
}

.row--reused {
  background: var(--seal-green-soft);
}
</style>
