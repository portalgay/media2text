<!-- 文件名清洗在左侧栏；正则模板下拉的编辑/删除交互在此实现（原需求写在 ConfigPanel，与布局一致即可）。 -->
<template>
  <div class="filename-clean">
    <h3 class="fc-title">文件名清洗</h3>
    <a-divider class="fc-divider" />
    <a-form layout="vertical" class="fc-form compact-form">
      <a-form-item label="固定替换" class="fc-form-item">
        <div class="fc-inline">
          <a-select
            v-model:value="config.filename_temp_rules"
            mode="multiple"
            allow-clear
            size="small"
            class="fc-grow"
            placeholder="旧→新规则，可点 × 移除"
            :options="tempRuleSelectOptions"
            :max-tag-count="1"
          />
          <a-button size="small" type="primary" ghost @click="openFixModal">+ 添加</a-button>
        </div>
        <div class="hint-line">
          按顺序替换主文件名片段；「被替换值-&gt;替换值」，替换值空则删除。
        </div>
      </a-form-item>
      <a-form-item label="正则模板" class="fc-form-item">
        <div class="fc-inline">
          <a-select
            v-model:value="config.filename_selected_regex_ids"
            mode="multiple"
            allow-clear
            size="small"
            class="fc-grow"
            placeholder="多选，顺序即执行顺序"
            :options="regexDropdownOptions"
            :filter-option="filterRegexOption"
            option-label-prop="label"
            popup-class-name="fc-regex-select-dropdown"
          >
            <template #option="opt">
              <div class="regex-option-row">
                <div class="regex-option-main">
                  <div class="regex-opt-title">{{ opt.rule?.name || '(未命名)' }}</div>
                  <div v-if="opt.rule?.description" class="regex-opt-desc">{{ opt.rule.description }}</div>
                  <div class="regex-opt-sub">
                    <span class="regex-kind">{{ opt.rule?.rule_type === 'extract' ? '[提取]' : '[替换]' }}</span>
                    <span class="regex-pat">{{ opt.rule?.pattern }}</span>
                  </div>
                </div>
                <div class="regex-option-actions" @mousedown.stop>
                  <a-space :size="8" class="regex-act-space">
                    <a-button 
                      size="small" 
                      type="link" 
                      class="regex-act-btn"
                      @click.stop="handleEditRegexRule(opt.rule)"
                    >
                      编辑
                    </a-button>
                    <a-popconfirm
                      title="确定删除该模板？"
                      ok-text="删除"
                      ok-type="danger"
                      cancel-text="取消"
                      placement="left"
                      @confirm="handleDeleteRegexRule(opt.rule?.id)"
                    >
                      <a-button 
                        size="small" 
                        type="link" 
                        danger
                        class="regex-act-btn"
                        @click.stop
                      >
                        删除
                      </a-button>
                    </a-popconfirm>
                  </a-space>
                </div>
              </div>
            </template>
          </a-select>
          <a-button size="small" type="primary" ghost @click="openRegexModal">+ 模板</a-button>
        </div>
        <div class="hint-line">存 Redis；设置里单条正则仍最后执行。</div>
      </a-form-item>
    </a-form>

    <a-modal v-model:open="fixModalOpen" title="添加固定替换" ok-text="确定" destroy-on-close @ok="onFixModalOk">
      <a-form layout="vertical" class="fc-modal-form">
        <a-form-item label="被替换值" required>
          <a-input v-model:value="fixForm.from" placeholder="主文件名中要替换的连续文本" allow-clear />
        </a-form-item>
        <a-form-item label="替换值">
          <a-input v-model:value="fixForm.to" placeholder="留空表示删除被替换值" allow-clear />
        </a-form-item>
      </a-form>
    </a-modal>

    <a-modal
      v-model:open="regexModalOpen"
      title="新增正则模板"
      ok-text="保存到规则库"
      destroy-on-close
      :confirm-loading="regexSaving"
      @ok="onRegexModalOk"
    >
      <a-form layout="vertical" class="fc-modal-form">
        <a-form-item label="规则名称" required>
          <a-input v-model:value="regexForm.name" placeholder="便于识别的名称" allow-clear />
        </a-form-item>
        <a-form-item label="规则类型" required>
          <a-radio-group v-model:value="regexForm.rule_type" class="fc-radio-group">
            <a-radio value="replace">替换 / 删除（留空则删匹配段）</a-radio>
            <a-radio value="extract">提取捕获组</a-radio>
          </a-radio-group>
        </a-form-item>
        <a-form-item label="正则表达式" required>
          <a-input v-model:value="regexForm.pattern" placeholder="Python 正则" allow-clear />
        </a-form-item>
        <a-form-item v-if="regexForm.rule_type === 'replace'" label="替换为">
          <a-input v-model:value="regexForm.replacement" placeholder="留空表示删去匹配" allow-clear />
        </a-form-item>
        <a-form-item label="描述">
          <a-input v-model:value="regexForm.description" placeholder="可选说明" allow-clear />
        </a-form-item>
      </a-form>
    </a-modal>

    <a-modal
      v-model:open="editModalVisible"
      title="编辑正则模板"
      ok-text="保存"
      destroy-on-close
      :confirm-loading="editSaving"
      @ok="onEditModalOk"
      @cancel="onEditModalCancel"
    >
      <a-form v-if="editingRule" layout="vertical" class="fc-modal-form">
        <a-form-item label="规则名称" required>
          <a-input v-model:value="editingRule.name" placeholder="便于识别的名称" allow-clear />
        </a-form-item>
        <a-form-item label="规则类型" required>
          <a-radio-group v-model:value="editingRule.rule_type" class="fc-radio-group">
            <a-radio value="replace">替换 / 删除（留空则删匹配段）</a-radio>
            <a-radio value="extract">提取捕获组</a-radio>
          </a-radio-group>
        </a-form-item>
        <a-form-item label="正则表达式" required>
          <a-input v-model:value="editingRule.pattern" placeholder="Python 正则" allow-clear />
        </a-form-item>
        <a-form-item v-if="editingRule.rule_type === 'replace'" label="替换为">
          <a-input v-model:value="editingRule.replacement" placeholder="留空表示删去匹配" allow-clear />
        </a-form-item>
        <a-form-item label="描述">
          <a-input v-model:value="editingRule.description" placeholder="可选说明" allow-clear />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { useConfigStore } from '@/stores/config.js'

const config = useConfigStore()

const tempRuleSelectOptions = computed(() =>
  (config.filename_temp_rules || []).map((line) => ({
    value: line,
    label: summarizeTempRule(line),
  }))
)

function summarizeTempRule(line) {
  const t = (line || '').trim()
  if (!t.includes('->')) return t || '(无效规则)'
  const i = t.indexOf('->')
  const a = t.slice(0, i)
  const b = t.slice(i + 2)
  const right = b === '' ? '∅' : b
  return `${a} → ${right}`
}

const fixModalOpen = ref(false)
const fixForm = reactive({ from: '', to: '' })

function openFixModal() {
  fixForm.from = ''
  fixForm.to = ''
  fixModalOpen.value = true
}

function onFixModalOk() {
  const from = (fixForm.from || '').trim()
  if (!from) {
    message.warning('请填写被替换值')
    return Promise.reject()
  }
  const to = fixForm.to ?? ''
  const line = `${from}->${to}`
  const list = config.filename_temp_rules || []
  if (list.includes(line)) {
    message.warning('该规则已存在')
    return Promise.reject()
  }
  config.filename_temp_rules = [...list, line]
  fixModalOpen.value = false
  message.success('已添加固定替换')
}

const regexOptionMap = computed(() => {
  const m = {}
  for (const r of config.filename_regex_library || []) {
    if (r && r.id) m[r.id] = r
  }
  return m
})

const enabledRegexLibrary = computed(() =>
  (config.filename_regex_library || []).filter((r) => r && r.enabled !== false)
)

/** 下拉 dataSource：附带 rule 供 #option 与筛选 */
const regexDropdownOptions = computed(() =>
  enabledRegexLibrary.value.map((r) => ({
    value: r.id,
    label: r.name || r.pattern || r.id,
    rule: r,
  }))
)

function filterRegexOption(input, option) {
  const r = option?.rule || regexOptionMap.value[option?.value]
  if (!r) return false
  const q = (input || '').trim().toLowerCase()
  if (!q) return true
  return (
    (r.name || '').toLowerCase().includes(q) ||
    (r.pattern || '').toLowerCase().includes(q) ||
    (r.description || '').toLowerCase().includes(q)
  )
}

const editModalVisible = ref(false)
const editSaving = ref(false)
/** 当前编辑中的规则副本（弹窗表单绑定） */
const editingRule = ref(null)

function handleEditRegexRule(rule) {
  if (!rule?.id) return
  editingRule.value = {
    id: rule.id,
    name: rule.name ?? '',
    rule_type: rule.rule_type === 'extract' ? 'extract' : 'replace',
    pattern: rule.pattern ?? '',
    replacement: rule.replacement ?? '',
    description: rule.description ?? '',
    enabled: rule.enabled !== false,
  }
  editModalVisible.value = true
}

async function handleDeleteRegexRule(ruleId) {
  if (!ruleId) return
  const lib = (config.filename_regex_library || []).filter((r) => r.id !== ruleId)
  const ids = (config.filename_selected_regex_ids || []).filter((id) => id !== ruleId)
  try {
    await config.saveConfig({
      filename_regex_library: lib,
      filename_selected_regex_ids: ids,
    })
    message.success('已删除模板')
  } catch (e) {
    message.error(e?.response?.data?.detail || e.message || String(e))
  }
}

function onEditModalCancel() {
  editingRule.value = null
}

async function onEditModalOk() {
  const er = editingRule.value
  if (!er?.id) return Promise.reject()
  const name = (er.name || '').trim()
  const pattern = (er.pattern || '').trim()
  if (!name) {
    message.warning('请填写规则名称')
    return Promise.reject()
  }
  if (!pattern) {
    message.warning('请填写正则表达式')
    return Promise.reject()
  }
  const idx = (config.filename_regex_library || []).findIndex((r) => r.id === er.id)
  if (idx < 0) {
    message.error('规则不存在或已删除')
    return Promise.reject()
  }
  const prev = config.filename_regex_library[idx]
  const updated = {
    ...prev,
    name,
    pattern,
    replacement: er.rule_type === 'extract' ? '' : er.replacement ?? '',
    rule_type: er.rule_type === 'extract' ? 'extract' : 'replace',
    description: (er.description || '').trim(),
    enabled: er.enabled !== false,
  }
  const lib = [...config.filename_regex_library]
  lib[idx] = updated
  editSaving.value = true
  try {
    await config.saveConfig({ filename_regex_library: lib })
    editModalVisible.value = false
    editingRule.value = null
    message.success('已保存')
  } catch (e) {
    message.error(e?.response?.data?.detail || e.message || String(e))
    throw e
  } finally {
    editSaving.value = false
  }
}

const regexModalOpen = ref(false)
const regexSaving = ref(false)
const regexForm = reactive({
  name: '',
  rule_type: 'replace',
  pattern: '',
  replacement: '',
  description: '',
})

function openRegexModal() {
  regexForm.name = ''
  regexForm.rule_type = 'replace'
  regexForm.pattern = ''
  regexForm.replacement = ''
  regexForm.description = ''
  regexModalOpen.value = true
}

function newRuleId() {
  return `rule_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`
}

async function onRegexModalOk() {
  const name = (regexForm.name || '').trim()
  const pattern = (regexForm.pattern || '').trim()
  if (!name) {
    message.warning('请填写规则名称')
    return Promise.reject()
  }
  if (!pattern) {
    message.warning('请填写正则表达式')
    return Promise.reject()
  }
  const newRule = {
    id: newRuleId(),
    name,
    pattern,
    replacement: regexForm.rule_type === 'extract' ? '' : regexForm.replacement ?? '',
    rule_type: regexForm.rule_type === 'extract' ? 'extract' : 'replace',
    description: (regexForm.description || '').trim(),
    enabled: true,
  }
  regexSaving.value = true
  try {
    const lib = [...(config.filename_regex_library || []), newRule]
    const ids = [...(config.filename_selected_regex_ids || []), newRule.id]
    await config.saveConfig({
      filename_regex_library: lib,
      filename_selected_regex_ids: ids,
    })
    regexModalOpen.value = false
    message.success('已保存模板并选中')
  } catch (e) {
    message.error(e?.response?.data?.detail || e.message || String(e))
    throw e
  } finally {
    regexSaving.value = false
  }
}

watch(
  () => [config.filename_selected_regex_ids, config.filename_regex_library],
  () => {
    const allowed = new Set(
      (config.filename_regex_library || [])
        .filter((r) => r && r.enabled !== false)
        .map((r) => r.id)
    )
    const ids = config.filename_selected_regex_ids || []
    const filtered = ids.filter((id) => allowed.has(id))
    if (filtered.length !== ids.length) {
      config.filename_selected_regex_ids = filtered
    }
  },
  { deep: true }
)
</script>

<style scoped>
.filename-clean {
  width: 100%;
  margin-top: 12px;
  padding-top: 4px;
  border-top: 1px solid rgba(99, 102, 241, 0.1);
  text-align: left;
}
.fc-title {
  margin: 0 0 2px;
  font-size: 13px;
  font-weight: 700;
  color: rgba(0, 0, 0, 0.78);
}
.fc-divider {
  margin: 6px 0 8px !important;
  border-color: rgba(99, 102, 241, 0.12) !important;
}
.fc-form :deep(.ant-form-item) {
  margin-bottom: 6px;
}
.fc-form :deep(.ant-form-item-label) {
  padding-bottom: 2px;
}
.fc-form :deep(.ant-form-item-label > label) {
  font-size: 12px;
  height: auto;
}
.fc-form-item :deep(.ant-form-item-control-input-content) {
  flex-direction: column;
  align-items: stretch;
}
.fc-inline {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  width: 100%;
}
.fc-grow {
  flex: 1;
  min-width: 0;
}
.hint-line {
  font-size: 11px;
  color: rgba(0, 0, 0, 0.45);
  margin-top: 4px;
  line-height: 1.35;
}
.regex-opt-title {
  font-size: 12px;
  font-weight: 600;
  color: rgba(0, 0, 0, 0.82);
}
.regex-opt-sub {
  margin-top: 2px;
  font-size: 11px;
  color: rgba(0, 0, 0, 0.45);
  line-height: 1.35;
  word-break: break-all;
}
.regex-kind {
  margin-right: 6px;
  color: #6366f1;
}
.regex-pat {
  font-family: ui-monospace, monospace;
}
.fc-modal-form :deep(.ant-form-item) {
  margin-bottom: 12px;
}
.fc-radio-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.fc-radio-group :deep(.ant-radio-wrapper) {
  align-items: flex-start;
  white-space: normal;
  line-height: 1.4;
}
.regex-option-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  width: 100%;
  min-width: 0;
  padding: 2px 0;
}
.regex-option-main {
  flex: 1;
  min-width: 0;
}
.regex-option-actions {
  flex-shrink: 0;
  padding-top: 2px;
  padding-left: 8px;
}
.regex-act-space {
  white-space: nowrap;
}
.regex-act-btn {
  padding: 0 6px !important;
  font-size: 12px;
  height: 24px;
  min-width: 40px;
}
.regex-opt-desc {
  font-size: 11px;
  color: rgba(0, 0, 0, 0.45);
  margin-top: 2px;
  line-height: 1.35;
  word-break: break-word;
}
</style>

<style>
/* 下拉挂载在 body */
.fc-regex-select-dropdown.ant-select-dropdown {
  min-width: min(360px, 92vw);
  max-width: min(520px, 96vw);
}
.fc-regex-select-dropdown .ant-select-item-option-content {
  padding-right: 8px;
}
</style>
