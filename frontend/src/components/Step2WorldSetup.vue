<template>
  <div class="world-setup-panel">
    <div class="scroll-container">
      <div class="step-card" :class="{ active: phase === 0, completed: phase > 0 }">
        <div class="card-header">
          <div class="step-info">
            <span class="step-num">01</span>
            <span class="step-title">World Runtime 初始化</span>
          </div>
          <span class="badge" :class="phase > 0 ? 'success' : 'processing'">
            {{ phase > 0 ? '已创建' : '初始化' }}
          </span>
        </div>
        <div class="card-content">
          <p class="api-note">POST /api/simulation/prepare · GET /api/simulation/world-presets</p>
          <p class="description">先选 preset，再生成世界实体卡和推进配置。切换 preset 不会自动重建，必须显式执行。</p>

          <div class="preset-panel">
            <div class="preset-row">
              <label class="preset-field">
                <span class="field-label">Runtime Preset</span>
                <select
                  v-model="selectedPresetId"
                  class="preset-select"
                  :disabled="presetsLoading || isPreparing || !presetOptions.length"
                >
                  <option v-for="preset in presetOptions" :key="preset.id" :value="preset.id">
                    {{ preset.label }}
                  </option>
                </select>
              </label>
              <button class="secondary-btn" :disabled="!canRebuildPreset" @click="handleRebuildPreset">
                {{ presetActionLabel }}
              </button>
            </div>

            <div class="preset-state-row">
              <span class="state-pill" :class="presetDirty ? 'warning' : 'success'">
                {{ presetDirty ? '未应用到当前 runtime' : '当前 runtime 已对齐' }}
              </span>
              <span v-if="selectedPreset?.is_default" class="state-pill neutral">Registry 默认</span>
              <span v-if="selectedPreset?.strategy_class" class="state-pill neutral">
                {{ selectedPreset.strategy_class }}
              </span>
              <span v-if="isPreparing" class="state-pill neutral">
                {{ isRegenerating ? '重建中' : '准备中' }}
              </span>
            </div>

            <p class="preset-description">
              {{ selectedPreset?.description || '尚未获取 preset 信息，world prepare 会回退到后端默认 preset。' }}
            </p>
            <p v-if="selectedPreset?.recommendation" class="preset-recommendation">
              {{ selectedPreset.recommendation }}
            </p>
            <p v-if="presetError" class="preset-warning">{{ presetError }}</p>

            <div class="preset-metrics">
              <div class="metric-card">
                <span class="metric-label">已应用</span>
                <span class="metric-value">{{ appliedPresetLabel }}</span>
              </div>
              <div class="metric-card">
                <span class="metric-label">Actor Route</span>
                <span class="metric-value mono">{{ selectedActorSelector }}</span>
              </div>
              <div class="metric-card">
                <span class="metric-label">Resolver Route</span>
                <span class="metric-value mono">{{ selectedResolverSelector }}</span>
              </div>
              <div class="metric-card">
                <span class="metric-label">Runtime Overrides</span>
                <span class="metric-value">{{ runtimeOverrideCount }}</span>
              </div>
            </div>

            <div v-if="selectedPresetScoreCards.length" class="score-grid">
              <div v-for="item in selectedPresetScoreCards" :key="item.label" class="score-card">
                <span class="score-label">{{ item.label }}</span>
                <span class="score-value">{{ item.value }}</span>
              </div>
            </div>

            <div v-if="selectedPreset?.notes?.length" class="note-list">
              <div v-for="note in selectedPreset.notes" :key="note" class="note-item">{{ note }}</div>
            </div>
          </div>

          <div class="info-card">
            <div class="info-row">
              <span class="info-label">Simulation</span>
              <span class="info-value mono">{{ simulationId }}</span>
            </div>
            <div class="info-row">
              <span class="info-label">Mode</span>
              <span class="info-value">world</span>
            </div>
            <div class="info-row">
              <span class="info-label">Task</span>
              <span class="info-value mono">{{ taskId || 'preparing...' }}</span>
            </div>
            <div class="info-row">
              <span class="info-label">Applied Preset</span>
              <span class="info-value">{{ appliedPresetLabel }}</span>
            </div>
          </div>
        </div>
      </div>

      <div class="step-card" :class="{ active: phase === 1, completed: phase > 1 }">
        <div class="card-header">
          <div class="step-info">
            <span class="step-num">02</span>
            <span class="step-title">世界实体卡</span>
          </div>
          <span class="badge" :class="phase > 1 ? 'success' : phase === 1 ? 'processing' : 'pending'">
            {{ phase > 1 ? '已完成' : `${prepareProgress}%` }}
          </span>
        </div>
        <div class="card-content">
          <p class="api-note">GET /api/simulation/:id/profiles/realtime?platform=world</p>
          <p class="description">{{ prepareMessage }}</p>
          <div class="stats-grid">
            <div class="stat-card">
              <span class="stat-value">{{ profiles.length }}</span>
              <span class="stat-label">已生成实体卡</span>
            </div>
            <div class="stat-card">
              <span class="stat-value">{{ expectedTotal || '-' }}</span>
              <span class="stat-label">预期实体数</span>
            </div>
            <div class="stat-card">
              <span class="stat-value">{{ actorCount }}</span>
              <span class="stat-label">可行动主体</span>
            </div>
          </div>
          <div v-if="profiles.length > 0" class="profile-grid">
            <article v-for="profile in profiles.slice(0, 8)" :key="profile.entity_uuid || profile.agent_id" class="profile-card">
              <div class="profile-top">
                <span class="profile-name">{{ profile.entity_name }}</span>
                <span class="profile-type">{{ profile.entity_type }}</span>
              </div>
              <p class="profile-summary">{{ profile.summary || profile.core_identity || 'No summary' }}</p>
              <div class="profile-tags">
                <span v-for="tag in getProfileTags(profile)" :key="tag" class="tag">{{ tag }}</span>
              </div>
            </article>
          </div>
        </div>
      </div>

      <div class="step-card" :class="{ active: phase === 2, completed: phase > 2 }">
        <div class="card-header">
          <div class="step-info">
            <span class="step-num">03</span>
            <span class="step-title">推进节奏配置</span>
          </div>
          <span class="badge" :class="phase > 2 ? 'success' : config ? 'processing' : 'pending'">
            {{ phase > 2 ? '已完成' : (config ? '已生成' : '等待中') }}
          </span>
        </div>
        <div class="card-content">
          <p class="api-note">GET /api/simulation/:id/config/realtime</p>
          <div v-if="config" class="config-grid">
            <div class="config-card">
              <span class="config-label">总轮次</span>
              <span class="config-value">{{ totalRounds }}</span>
            </div>
            <div class="config-card">
              <span class="config-label">每轮时长</span>
              <span class="config-value">{{ config.time_config?.minutes_per_round || 60 }} min</span>
            </div>
            <div class="config-card">
              <span class="config-label">压力轨</span>
              <span class="config-value">{{ pressureCount }}</span>
            </div>
          </div>
          <div v-if="config?.preset" class="config-banner">
            <span class="config-banner-title">当前配置 preset</span>
            <span class="config-banner-value">
              {{ config.preset.label || config.preset.id }}
              <span class="mono">
                {{ config.runtime_config?.default_actor_llm_selector || selectedActorSelector }}
                /
                {{ config.runtime_config?.resolver_llm_selector || selectedResolverSelector }}
              </span>
            </span>
          </div>
          <div v-if="config?.plot_threads?.length" class="thread-list">
            <div v-for="thread in config.plot_threads.slice(0, 5)" :key="thread.title" class="thread-item">
              <span class="thread-title">{{ thread.title }}</span>
              <span class="thread-owner">{{ thread.owner }}</span>
            </div>
          </div>
          <div v-if="config?.world_rules?.length" class="rule-list">
            <div v-for="rule in config.world_rules.slice(0, 4)" :key="rule" class="rule-item">{{ rule }}</div>
          </div>
        </div>
      </div>

      <div class="step-card completed">
        <div class="card-header">
          <div class="step-info">
            <span class="step-num">04</span>
            <span class="step-title">进入推进模拟</span>
          </div>
        </div>
        <div class="card-content action-row">
          <div class="round-input">
            <label>自定义轮次</label>
            <input v-model.number="customMaxRounds" type="number" min="1" :placeholder="String(totalRounds || 12)" />
          </div>
          <button class="action-btn" :disabled="!readyToAdvance" @click="handleNextStep">
            进入 World Simulation →
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  getPrepareStatus,
  getSimulationConfigRealtime,
  getSimulationProfilesRealtime,
  getWorldPresets,
  prepareSimulation
} from '../api/simulation'

const props = defineProps({
  simulationId: String,
  projectData: Object,
  graphData: Object,
  systemLogs: Array
})

const emit = defineEmits(['go-back', 'next-step', 'add-log', 'update-status'])

const phase = ref(0)
const taskId = ref('')
const prepareProgress = ref(0)
const prepareMessage = ref('正在连接后端并准备 world runtime...')
const profiles = ref([])
const config = ref(null)
const expectedTotal = ref(null)
const customMaxRounds = ref(null)
const presetPayload = ref({ presets: [], default_preset: '' })
const selectedPresetId = ref('')
const appliedPresetId = ref('')
const presetsLoading = ref(false)
const presetError = ref('')
const isPreparing = ref(false)
const isRegenerating = ref(false)
const prepareRunId = ref(0)

let pollTimer = null

const actorTypePattern = /actor|agent|character|person|people|faction|organization|org|creature|house|guild|nation|kingdom|tribe|clan|order|council/i

const presetOptions = computed(() => presetPayload.value?.presets || [])
const registryDefaultPresetId = computed(() => {
  return presetPayload.value?.default_preset || presetOptions.value.find(item => item.is_default)?.id || ''
})
const selectedPreset = computed(() => {
  return presetOptions.value.find(item => item.id === selectedPresetId.value) || null
})
const appliedPreset = computed(() => {
  return presetOptions.value.find(item => item.id === appliedPresetId.value) || config.value?.preset || null
})
const actorCount = computed(() => {
  return profiles.value.filter(profile => actorTypePattern.test(profile.entity_type || '')).length
})
const totalRounds = computed(() => {
  return config.value?.time_config?.total_rounds || config.value?.time_config?.total_ticks || 12
})
const pressureCount = computed(() => config.value?.pressure_tracks?.length || 0)
const presetDirty = computed(() => {
  return Boolean(selectedPresetId.value && appliedPresetId.value && selectedPresetId.value !== appliedPresetId.value)
})
const readyToAdvance = computed(() => phase.value >= 3 && !!config.value && !isPreparing.value)
const canRebuildPreset = computed(() => {
  return Boolean(selectedPresetId.value || registryDefaultPresetId.value) && !presetsLoading.value && !isPreparing.value
})
const presetActionLabel = computed(() => {
  return presetDirty.value ? '切换并重建' : '按当前 Preset 重建'
})
const appliedPresetLabel = computed(() => {
  return appliedPreset.value?.label || appliedPreset.value?.id || '尚未应用'
})
const selectedActorSelector = computed(() => {
  return selectedPreset.value?.actor_selector || config.value?.runtime_config?.default_actor_llm_selector || 'WORLD_AGENT'
})
const selectedResolverSelector = computed(() => {
  return selectedPreset.value?.resolver_selector || config.value?.runtime_config?.resolver_llm_selector || 'WORLD_RESOLVER'
})
const runtimeOverrideCount = computed(() => {
  return Object.keys(selectedPreset.value?.runtime_overrides || {}).length
})
const selectedPresetScoreCards = computed(() => {
  const evaluation = selectedPreset.value?.evaluation || {}
  const cards = [
    { label: 'Overall', value: formatScore(evaluation.overall) },
    { label: 'Progression', value: formatScore(evaluation.progression) },
    { label: 'Resilience', value: formatScore(evaluation.resilience) },
    { label: 'Speed', value: formatScore(evaluation.speed) }
  ]

  return cards.filter(item => item.value !== '--')
})

const addLog = (message) => emit('add-log', message)

const formatScore = (value) => {
  const num = Number(value)
  return Number.isFinite(num) ? num.toFixed(1) : '--'
}

const stopPolling = () => {
  if (pollTimer) {
    window.clearInterval(pollTimer)
    pollTimer = null
  }
}

const getProfileTags = (profile) => {
  const tags = []
  if (profile.public_role) tags.push(profile.public_role)
  if (profile.home_location) tags.push(profile.home_location)
  if (profile.driving_goals?.length) tags.push(profile.driving_goals[0])
  return tags.slice(0, 3)
}

const syncPresetFromConfig = (configPayload, { syncSelectedPreset = false } = {}) => {
  const presetId = configPayload?.preset?.id || ''
  if (presetId) {
    appliedPresetId.value = presetId
    if (syncSelectedPreset || !selectedPresetId.value) {
      selectedPresetId.value = presetId
    }
    return
  }

  if (!selectedPresetId.value && registryDefaultPresetId.value) {
    selectedPresetId.value = registryDefaultPresetId.value
  }
}

const refreshArtifacts = async ({ syncSelectedPreset = false } = {}) => {
  const [profilesResult, configResult] = await Promise.allSettled([
    getSimulationProfilesRealtime(props.simulationId, 'world'),
    getSimulationConfigRealtime(props.simulationId)
  ])

  if (profilesResult.status === 'fulfilled') {
    const profilesRes = profilesResult.value
    if (profilesRes.success && profilesRes.data) {
      profiles.value = profilesRes.data.profiles || []
      expectedTotal.value = profilesRes.data.total_expected || expectedTotal.value
      if (profiles.value.length > 0) phase.value = Math.max(phase.value, 1)
    }
  }

  if (configResult.status === 'fulfilled') {
    const configRes = configResult.value
    if (configRes.success && configRes.data?.config) {
      config.value = configRes.data.config
      syncPresetFromConfig(config.value, { syncSelectedPreset })
      phase.value = Math.max(phase.value, 2)
    }
  }
}

const loadWorldPresets = async () => {
  presetsLoading.value = true
  presetError.value = ''

  try {
    const res = await getWorldPresets()
    presetPayload.value = res.data || { presets: [], default_preset: '' }

    if (!selectedPresetId.value) {
      selectedPresetId.value = appliedPresetId.value || registryDefaultPresetId.value || presetOptions.value[0]?.id || ''
    }
  } catch (error) {
    presetError.value = error.message || '加载 world preset 失败，将继续使用后端默认 preset。'
    addLog(`读取 world presets 失败: ${presetError.value}`)
  } finally {
    presetsLoading.value = false
  }
}

const finalizePreparation = async ({ syncSelectedPreset = true, message }) => {
  await refreshArtifacts({ syncSelectedPreset })
  phase.value = 3
  isPreparing.value = false
  isRegenerating.value = false
  emit('update-status', 'completed')
  stopPolling()
  addLog(typeof message === 'function' ? message() : message)
}

const failPreparation = (message) => {
  isPreparing.value = false
  isRegenerating.value = false
  prepareMessage.value = message
  emit('update-status', 'error')
  stopPolling()
  addLog(message)
}

const checkPrepareStatus = async (runId) => {
  if (runId !== prepareRunId.value) return

  try {
    const statusRes = await getPrepareStatus(
      taskId.value
        ? { task_id: taskId.value }
        : { simulation_id: props.simulationId }
    )

    if (runId !== prepareRunId.value || !statusRes.success || !statusRes.data) return

    prepareProgress.value = statusRes.data.progress || 0
    prepareMessage.value = statusRes.data.message || prepareMessage.value
    expectedTotal.value = statusRes.data.expected_entities_count || expectedTotal.value

    await refreshArtifacts()
    if (runId !== prepareRunId.value) return

    const status = statusRes.data.status
    if (['ready', 'completed'].includes(status)) {
      await finalizePreparation({
        message: () => {
          const presetLabel = appliedPresetLabel.value === '尚未应用' ? '' : ` (${appliedPresetLabel.value})`
          return `world runtime 准备完成${presetLabel}`
        }
      })
      return
    }

    if (status === 'failed') {
      failPreparation(`world runtime 准备失败: ${statusRes.data.error || prepareMessage.value || '未知错误'}`)
    }
  } catch (error) {
    if (runId !== prepareRunId.value) return
    addLog(`world runtime 状态轮询异常: ${error.message}`)
  }
}

const startPreparation = async ({
  forceRegenerate = false,
  presetId = null,
  logMessage = '开始准备 world runtime'
} = {}) => {
  if (!props.simulationId) {
    prepareMessage.value = 'simulationId 缺失，无法初始化 world runtime'
    emit('update-status', 'error')
    return
  }

  const resolvedPresetId = presetId || selectedPresetId.value || appliedPresetId.value || registryDefaultPresetId.value || null
  const runId = prepareRunId.value + 1
  prepareRunId.value = runId

  stopPolling()
  isPreparing.value = true
  isRegenerating.value = forceRegenerate
  prepareProgress.value = 0
  prepareMessage.value = forceRegenerate
    ? `正在按 ${selectedPreset.value?.label || resolvedPresetId || '默认 preset'} 重建 world runtime...`
    : '正在连接后端并准备 world runtime...'

  emit('update-status', 'processing')
  addLog(logMessage)

  try {
    const res = await prepareSimulation({
      simulation_id: props.simulationId,
      use_llm_for_profiles: true,
      parallel_profile_count: 3,
      force_regenerate: forceRegenerate,
      world_preset_id: resolvedPresetId
    })

    if (runId !== prepareRunId.value) return

    phase.value = 1
    taskId.value = res.data?.task_id || ''
    expectedTotal.value = res.data?.expected_entities_count || null

    if (res.data?.world_preset_id && !selectedPresetId.value) {
      selectedPresetId.value = res.data.world_preset_id
    }

    if (res.data?.already_prepared) {
      await finalizePreparation({
        message: () => `检测到已有 world runtime，直接复用 preset: ${appliedPresetLabel.value}`
      })
      return
    }

    await checkPrepareStatus(runId)
    if (runId !== prepareRunId.value || !isPreparing.value) return
    pollTimer = window.setInterval(() => checkPrepareStatus(runId), 2000)
  } catch (error) {
    if (runId !== prepareRunId.value) return
    failPreparation(`world runtime 初始化异常: ${error.message || '未知错误'}`)
  }
}

const handleRebuildPreset = async () => {
  await startPreparation({
    forceRegenerate: true,
    presetId: selectedPresetId.value || registryDefaultPresetId.value,
    logMessage: presetDirty.value
      ? `切换 world preset 并重建: ${selectedPreset.value?.label || selectedPresetId.value}`
      : `按当前 world preset 重建: ${selectedPreset.value?.label || selectedPresetId.value}`
  })
}

const handleNextStep = () => {
  emit('next-step', {
    maxRounds: customMaxRounds.value || totalRounds.value
  })
}

onMounted(async () => {
  await loadWorldPresets()
  await refreshArtifacts({ syncSelectedPreset: true })

  if (!selectedPresetId.value) {
    selectedPresetId.value = appliedPresetId.value || registryDefaultPresetId.value || presetOptions.value[0]?.id || ''
  }

  await startPreparation({
    presetId: selectedPresetId.value || registryDefaultPresetId.value,
    logMessage: '开始准备 world runtime'
  })
})

onUnmounted(() => {
  stopPolling()
})
</script>

<style scoped>
.world-setup-panel {
  height: 100%;
  overflow: hidden;
  background: #fff;
}

.scroll-container {
  height: 100%;
  overflow-y: auto;
  padding: 24px;
  display: grid;
  gap: 18px;
}

.step-card {
  border: 1px solid #e5e7eb;
  background: #fafaf9;
  padding: 18px;
}

.step-card.active {
  border-color: #111827;
  background: #fff;
}

.step-card.completed {
  background: #f8fafc;
}

.card-header, .step-info, .info-row, .profile-top, .thread-item, .action-row, .preset-row, .config-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.step-info {
  gap: 12px;
  justify-content: flex-start;
}

.step-num, .api-note, .mono {
  font-family: 'JetBrains Mono', monospace;
}

.step-num {
  font-size: 0.82rem;
  color: #6b7280;
}

.step-title {
  font-size: 1rem;
  font-weight: 600;
  color: #111827;
}

.badge {
  font-size: 0.75rem;
  padding: 4px 8px;
  border: 1px solid #d1d5db;
  color: #6b7280;
}

.badge.processing {
  border-color: #111827;
  color: #111827;
}

.badge.success {
  border-color: #166534;
  color: #166534;
}

.badge.pending {
  border-color: #d1d5db;
  color: #6b7280;
}

.api-note {
  color: #9ca3af;
  font-size: 0.76rem;
  margin-bottom: 8px;
}

.description {
  color: #4b5563;
  font-size: 0.92rem;
  line-height: 1.5;
}

.preset-panel,
.info-card,
.config-grid,
.stats-grid,
.profile-grid,
.thread-list,
.rule-list,
.preset-metrics,
.score-grid,
.note-list {
  margin-top: 14px;
}

.preset-panel {
  border: 1px solid #e5e7eb;
  background: #fff;
  padding: 14px;
  display: grid;
  gap: 12px;
}

.preset-row {
  gap: 12px;
  align-items: flex-end;
}

.preset-field {
  flex: 1;
  display: grid;
  gap: 6px;
}

.field-label {
  font-size: 0.76rem;
  color: #6b7280;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.preset-select,
.round-input input {
  border: 1px solid #d1d5db;
  padding: 10px 12px;
  font-size: 0.9rem;
  background: #fff;
}

.preset-state-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.state-pill {
  font-size: 0.72rem;
  padding: 4px 8px;
  border: 1px solid #d1d5db;
}

.state-pill.success {
  border-color: #166534;
  color: #166534;
}

.state-pill.warning {
  border-color: #b45309;
  color: #b45309;
}

.state-pill.neutral {
  color: #4b5563;
}

.preset-description,
.preset-recommendation,
.preset-warning {
  margin: 0;
  font-size: 0.86rem;
  line-height: 1.5;
}

.preset-description {
  color: #374151;
}

.preset-recommendation {
  color: #111827;
}

.preset-warning {
  color: #b91c1c;
}

.preset-metrics,
.score-grid,
.stats-grid,
.config-grid {
  display: grid;
  gap: 12px;
  grid-template-columns: repeat(4, minmax(0, 1fr));
}

.metric-card,
.score-card,
.stat-card,
.config-card {
  border: 1px solid #e5e7eb;
  background: #fff;
  padding: 14px;
}

.metric-label,
.score-label,
.stat-label,
.config-label {
  display: block;
  font-size: 0.78rem;
  color: #6b7280;
}

.metric-value,
.score-value,
.stat-value,
.config-value {
  display: block;
  margin-top: 6px;
  font-size: 1.1rem;
  font-weight: 700;
  color: #111827;
  word-break: break-word;
}

.info-card {
  display: grid;
  gap: 8px;
}

.info-row,
.thread-item,
.config-banner {
  border-bottom: 1px solid #e5e7eb;
  padding-bottom: 8px;
  font-size: 0.86rem;
  gap: 12px;
}

.info-label {
  color: #6b7280;
}

.info-value {
  color: #111827;
}

.profile-grid {
  display: grid;
  gap: 12px;
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.profile-card {
  border: 1px solid #e5e7eb;
  background: #fff;
  padding: 14px;
}

.profile-name {
  font-weight: 600;
  color: #111827;
}

.profile-type {
  font-size: 0.76rem;
  color: #6b7280;
  text-transform: uppercase;
}

.profile-summary {
  margin: 10px 0;
  font-size: 0.86rem;
  color: #4b5563;
  line-height: 1.5;
}

.profile-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.tag,
.rule-item,
.note-item {
  font-size: 0.74rem;
  border: 1px solid #d1d5db;
  padding: 4px 7px;
  color: #374151;
}

.thread-list,
.rule-list,
.note-list {
  display: grid;
  gap: 8px;
}

.thread-title,
.config-banner-title {
  color: #111827;
  font-weight: 600;
}

.thread-owner,
.config-banner-value {
  color: #6b7280;
}

.action-row {
  gap: 16px;
  flex-wrap: wrap;
}

.round-input {
  display: grid;
  gap: 6px;
  min-width: 180px;
}

.secondary-btn,
.action-btn {
  border: none;
  background: #111827;
  color: #fff;
  padding: 12px 18px;
  font-weight: 600;
  cursor: pointer;
}

.secondary-btn {
  white-space: nowrap;
}

.secondary-btn:disabled,
.action-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

@media (max-width: 1080px) {
  .preset-metrics,
  .score-grid,
  .stats-grid,
  .config-grid,
  .profile-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 900px) {
  .preset-row,
  .config-banner,
  .action-row {
    flex-direction: column;
    align-items: stretch;
  }

  .preset-metrics,
  .score-grid,
  .stats-grid,
  .config-grid,
  .profile-grid {
    grid-template-columns: 1fr;
  }
}
</style>
