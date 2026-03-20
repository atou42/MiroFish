<template>
  <div class="world-run-panel">
    <div class="control-bar">
      <div class="status-block">
        <div class="status-label">Concurrent World Runtime</div>
        <div class="status-metrics">
          <span class="metric">TICK <b>{{ runStatus.current_round || 0 }}/{{ runStatus.total_rounds || maxRounds || '-' }}</b></span>
          <span class="metric">ACTIVE <b>{{ activeEvents.length }}</b></span>
          <span class="metric">QUEUED <b>{{ queuedEvents.length }}</b></span>
          <span class="metric">DONE <b>{{ completedEventsCount }}</b></span>
          <span class="metric">DEFERRED <b>{{ deferredIntentsCount }}</b></span>
          <span class="metric">WAITS <b>{{ providerWaitsCount }}</b></span>
          <span class="metric">BLOCKED <b>{{ blockedTicksCount }}</b></span>
          <span class="metric">PHASE <b>{{ currentPhaseLabel }}</b></span>
          <span class="metric">STATE <b>{{ runStatus.runner_status || 'idle' }}</b></span>
        </div>
      </div>
      <div class="control-actions">
        <button
          v-if="canResume"
          class="action-btn action-btn--secondary"
          :disabled="isPausing || isResuming || isRestarting"
          @click="handleResumeSimulation"
        >
          {{ isResuming ? '继续中...' : '继续' }}
        </button>
        <button
          v-else
          class="action-btn action-btn--secondary"
          :disabled="!canPause || isPausing || isResuming || isRestarting"
          @click="handlePauseSimulation"
        >
          {{ isPausing ? '暂停中...' : '暂停' }}
        </button>
        <button
          class="action-btn action-btn--secondary"
          :disabled="!canRestart || isPausing || isResuming || isRestarting"
          @click="handleRestartSimulation"
        >
          {{ isRestarting ? '重启中...' : '重启' }}
        </button>
        <button class="action-btn" :disabled="!canGenerateReport || isGeneratingReport" @click="handleGenerateReport">
          {{ isGeneratingReport ? '启动中...' : '生成 World Report →' }}
        </button>
      </div>
    </div>

    <div v-if="phaseAlert" class="phase-alert" :class="phaseAlert.kind">
      {{ phaseAlert.message }}
    </div>

    <div class="hero-grid">
      <section class="snapshot-card">
        <div class="panel-head">
          <div>
            <div class="panel-label">世界快照</div>
            <div class="panel-title">Tick {{ latestTickLabel }}</div>
          </div>
          <div class="progress-badge">{{ progressPercent }}%</div>
        </div>
        <p class="summary-text">{{ latestSummary }}</p>

        <div class="snapshot-stats">
          <div class="snapshot-stat">
            <span class="label">Tension</span>
            <span class="value">{{ formatDecimal(worldState.tension) }}</span>
          </div>
          <div class="snapshot-stat">
            <span class="label">Stability</span>
            <span class="value">{{ formatDecimal(worldState.stability) }}</span>
          </div>
          <div class="snapshot-stat">
            <span class="label">Event Feed</span>
            <span class="value">{{ lifecycleFeed.length }}</span>
          </div>
          <div class="snapshot-stat">
            <span class="label">Completed</span>
            <span class="value">{{ completedEventsCount }}</span>
          </div>
        </div>

        <div v-if="pressureEntries.length" class="pressure-list">
          <div class="mini-title">Pressure Tracks</div>
          <div class="pressure-grid">
            <div v-for="[name, value] in pressureEntries" :key="name" class="pressure-chip">
              <span>{{ name }}</span>
              <b>{{ formatDecimal(value) }}</b>
            </div>
          </div>
        </div>

        <div v-if="focusThreads.length" class="focus-list">
          <div class="mini-title">Focus Threads</div>
          <div class="focus-tags">
            <span v-for="thread in focusThreads" :key="thread" class="focus-tag">{{ thread }}</span>
          </div>
        </div>
      </section>

      <section class="events-card">
        <div class="panel-head">
          <div>
            <div class="panel-label">并发事件面板</div>
            <div class="panel-title">Live Event Lanes</div>
          </div>
          <div class="stack-count">{{ activeEvents.length + queuedEvents.length }} tracked</div>
        </div>

        <div class="lane-grid">
          <div class="lane-column">
            <div class="lane-title">Active</div>
            <div v-if="!activeEvents.length" class="lane-empty">当前没有正在推进的事件。</div>
            <article v-for="event in activeEvents" :key="event.event_id" class="lane-card lane-card--active">
              <div class="lane-card-head">
                <strong>{{ event.title || 'Untitled Event' }}</strong>
                <span>T{{ event.resolves_at_tick || '-' }}</span>
              </div>
              <p>{{ event.summary || '暂无摘要' }}</p>
              <div class="lane-meta">
                <span>{{ formatParticipants(event.participants) }}</span>
                <span v-if="event.location">{{ event.location }}</span>
                <span v-if="event.resource">{{ event.resource }}</span>
                <span v-if="Number.isFinite(event.remaining_ticks)">剩余 {{ event.remaining_ticks }} tick</span>
              </div>
            </article>
          </div>

          <div class="lane-column">
            <div class="lane-title">Queued</div>
            <div v-if="!queuedEvents.length" class="lane-empty">没有排队中的事件。</div>
            <article v-for="event in queuedEvents" :key="event.event_id" class="lane-card lane-card--queued">
              <div class="lane-card-head">
                <strong>{{ event.title || 'Queued Event' }}</strong>
                <span>{{ event.status || 'queued' }}</span>
              </div>
              <p>{{ event.summary || '等待进入活跃队列' }}</p>
              <div class="lane-meta">
                <span>{{ formatParticipants(event.participants) }}</span>
                <span v-if="event.target">Target: {{ event.target }}</span>
                <span v-if="event.location">{{ event.location }}</span>
              </div>
            </article>
          </div>
        </div>
      </section>
    </div>

    <div class="feed-grid">
      <section class="tick-panel">
        <div class="panel-head">
          <div>
            <div class="panel-label">Tick Timeline</div>
            <div class="panel-title">World Evolution</div>
          </div>
          <div class="stack-count">{{ worldTimeline.length }} ticks</div>
        </div>

        <div v-if="!worldTimeline.length" class="timeline-empty">Waiting for tick snapshots...</div>
        <div v-else class="tick-list">
          <article v-for="tick in visibleTimeline" :key="tick.tick || tick.round_num" class="tick-card">
            <div class="tick-top">
              <strong>Tick {{ tick.tick || tick.round_num }}</strong>
              <span>{{ tick.active_events_count || 0 }} active / {{ tick.events_completed || 0 }} done</span>
            </div>
            <p>{{ tick.summary || '该 tick 尚未产出摘要。' }}</p>
            <div class="tick-metrics">
              <span>intent {{ tick.intent_created || 0 }}</span>
              <span>resolved {{ tick.intent_resolved || 0 }}</span>
              <span>deferred {{ tick.intent_deferred || 0 }}</span>
              <span>started {{ tick.events_started || 0 }}</span>
              <span>queued {{ tick.events_queued || tick.queued_events_count || 0 }}</span>
              <span>completed {{ tick.events_completed || 0 }}</span>
              <span v-if="tick.provider_waiting">waits {{ tick.provider_waiting }}</span>
              <span v-if="tick.ticks_blocked">blocked {{ tick.ticks_blocked }}</span>
            </div>
          </article>
        </div>
      </section>

      <section class="timeline-panel">
        <div class="panel-head">
          <div>
            <div class="panel-label">Lifecycle Feed</div>
            <div class="panel-title">Intent / Resolve / Event</div>
          </div>
          <div class="stack-count">{{ lifecycleFeed.length }} records</div>
        </div>

        <div v-if="!lifecycleFeed.length" class="timeline-empty">Waiting for world events...</div>
        <div v-else class="timeline-list">
          <article v-for="event in lifecycleFeed" :key="event._uniqueId" class="event-card" :class="eventCardClass(event.event_type, event.resolution_status)">
            <div class="event-top">
              <div>
                <div class="event-actor">{{ event.agent_name || event.title || 'World Event' }}</div>
                <div class="event-type">{{ formatEventType(event.event_type) }}</div>
              </div>
              <div class="event-round">T{{ event.tick || event.round || '-' }}</div>
            </div>
            <p class="event-content">{{ event.summary || event.description || event.title || 'No event detail' }}</p>
            <div class="event-meta">
              <span v-if="event.title && event.agent_name && event.title !== event.agent_name">{{ event.title }}</span>
              <span v-if="event.location">{{ event.location }}</span>
              <span v-if="event.resource">{{ event.resource }}</span>
              <span v-if="event.target">Target: {{ event.target }}</span>
              <span v-if="event.provider_role">Provider: {{ event.provider_role }}</span>
              <span v-if="event.wait_seconds">Wait {{ event.wait_seconds }}s</span>
              <span v-if="event.context">{{ event.context }}</span>
            </div>
          </article>
        </div>
      </section>
    </div>

    <div class="system-logs">
      <div class="log-header">
        <span class="log-title">WORLD MONITOR</span>
        <span class="log-id">{{ simulationId || 'NO_SIMULATION' }}</span>
      </div>
      <div class="log-content">
        <div class="log-line" v-for="(log, idx) in systemLogs" :key="idx">
          <span class="log-time">{{ log.time }}</span>
          <span class="log-msg">{{ log.msg }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { generateReport } from '../api/report'
import { getRunStatusDetail, pauseSimulation, resumeSimulation, startSimulation } from '../api/simulation'

const props = defineProps({
  simulationId: String,
  maxRounds: Number,
  minutesPerRound: {
    type: Number,
    default: 60
  },
  projectData: Object,
  graphData: Object,
  systemLogs: Array
})

const emit = defineEmits(['go-back', 'next-step', 'add-log', 'update-status'])

const router = useRouter()
const runStatus = ref({})
const isGeneratingReport = ref(false)
const isPausing = ref(false)
const isResuming = ref(false)
const isRestarting = ref(false)
let pollTimer = null

const addLog = (message) => emit('add-log', message)

const formatDecimal = (value) => {
  const num = Number(value)
  if (!Number.isFinite(num)) return '--'
  return num.toFixed(2)
}

const formatEventType = (value) => {
  return String(value || 'event').replace(/_/g, ' ').toUpperCase()
}

const formatParticipants = (participants = []) => {
  if (!Array.isArray(participants) || !participants.length) return 'No participants'
  return participants
    .slice(0, 3)
    .map(item => {
      if (typeof item === 'string') return item
      return item?.agent_name || item?.name || item?.primary_agent_name || 'Unknown'
    })
    .join(' / ')
}

const eventCardClass = (eventType, resolutionStatus) => {
  return {
    'event-card--intent': eventType === 'intent_created',
    'event-card--resolve': eventType === 'intent_resolved' && resolutionStatus !== 'queued',
    'event-card--start': eventType === 'event_started',
    'event-card--queue': eventType === 'event_queued' || (eventType === 'intent_resolved' && resolutionStatus === 'queued'),
    'event-card--done': eventType === 'event_completed',
    'event-card--warn': ['provider_waiting', 'tick_blocked', 'intent_deferred'].includes(eventType),
    'event-card--recover': eventType === 'provider_recovered'
  }
}

const progressPercent = computed(() => {
  const total = runStatus.value.total_rounds || props.maxRounds || 0
  if (!total) return 0
  return Math.round(((runStatus.value.current_round || 0) / total) * 100)
})

const latestSnapshot = computed(() => runStatus.value.latest_snapshot || {})

const worldState = computed(() => latestSnapshot.value.world_state || {})

const focusThreads = computed(() => worldState.value.focus_threads || [])

const pressureEntries = computed(() => {
  return Object.entries(worldState.value.pressure_levels || worldState.value.pressure_tracks || {})
    .sort((a, b) => String(a[0]).localeCompare(String(b[0])))
})

const activeEvents = computed(() => {
  return runStatus.value.world_active_events
    || latestSnapshot.value.active_events
    || []
})

const queuedEvents = computed(() => {
  return runStatus.value.world_queued_events
    || latestSnapshot.value.queued_events
    || []
})

const completedEventsCount = computed(() => {
  return runStatus.value.world_completed_events_count
    ?? latestSnapshot.value.metrics?.completed_events_count
    ?? 0
})

const providerWaitsCount = computed(() => {
  return runStatus.value.world_metrics?.provider_waits_count
    ?? runStatus.value.world_provider_waits_count
    ?? 0
})

const blockedTicksCount = computed(() => {
  return runStatus.value.world_metrics?.ticks_blocked_count
    ?? runStatus.value.world_ticks_blocked_count
    ?? 0
})

const deferredIntentsCount = computed(() => {
  return runStatus.value.world_metrics?.intents_deferred_count
    ?? runStatus.value.world_intents_deferred_count
    ?? 0
})

const currentPhaseLabel = computed(() => {
  return runStatus.value.world_current_phase
    || latestSnapshot.value.phase
    || 'idle'
})

const lifecycleFeed = computed(() => {
  const events = runStatus.value.world_event_feed
    || runStatus.value.world_events
    || runStatus.value.world_recent_events
    || []

  return events
    .filter(event => ['intent_created', 'intent_resolved', 'intent_deferred', 'event_started', 'event_queued', 'event_completed', 'provider_waiting', 'provider_recovered', 'tick_blocked'].includes(event.event_type))
    .map((event, index) => ({
      ...event,
      _uniqueId: event._uniqueId
        || event.id
        || event.event_id
        || `${event.timestamp || 'no-time'}-${event.event_type || 'event'}-${event.tick || event.round || 'na'}-${index}`
    }))
})

const latestLifecycleEvent = computed(() => lifecycleFeed.value[0] || null)

const worldTimeline = computed(() => runStatus.value.world_timeline || [])

const visibleTimeline = computed(() => {
  return [...worldTimeline.value]
    .sort((a, b) => (b.tick || b.round_num || 0) - (a.tick || a.round_num || 0))
    .slice(0, 16)
})

const latestSummary = computed(() => {
  return latestSnapshot.value.summary
    || worldState.value.last_tick_summary
    || worldState.value.last_round_summary
    || lifecycleFeed.value[0]?.summary
    || '等待第一批并发世界事件...'
})

const latestTickLabel = computed(() => {
  return latestSnapshot.value.tick || runStatus.value.current_round || 0
})

const phaseAlert = computed(() => {
  const phase = currentPhaseLabel.value
  const latestEvent = latestLifecycleEvent.value
  const providerRole = latestEvent?.provider_role
  const waitSeconds = latestEvent?.wait_seconds

  if (phase === 'tick_blocked' || (phase === 'waiting_provider' && providerRole === 'world_resolver' && blockedTicksCount.value > 0)) {
    const waitClause = waitSeconds ? `，按 ${waitSeconds}s 节奏重试` : ''
    return {
      kind: 'warn',
      message: `当前 tick 被 resolver 阻塞，累计阻塞 ${blockedTicksCount.value} 次${waitClause}。`
    }
  }

  if (phase === 'waiting_provider') {
    const providerLabel = providerRole === 'world_agent'
      ? 'world agent'
      : providerRole === 'world_resolver'
        ? 'world resolver'
        : 'provider'
    const waitClause = waitSeconds ? `，${waitSeconds}s 后重试` : ''
    return {
      kind: 'warn',
      message: `${providerLabel} 暂不可用，world runtime 正在等待恢复，不会切换模型继续生成${waitClause}。`
    }
  }

  if (phase === 'provider_recovered') {
    return {
      kind: 'info',
      message: 'Provider 已恢复，world runtime 已继续使用原模型推进。'
    }
  }

  return null
})

const canGenerateReport = computed(() => {
  return ['completed', 'stopped'].includes(runStatus.value.runner_status)
})

const canPause = computed(() => {
  return ['running', 'processing', 'starting', 'in_progress'].includes(runStatus.value.runner_status)
})

const canResume = computed(() => Boolean(runStatus.value.resume_supported))

const canRestart = computed(() => Boolean(props.simulationId))

const stopPolling = () => {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

const refreshRunStatus = async () => {
  const res = await getRunStatusDetail(props.simulationId)
  if (!res.success || !res.data) return

  runStatus.value = res.data

  if (res.data.runner_status === 'paused') {
    emit('update-status', 'paused')
    stopPolling()
    addLog('world simulation 已暂停')
  } else if (res.data.runner_status === 'completed') {
    emit('update-status', 'completed')
    stopPolling()
    addLog('world simulation 已完成')
  } else if (res.data.runner_status === 'stopped') {
    emit('update-status', res.data.resume_supported ? 'paused' : 'completed')
    stopPolling()
    addLog(res.data.resume_supported ? 'world simulation 已停止，可从 checkpoint 继续' : 'world simulation 已停止')
  } else if (res.data.runner_status === 'failed') {
    emit('update-status', 'error')
    stopPolling()
    addLog(
      res.data.resume_supported
        ? `world simulation 失败，但可从 checkpoint 继续: ${res.data.error || '未知错误'}`
        : `world simulation 失败: ${res.data.error || '未知错误'}`
    )
  } else {
    emit('update-status', 'processing')
  }
}

const startPolling = () => {
  if (pollTimer) return
  pollTimer = window.setInterval(refreshRunStatus, 2000)
}

const launchSimulation = async () => {
  emit('update-status', 'processing')
  addLog('启动 world simulation')

  const res = await startSimulation({
    simulation_id: props.simulationId,
    platform: 'world',
    max_rounds: props.maxRounds || undefined,
    enable_graph_memory_update: false
  })

  if (!res.success) {
    if ((res.error || '').includes('正在运行')) {
      addLog('检测到 world simulation 已在运行，转为状态轮询')
      await refreshRunStatus()
      startPolling()
      return
    }
    emit('update-status', 'error')
    addLog(`启动失败: ${res.error || '未知错误'}`)
    return
  }

  await refreshRunStatus()
  startPolling()
}

const handlePauseSimulation = async () => {
  if (!canPause.value || isPausing.value) return

  isPausing.value = true
  addLog('暂停 world simulation')

  try {
    const res = await pauseSimulation({ simulation_id: props.simulationId })
    if (!res.success) {
      throw new Error(res.error || '暂停失败')
    }

    runStatus.value = res.data || runStatus.value
    stopPolling()
    emit('update-status', 'paused')
    addLog('world simulation 已暂停')
  } catch (error) {
    addLog(`暂停失败: ${error.message}`)
  } finally {
    isPausing.value = false
  }
}

const handleResumeSimulation = async () => {
  if (!canResume.value || isResuming.value) return

  isResuming.value = true
  addLog('恢复 world simulation')

  try {
    const res = await resumeSimulation({
      simulation_id: props.simulationId,
      max_rounds: props.maxRounds || undefined
    })
    if (!res.success) {
      throw new Error(res.error || '恢复失败')
    }

    runStatus.value = res.data || runStatus.value
    emit('update-status', 'processing')
    if (res.data?.resumed_from_checkpoint) {
      const tick = res.data?.checkpoint_tick ?? runStatus.value.current_round ?? 0
      addLog(`world simulation 已从 checkpoint tick ${tick} 续跑`)
    } else {
      addLog('world simulation 已继续')
    }
    startPolling()
    await refreshRunStatus()
  } catch (error) {
    addLog(`恢复失败: ${error.message}`)
  } finally {
    isResuming.value = false
  }
}

const handleRestartSimulation = async () => {
  if (!canRestart.value || isRestarting.value) return

  isRestarting.value = true
  stopPolling()
  emit('update-status', 'processing')
  addLog('强制重启 world simulation')

  try {
    const res = await startSimulation({
      simulation_id: props.simulationId,
      platform: 'world',
      max_rounds: props.maxRounds || undefined,
      enable_graph_memory_update: false,
      force: true
    })

    if (!res.success) {
      throw new Error(res.error || '重启失败')
    }

    if (res.data?.force_restarted) {
      addLog('已清理旧日志并重新启动 world simulation')
    }

    runStatus.value = res.data || runStatus.value
    await refreshRunStatus()
    startPolling()
  } catch (error) {
    emit('update-status', 'error')
    addLog(`重启失败: ${error.message}`)
  } finally {
    isRestarting.value = false
  }
}

const handleGenerateReport = async () => {
  if (isGeneratingReport.value) return
  isGeneratingReport.value = true
  addLog('启动 world report 生成')

  try {
    const res = await generateReport({ simulation_id: props.simulationId })
    if (!res.success || !res.data?.report_id) {
      throw new Error(res.error || '生成报告失败')
    }
    router.push({
      name: 'Report',
      params: { reportId: res.data.report_id }
    })
  } catch (error) {
    addLog(`world report 启动失败: ${error.message}`)
  } finally {
    isGeneratingReport.value = false
  }
}

const bootstrapSimulation = async () => {
  await refreshRunStatus()

  const status = runStatus.value.runner_status
  if (['completed', 'stopped', 'failed', 'paused'].includes(status)) {
    return
  }

  if (['running', 'processing', 'starting', 'in_progress'].includes(status)) {
    addLog('检测到已有 world simulation 状态，转为轮询')
    startPolling()
    return
  }

  await launchSimulation()
}

onMounted(() => {
  bootstrapSimulation()
})

onUnmounted(() => {
  stopPolling()
})
</script>

<style scoped>
.world-run-panel {
  height: 100%;
  display: flex;
  flex-direction: column;
  background:
    radial-gradient(circle at top left, rgba(14, 165, 233, 0.12), transparent 26%),
    radial-gradient(circle at top right, rgba(249, 115, 22, 0.10), transparent 24%),
    #f8fafc;
}

.control-bar,
.status-metrics,
.panel-head,
.event-top,
.tick-top,
.log-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.control-bar {
  border-bottom: 1px solid rgba(15, 23, 42, 0.08);
  padding: 18px 22px;
  background: rgba(255, 255, 255, 0.88);
  backdrop-filter: blur(12px);
}

.control-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.phase-alert {
  margin: 16px 22px 0;
  padding: 12px 16px;
  border: 1px solid rgba(15, 23, 42, 0.08);
  border-radius: 14px;
  font-size: 0.92rem;
  line-height: 1.5;
  backdrop-filter: blur(10px);
}

.phase-alert.warn {
  color: #9a3412;
  border-color: rgba(249, 115, 22, 0.2);
  background: linear-gradient(180deg, rgba(255, 247, 237, 0.96), rgba(255, 237, 213, 0.9));
}

.phase-alert.info {
  color: #0f766e;
  border-color: rgba(13, 148, 136, 0.16);
  background: linear-gradient(180deg, rgba(240, 253, 250, 0.96), rgba(204, 251, 241, 0.86));
}

.status-label,
.panel-title,
.lane-title {
  font-weight: 700;
  color: #0f172a;
}

.panel-label {
  font-size: 0.74rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: #64748b;
}

.status-metrics {
  gap: 12px;
  margin-top: 8px;
  flex-wrap: wrap;
}

.metric,
.progress-badge,
.stack-count,
.log-id {
  font-size: 0.8rem;
  color: #475569;
  font-family: 'JetBrains Mono', monospace;
}

.action-btn {
  border: 1px solid transparent;
  background: linear-gradient(135deg, #0f172a, #1d4ed8);
  color: #fff;
  padding: 12px 18px;
  font-weight: 600;
  cursor: pointer;
  border-radius: 12px;
  box-shadow: 0 14px 30px rgba(15, 23, 42, 0.14);
}

.action-btn--secondary {
  background: rgba(255, 255, 255, 0.92);
  color: #0f172a;
  border-color: rgba(15, 23, 42, 0.12);
  box-shadow: none;
}

.action-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.hero-grid,
.feed-grid {
  display: grid;
  gap: 18px;
  padding: 18px 22px 0;
}

.hero-grid {
  grid-template-columns: minmax(280px, 380px) minmax(0, 1fr);
}

.feed-grid {
  flex: 1;
  min-height: 0;
  grid-template-columns: minmax(260px, 360px) minmax(0, 1fr);
  padding-bottom: 18px;
}

.snapshot-card,
.events-card,
.tick-panel,
.timeline-panel,
.system-logs {
  border: 1px solid rgba(15, 23, 42, 0.08);
  border-radius: 20px;
  background: rgba(255, 255, 255, 0.92);
  box-shadow: 0 18px 45px rgba(15, 23, 42, 0.06);
}

.snapshot-card,
.events-card,
.tick-panel,
.timeline-panel {
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.snapshot-card,
.events-card {
  padding: 20px;
}

.tick-panel,
.timeline-panel {
  overflow: hidden;
}

.panel-head {
  padding: 20px 20px 0;
}

.snapshot-card .panel-head,
.events-card .panel-head {
  padding: 0;
}

.summary-text {
  margin: 18px 0 0;
  line-height: 1.68;
  color: #334155;
}

.snapshot-stats {
  margin-top: 20px;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.snapshot-stat {
  border: 1px solid rgba(15, 23, 42, 0.08);
  border-radius: 14px;
  background: linear-gradient(180deg, rgba(248, 250, 252, 0.9), #fff);
  padding: 14px;
}

.snapshot-stat .label {
  display: block;
  font-size: 0.76rem;
  color: #64748b;
}

.snapshot-stat .value {
  display: block;
  margin-top: 6px;
  font-size: 1.16rem;
  font-weight: 700;
  color: #0f172a;
}

.mini-title {
  margin-top: 20px;
  margin-bottom: 10px;
  font-size: 0.74rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: #64748b;
}

.pressure-grid,
.focus-tags {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}

.pressure-chip,
.focus-tag {
  border-radius: 999px;
  padding: 8px 12px;
  font-size: 0.82rem;
  background: rgba(15, 23, 42, 0.06);
  color: #0f172a;
}

.pressure-chip {
  display: inline-flex;
  gap: 8px;
  align-items: center;
}

.lane-grid {
  margin-top: 18px;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
  min-height: 0;
}

.lane-column {
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.lane-empty,
.timeline-empty {
  color: #64748b;
  padding: 18px 20px;
}

.lane-card,
.event-card,
.tick-card {
  border: 1px solid rgba(15, 23, 42, 0.08);
  border-radius: 16px;
  background: #fff;
}

.lane-card {
  padding: 14px;
}

.lane-card--active {
  border-color: rgba(14, 165, 233, 0.22);
  background: linear-gradient(180deg, rgba(240, 249, 255, 0.9), #fff);
}

.lane-card--queued {
  border-color: rgba(249, 115, 22, 0.22);
  background: linear-gradient(180deg, rgba(255, 247, 237, 0.9), #fff);
}

.lane-card-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  color: #0f172a;
}

.lane-card p,
.tick-card p,
.event-content {
  margin: 10px 0 0;
  line-height: 1.58;
  color: #334155;
}

.lane-meta,
.tick-metrics,
.event-meta {
  margin-top: 12px;
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  font-size: 0.78rem;
  color: #64748b;
}

.tick-list,
.timeline-list {
  overflow-y: auto;
  padding: 16px 20px 20px;
  display: grid;
  gap: 12px;
}

.tick-card,
.event-card {
  padding: 14px;
}

.event-card--intent {
  border-color: rgba(14, 165, 233, 0.18);
}

.event-card--resolve {
  border-color: rgba(37, 99, 235, 0.18);
}

.event-card--start {
  border-color: rgba(34, 197, 94, 0.18);
}

.event-card--queue {
  border-color: rgba(249, 115, 22, 0.18);
}

.event-card--done {
  border-color: rgba(99, 102, 241, 0.18);
}

.event-card--warn {
  border-color: rgba(249, 115, 22, 0.24);
  background: linear-gradient(180deg, rgba(255, 247, 237, 0.88), #fff);
}

.event-card--recover {
  border-color: rgba(13, 148, 136, 0.2);
  background: linear-gradient(180deg, rgba(240, 253, 250, 0.88), #fff);
}

.event-actor {
  font-weight: 600;
  color: #0f172a;
}

.event-type {
  margin-top: 4px;
  font-size: 0.76rem;
  color: #64748b;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.event-round {
  font-family: 'JetBrains Mono', monospace;
  color: #475569;
}

.system-logs {
  margin: 0 22px 22px;
  padding: 18px 20px;
}

.log-title {
  font-weight: 700;
  color: #0f172a;
}

.log-content {
  margin-top: 12px;
  max-height: 160px;
  overflow-y: auto;
  display: grid;
  gap: 8px;
}

.log-line {
  display: grid;
  grid-template-columns: 86px 1fr;
  gap: 12px;
  font-size: 0.82rem;
  color: #334155;
}

.log-time {
  color: #64748b;
  font-family: 'JetBrains Mono', monospace;
}

@media (max-width: 1100px) {
  .hero-grid,
  .feed-grid {
    grid-template-columns: 1fr;
  }

  .system-logs {
    margin-top: 0;
  }
}

@media (max-width: 720px) {
  .control-bar {
    align-items: flex-start;
    gap: 16px;
    flex-direction: column;
  }

  .phase-alert {
    margin: 14px 16px 0;
  }

  .lane-grid,
  .snapshot-stats {
    grid-template-columns: 1fr;
  }
}
</style>
