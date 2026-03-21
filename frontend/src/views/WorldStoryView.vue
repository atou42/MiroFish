<template>
  <div class="story-page">
    <header class="story-nav">
      <button class="nav-pill nav-back" @click="router.back()">Back</button>
      <div class="nav-brand">
        <span class="brand-mark">MiroFish</span>
        <span class="brand-mode">World Story</span>
      </div>
      <div class="nav-actions">
        <RouterLink v-if="reportLink" :to="reportLink" class="nav-pill nav-link">
          Full Report
        </RouterLink>
        <RouterLink :to="{ name: 'Home' }" class="nav-pill nav-link">
          Home
        </RouterLink>
      </div>
    </header>

    <main v-if="story" class="story-main">
      <section class="hero-section">
        <div class="hero-copy reveal-in">
          <p class="hero-eyebrow">{{ story.hero.eyebrow }}</p>
          <h1 class="hero-title">{{ story.hero.headline }}</h1>
          <p class="hero-subtitle">{{ story.hero.subtitle }}</p>

          <div class="hero-meta">
            <span>Simulation {{ story.simulation_id }}</span>
            <span>{{ story.meta.ticks }} ticks</span>
            <span>Status {{ story.meta.status }}</span>
          </div>

          <div class="hero-actions">
            <a href="#episodes" class="cta-primary">Jump to Episodes</a>
            <a href="#risks" class="cta-secondary">See the Cliffhangers</a>
          </div>
        </div>

        <aside class="hero-board reveal-in reveal-delay-1">
          <div class="hero-board-topline">Current World State</div>

          <div class="metrics-band">
            <article
              v-for="metric in story.hero.metrics"
              :key="metric.label"
              class="metric-column"
              :class="`tone-${metric.tone}`"
            >
              <span class="metric-label">{{ metric.label }}</span>
              <strong class="metric-value">{{ metric.value.toFixed(2) }}</strong>
              <span class="metric-bar">
                <span class="metric-fill" :style="{ height: `${Math.max(8, metric.value * 100)}%` }"></span>
              </span>
            </article>
          </div>

          <div class="flashpoints-block">
            <div class="section-kicker">Flashpoints</div>
            <div class="flashpoint-list">
              <article
                v-for="item in story.hero.flashpoints"
                :key="item.title"
                class="flashpoint-item"
              >
                <span class="flashpoint-score">{{ formatSeverity(item.severity) }}</span>
                <div>
                  <h3>{{ item.title }}</h3>
                  <p>{{ item.summary }}</p>
                </div>
              </article>
            </div>
          </div>

          <div class="top-factions">
            <div class="section-kicker">Power Centers</div>
            <div class="faction-chip-list">
              <button
                v-for="(item, index) in story.hero.top_factions"
                :key="item.name"
                class="faction-chip"
                @click="focusFactionByName(item.name)"
              >
                <span class="faction-rank">0{{ index + 1 }}</span>
                <span class="faction-name">{{ item.name }}</span>
              </button>
            </div>
          </div>
        </aside>
      </section>

      <section id="episodes" class="episodes-section">
        <div class="section-heading reveal-in">
          <p class="section-label">Season Timeline</p>
          <h2>把 240 轮世界推进，读成一季真正有节奏的连续剧</h2>
          <p class="section-copy">
            每一张 episode 不是 tick 汇总，而是一段局势升级、角色出手、世界改变、然后留下悬念的剧集。
          </p>
        </div>

        <div class="episode-rail reveal-in reveal-delay-1">
          <button
            v-for="(episode, index) in story.episodes"
            :key="episode.id"
            class="episode-card"
            :class="{ active: index === activeEpisodeIndex }"
            @click="activeEpisodeIndex = index"
          >
            <div class="episode-card-top">
              <span class="episode-index">{{ episode.index.toString().padStart(2, '0') }}</span>
              <span class="episode-ticks">T{{ episode.tick_start }}-{{ episode.tick_end }}</span>
            </div>
            <h3>{{ episode.title }}</h3>
            <p>{{ episode.logline }}</p>
            <div class="episode-shift">
              <span>Tension {{ signedNumber(episode.world_shift.tension_delta) }}</span>
              <span>Stability {{ signedNumber(episode.world_shift.stability_delta) }}</span>
            </div>
          </button>
        </div>

        <div v-if="activeEpisode" class="episode-stage reveal-in reveal-delay-2">
          <div class="episode-poster">
            <p class="poster-kicker">Episode Focus</p>
            <h3>{{ activeEpisode.title }}</h3>
            <p class="poster-logline">{{ activeEpisode.logline }}</p>
            <div class="poster-metrics">
              <span>Ending tension {{ activeEpisode.world_shift.ending_tension.toFixed(2) }}</span>
              <span>Ending stability {{ activeEpisode.world_shift.ending_stability.toFixed(2) }}</span>
            </div>
          </div>

          <div class="episode-columns">
            <article class="episode-column">
              <p class="column-label">Turning Points</p>
              <ul class="story-list">
                <li v-for="item in activeEpisode.turning_points" :key="item">{{ item }}</li>
              </ul>
            </article>

            <article class="episode-column">
              <p class="column-label">Featured Actors</p>
              <ul class="actor-mini-list">
                <li v-for="item in activeEpisode.key_actors" :key="item.name">
                  <strong>{{ item.name }}</strong>
                  <span>{{ item.latest_move }}</span>
                </li>
              </ul>
            </article>

            <article class="episode-column cliffhanger-column">
              <p class="column-label">Cliffhanger</p>
              <div class="cliffhanger-box">
                {{ activeEpisode.cliffhanger || 'The world ends this episode with pressure still unresolved.' }}
              </div>
            </article>
          </div>
        </div>
      </section>

      <section id="factions" class="factions-section">
        <div class="section-heading reveal-in">
          <p class="section-label">Faction War Room</p>
          <h2>谁在真正推动世界，谁只是被浪潮推着走</h2>
        </div>

        <div class="faction-layout reveal-in reveal-delay-1">
          <div class="faction-list-panel">
            <button
              v-for="(faction, index) in story.factions.primary"
              :key="faction.name"
              class="faction-row"
              :class="{ active: index === activeFactionIndex }"
              @click="activeFactionIndex = index"
            >
              <div>
                <span class="faction-row-name">{{ faction.name }}</span>
                <span class="faction-row-type">{{ faction.type || 'Unknown' }}</span>
              </div>
              <strong class="faction-row-score">{{ faction.activity_score }}</strong>
            </button>
          </div>

          <div v-if="activeFaction" class="faction-detail-panel">
            <div class="faction-detail-head">
              <p class="section-label">Now Tracking</p>
              <h3>{{ activeFaction.name }}</h3>
              <p>{{ activeFaction.latest_move }}</p>
            </div>

            <div class="faction-stat-grid">
              <article class="faction-stat">
                <span class="faction-stat-label">Selections</span>
                <strong>{{ activeFaction.selections }}</strong>
              </article>
              <article class="faction-stat">
                <span class="faction-stat-label">Events</span>
                <strong>{{ activeFaction.event_count }}</strong>
              </article>
              <article class="faction-stat">
                <span class="faction-stat-label">Accepted</span>
                <strong>{{ activeFaction.accepted_events }}</strong>
              </article>
              <article class="faction-stat">
                <span class="faction-stat-label">Completed</span>
                <strong>{{ activeFaction.completed_events }}</strong>
              </article>
            </div>

            <div class="thread-columns">
              <article class="thread-column">
                <p class="column-label">Active Threads</p>
                <ul class="story-list compact">
                  <li v-for="item in activeFaction.active_events" :key="item">{{ item }}</li>
                  <li v-if="!activeFaction.active_events.length" class="muted">No active threads</li>
                </ul>
              </article>
              <article class="thread-column">
                <p class="column-label">Queued Moves</p>
                <ul class="story-list compact">
                  <li v-for="item in activeFaction.queued_event_titles" :key="item">{{ item }}</li>
                  <li v-if="!activeFaction.queued_event_titles.length" class="muted">No queued moves</li>
                </ul>
              </article>
            </div>
          </div>
        </div>
      </section>

      <section id="risks" class="risks-section">
        <div class="section-heading reveal-in">
          <p class="section-label">Next Episode Pressure</p>
          <h2>最值得追的，不是已经发生的，而是马上就要爆开的那几条线</h2>
        </div>

        <div class="risk-grid reveal-in reveal-delay-1">
          <article
            v-for="item in story.risks.items"
            :key="`${item.category}-${item.title}`"
            class="risk-card"
          >
            <div class="risk-card-top">
              <span class="risk-category">{{ item.category }}</span>
              <span class="risk-score">{{ formatSeverity(item.severity) }}</span>
            </div>
            <h3>{{ item.title }}</h3>
            <p>{{ item.summary }}</p>
            <div class="risk-meta">
              <span v-if="item.owner">Owner {{ item.owner }}</span>
              <span v-if="item.status">Status {{ item.status }}</span>
            </div>
          </article>
        </div>
      </section>

      <section class="process-section">
        <div class="section-heading reveal-in">
          <p class="section-label">How This Story Is Made</p>
          <h2>这不是手写剧情梗概，而是一个持续写盘、持续分叉、持续留下悬念的世界运行过程</h2>
        </div>

        <div class="process-strip reveal-in reveal-delay-1">
          <article
            v-for="(item, index) in story.process"
            :key="item.title"
            class="process-step"
          >
            <span class="process-index">0{{ index + 1 }}</span>
            <h3>{{ item.title }}</h3>
            <p>{{ item.body }}</p>
          </article>
        </div>
      </section>
    </main>

    <div v-else-if="error" class="story-state">
      <div>
        <p class="state-label">Story View Unavailable</p>
        <h1>{{ error }}</h1>
      </div>
    </div>

    <div v-else class="story-state">
      <div>
        <p class="state-label">Loading</p>
        <h1>Building the current world cut...</h1>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { getWorldStory } from '../api/simulation'

const route = useRoute()
const router = useRouter()

const story = ref(null)
const error = ref('')
const activeEpisodeIndex = ref(0)
const activeFactionIndex = ref(0)

const reportLink = computed(() => {
  const reportId = story.value?.meta?.report_id
  return reportId ? { name: 'Report', params: { reportId } } : null
})

const activeEpisode = computed(() => {
  const episodes = story.value?.episodes || []
  return episodes[activeEpisodeIndex.value] || episodes[0] || null
})

const activeFaction = computed(() => {
  const factions = story.value?.factions?.primary || []
  return factions[activeFactionIndex.value] || factions[0] || null
})

const formatSeverity = (value) => Number(value || 0).toFixed(1)

const signedNumber = (value) => {
  const number = Number(value || 0)
  const prefix = number > 0 ? '+' : ''
  return `${prefix}${number.toFixed(2)}`
}

const focusFactionByName = (name) => {
  const factions = story.value?.factions?.primary || []
  const index = factions.findIndex(item => item.name === name)
  if (index >= 0) {
    activeFactionIndex.value = index
    window.location.hash = '#factions'
  }
}

const loadStory = async () => {
  error.value = ''
  story.value = null
  activeEpisodeIndex.value = 0
  activeFactionIndex.value = 0
  try {
    const res = await getWorldStory(route.params.simulationId)
    story.value = res.data
  } catch (err) {
    error.value = err.message || 'Failed to load world story.'
  }
}

watch(() => route.params.simulationId, () => {
  loadStory()
}, { immediate: true })
</script>

<style scoped>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+SC:wght@300;400;500;600;700&family=Noto+Serif+SC:wght@500;700;900&family=Space+Grotesk:wght@400;500;700&display=swap');

.story-page {
  --paper: #f4ede2;
  --paper-strong: #efe4d4;
  --ink: #201913;
  --ink-soft: #5b4b3f;
  --border: rgba(52, 34, 21, 0.14);
  --rust: #a43820;
  --rust-strong: #7f2616;
  --navy: #173c59;
  --gold: #c9892f;
  --teal: #2b6f6a;
  min-height: 100vh;
  background:
    radial-gradient(circle at top left, rgba(201, 137, 47, 0.16), transparent 26rem),
    radial-gradient(circle at 88% 16%, rgba(23, 60, 89, 0.16), transparent 22rem),
    linear-gradient(180deg, #f8f1e8 0%, var(--paper) 42%, #eadfcf 100%);
  color: var(--ink);
  font-family: 'IBM Plex Sans SC', 'Noto Sans SC', sans-serif;
}

.story-nav {
  position: sticky;
  top: 0;
  z-index: 30;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  padding: 1rem 1.5rem;
  backdrop-filter: blur(18px);
  background: rgba(248, 241, 232, 0.84);
  border-bottom: 1px solid rgba(52, 34, 21, 0.08);
}

.nav-brand {
  display: flex;
  align-items: baseline;
  gap: 0.75rem;
}

.brand-mark,
.brand-mode {
  letter-spacing: 0.16em;
  text-transform: uppercase;
  font-family: 'Space Grotesk', sans-serif;
}

.brand-mark {
  font-weight: 700;
}

.brand-mode {
  color: var(--ink-soft);
  font-size: 0.82rem;
}

.nav-actions,
.hero-actions {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  flex-wrap: wrap;
}

.nav-pill,
.cta-primary,
.cta-secondary {
  border: 1px solid var(--border);
  background: rgba(255, 255, 255, 0.44);
  color: var(--ink);
  text-decoration: none;
  padding: 0.7rem 1rem;
  border-radius: 999px;
  font-size: 0.9rem;
  transition: transform 220ms ease, background 220ms ease, border-color 220ms ease;
  cursor: pointer;
}

.nav-pill:hover,
.cta-primary:hover,
.cta-secondary:hover {
  transform: translateY(-1px);
}

.cta-primary {
  background: var(--ink);
  color: #f8f1e8;
  border-color: var(--ink);
}

.cta-secondary {
  background: transparent;
}

.story-main {
  width: min(1380px, calc(100vw - 2rem));
  margin: 0 auto;
  padding-bottom: 5rem;
}

.hero-section {
  display: grid;
  grid-template-columns: minmax(0, 1.35fr) minmax(320px, 0.9fr);
  gap: clamp(1.2rem, 2vw, 2.5rem);
  min-height: calc(100vh - 6rem);
  align-items: center;
  padding: clamp(2.5rem, 4vw, 5rem) 0 3rem;
}

.hero-copy {
  max-width: 52rem;
}

.hero-eyebrow,
.section-label,
.section-kicker,
.column-label,
.poster-kicker,
.state-label {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  font-family: 'Space Grotesk', sans-serif;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  font-size: 0.78rem;
  color: var(--rust);
}

.hero-title,
.section-heading h2,
.episode-poster h3,
.faction-detail-head h3,
.story-state h1 {
  font-family: 'Noto Serif SC', serif;
  font-weight: 900;
  letter-spacing: -0.03em;
}

.hero-title {
  font-size: clamp(3.1rem, 8vw, 7.4rem);
  line-height: 0.94;
  margin-top: 0.9rem;
  text-wrap: balance;
}

.hero-subtitle {
  max-width: 44rem;
  margin-top: 1.2rem;
  font-size: clamp(1.02rem, 1.8vw, 1.36rem);
  line-height: 1.8;
  color: var(--ink-soft);
}

.hero-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem;
  margin-top: 1.4rem;
}

.hero-meta span {
  padding: 0.4rem 0.7rem;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.4);
  border: 1px solid rgba(52, 34, 21, 0.08);
  font-size: 0.88rem;
}

.hero-board {
  position: relative;
  padding: 1.4rem;
  border-radius: 1.8rem;
  border: 1px solid rgba(52, 34, 21, 0.12);
  background:
    linear-gradient(160deg, rgba(255, 255, 255, 0.72), rgba(239, 228, 212, 0.92)),
    radial-gradient(circle at top, rgba(201, 137, 47, 0.14), transparent 16rem);
  overflow: hidden;
  box-shadow: 0 24px 80px rgba(64, 34, 16, 0.08);
}

.hero-board::before {
  content: '';
  position: absolute;
  inset: auto -5rem -7rem auto;
  width: 16rem;
  height: 16rem;
  border-radius: 50%;
  background: rgba(23, 60, 89, 0.08);
}

.hero-board-topline {
  position: relative;
  font-family: 'Space Grotesk', sans-serif;
  font-size: 0.78rem;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--ink-soft);
}

.metrics-band {
  position: relative;
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 1rem;
  margin-top: 1.2rem;
}

.metric-column {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 0.6rem;
}

.metric-label {
  font-size: 0.8rem;
  color: var(--ink-soft);
}

.metric-value {
  font-family: 'Noto Serif SC', serif;
  font-size: clamp(1.8rem, 3vw, 2.5rem);
}

.metric-bar {
  width: 100%;
  height: 7rem;
  border-radius: 1rem;
  background: rgba(255, 255, 255, 0.5);
  overflow: hidden;
  display: flex;
  align-items: end;
  padding: 0.45rem;
}

.metric-fill {
  display: block;
  width: 100%;
  border-radius: 0.75rem;
}

.tone-hot .metric-fill {
  background: linear-gradient(180deg, #d3632b, var(--rust));
}

.tone-cool .metric-fill {
  background: linear-gradient(180deg, #4b8ac0, var(--navy));
}

.tone-surge .metric-fill {
  background: linear-gradient(180deg, #4c918b, var(--teal));
}

.flashpoints-block,
.top-factions {
  position: relative;
  margin-top: 1.4rem;
}

.flashpoint-list {
  display: grid;
  gap: 0.8rem;
  margin-top: 0.8rem;
}

.flashpoint-item {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 0.9rem;
  padding: 0.9rem 0;
  border-top: 1px solid rgba(52, 34, 21, 0.08);
}

.flashpoint-score,
.risk-score,
.faction-row-score,
.episode-index,
.process-index {
  font-family: 'Space Grotesk', sans-serif;
  font-weight: 700;
}

.flashpoint-score {
  font-size: 1.1rem;
  color: var(--rust);
}

.flashpoint-item h3,
.risk-card h3,
.episode-card h3 {
  font-family: 'Noto Serif SC', serif;
  font-weight: 700;
}

.flashpoint-item p {
  margin-top: 0.3rem;
  color: var(--ink-soft);
  line-height: 1.6;
  font-size: 0.95rem;
}

.faction-chip-list {
  display: flex;
  flex-wrap: wrap;
  gap: 0.65rem;
  margin-top: 0.8rem;
}

.faction-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.6rem;
  padding: 0.55rem 0.8rem;
  border-radius: 999px;
  border: 1px solid rgba(52, 34, 21, 0.1);
  background: rgba(255, 255, 255, 0.6);
  cursor: pointer;
}

.faction-rank {
  color: var(--rust);
  font-size: 0.78rem;
}

.section-heading {
  max-width: 58rem;
}

.section-heading h2 {
  font-size: clamp(2rem, 4vw, 3.85rem);
  line-height: 1.06;
  margin-top: 0.7rem;
}

.section-copy {
  margin-top: 0.9rem;
  color: var(--ink-soft);
  font-size: 1.05rem;
  line-height: 1.75;
}

.episodes-section,
.factions-section,
.risks-section,
.process-section {
  padding: 4.5rem 0 0;
}

.episode-rail {
  display: grid;
  grid-auto-flow: column;
  grid-auto-columns: minmax(260px, 320px);
  gap: 1rem;
  overflow-x: auto;
  padding: 1.4rem 0 0.8rem;
  scroll-snap-type: x proximity;
}

.episode-card {
  scroll-snap-align: start;
  padding: 1.2rem;
  border-radius: 1.4rem;
  border: 1px solid rgba(52, 34, 21, 0.12);
  background: linear-gradient(180deg, rgba(255,255,255,0.78), rgba(239,228,212,0.88));
  cursor: pointer;
  text-align: left;
  min-height: 16.5rem;
}

.episode-card.active {
  transform: translateY(-0.35rem);
  border-color: rgba(164, 56, 32, 0.4);
  box-shadow: 0 24px 64px rgba(90, 43, 21, 0.12);
}

.episode-card-top,
.episode-shift,
.risk-card-top,
.risk-meta,
.poster-metrics {
  display: flex;
  justify-content: space-between;
  gap: 0.8rem;
  flex-wrap: wrap;
}

.episode-card-top,
.episode-shift,
.episode-card p,
.faction-row-type,
.risk-meta,
.risk-card p,
.process-step p,
.thread-column .muted,
.actor-mini-list span {
  color: var(--ink-soft);
}

.episode-card h3 {
  margin-top: 0.7rem;
  font-size: 1.3rem;
  line-height: 1.25;
}

.episode-card p {
  margin-top: 0.75rem;
  line-height: 1.7;
}

.episode-stage {
  display: grid;
  grid-template-columns: minmax(280px, 0.82fr) minmax(0, 1.2fr);
  gap: 1rem;
  margin-top: 1rem;
}

.episode-poster,
.faction-detail-panel,
.risk-card,
.process-step {
  border-radius: 1.5rem;
  border: 1px solid rgba(52, 34, 21, 0.12);
  background: rgba(255, 255, 255, 0.58);
  box-shadow: 0 16px 40px rgba(64, 34, 16, 0.06);
}

.episode-poster {
  padding: 1.4rem;
  background:
    linear-gradient(160deg, rgba(23, 60, 89, 0.12), transparent 50%),
    linear-gradient(180deg, rgba(255,255,255,0.82), rgba(239,228,212,0.92));
}

.episode-poster h3 {
  margin-top: 0.8rem;
  font-size: clamp(1.9rem, 3vw, 2.6rem);
  line-height: 1.1;
}

.poster-logline {
  margin-top: 1rem;
  color: var(--ink-soft);
  line-height: 1.8;
}

.poster-metrics {
  margin-top: 1rem;
  padding-top: 1rem;
  border-top: 1px solid rgba(52, 34, 21, 0.08);
  font-size: 0.92rem;
}

.episode-columns {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 1rem;
}

.episode-column {
  padding: 1.2rem;
  border-radius: 1.4rem;
  border: 1px solid rgba(52, 34, 21, 0.12);
  background: rgba(255, 255, 255, 0.44);
}

.story-list,
.actor-mini-list {
  margin-top: 0.9rem;
  display: grid;
  gap: 0.7rem;
  padding-left: 1rem;
}

.story-list li,
.actor-mini-list li {
  line-height: 1.65;
}

.actor-mini-list {
  padding-left: 0;
  list-style: none;
}

.actor-mini-list strong {
  display: block;
  font-family: 'Noto Serif SC', serif;
}

.cliffhanger-column {
  background: linear-gradient(180deg, rgba(164, 56, 32, 0.08), rgba(255,255,255,0.44));
}

.cliffhanger-box {
  margin-top: 0.95rem;
  font-family: 'Noto Serif SC', serif;
  font-size: 1.1rem;
  line-height: 1.7;
}

.faction-layout {
  display: grid;
  grid-template-columns: minmax(260px, 360px) minmax(0, 1fr);
  gap: 1rem;
  margin-top: 1.3rem;
}

.faction-list-panel {
  display: grid;
  gap: 0.65rem;
}

.faction-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  padding: 1rem 1.1rem;
  border-radius: 1.25rem;
  border: 1px solid rgba(52, 34, 21, 0.1);
  background: rgba(255, 255, 255, 0.52);
  text-align: left;
}

.faction-row.active {
  background: linear-gradient(135deg, rgba(164, 56, 32, 0.08), rgba(23, 60, 89, 0.05));
  border-color: rgba(164, 56, 32, 0.28);
}

.faction-row-name {
  display: block;
  font-family: 'Noto Serif SC', serif;
  font-size: 1.08rem;
}

.faction-detail-panel {
  padding: 1.4rem;
}

.faction-detail-head p:last-child {
  margin-top: 0.7rem;
  color: var(--ink-soft);
  line-height: 1.7;
}

.faction-stat-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0.8rem;
  margin-top: 1.15rem;
}

.faction-stat {
  padding: 0.95rem;
  border-radius: 1rem;
  background: rgba(255, 255, 255, 0.56);
  border: 1px solid rgba(52, 34, 21, 0.08);
}

.faction-stat-label {
  display: block;
  color: var(--ink-soft);
  font-size: 0.82rem;
}

.faction-stat strong {
  display: block;
  margin-top: 0.4rem;
  font-size: 1.35rem;
  font-family: 'Space Grotesk', sans-serif;
}

.thread-columns {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1rem;
  margin-top: 1rem;
}

.thread-column {
  padding: 1rem;
  border-radius: 1rem;
  background: rgba(255, 255, 255, 0.44);
  border: 1px solid rgba(52, 34, 21, 0.08);
}

.story-list.compact {
  gap: 0.5rem;
}

.risks-section {
  padding-bottom: 1rem;
}

.risk-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 1rem;
  margin-top: 1.4rem;
}

.risk-card {
  padding: 1.2rem;
  min-height: 14rem;
  background:
    linear-gradient(180deg, rgba(255,255,255,0.7), rgba(239,228,212,0.86)),
    linear-gradient(135deg, rgba(164,56,32,0.08), transparent 55%);
}

.risk-category {
  font-size: 0.78rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--rust);
}

.risk-card h3 {
  margin-top: 0.9rem;
  font-size: 1.32rem;
  line-height: 1.3;
}

.risk-card p {
  margin-top: 0.7rem;
  line-height: 1.75;
}

.risk-meta {
  margin-top: 1.1rem;
  font-size: 0.86rem;
}

.process-strip {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 1rem;
  margin-top: 1.4rem;
  padding-bottom: 4rem;
}

.process-step {
  padding: 1.25rem;
  min-height: 16rem;
}

.process-index {
  color: var(--rust);
  font-size: 0.88rem;
}

.process-step h3 {
  margin-top: 0.8rem;
  font-family: 'Noto Serif SC', serif;
  font-size: 1.4rem;
  line-height: 1.25;
}

.process-step p {
  margin-top: 0.9rem;
  line-height: 1.8;
}

.story-state {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 2rem;
  text-align: center;
}

.reveal-in {
  animation: revealIn 620ms cubic-bezier(.22, 1, .36, 1) both;
}

.reveal-delay-1 {
  animation-delay: 100ms;
}

.reveal-delay-2 {
  animation-delay: 180ms;
}

@keyframes revealIn {
  from {
    opacity: 0;
    transform: translateY(18px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@media (max-width: 1120px) {
  .hero-section,
  .episode-stage,
  .faction-layout {
    grid-template-columns: 1fr;
  }

  .episode-columns,
  .risk-grid,
  .process-strip {
    grid-template-columns: 1fr;
  }

  .faction-stat-grid,
  .thread-columns {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 720px) {
  .story-main {
    width: min(100vw - 1rem, 100%);
  }

  .story-nav {
    padding: 0.9rem 0.8rem;
  }

  .nav-actions {
    display: none;
  }

  .hero-section {
    min-height: auto;
    padding-top: 2rem;
  }

  .hero-title {
    font-size: clamp(2.7rem, 14vw, 4rem);
  }

  .metrics-band,
  .faction-stat-grid {
    grid-template-columns: 1fr 1fr;
  }

  .process-strip,
  .risk-grid,
  .episode-columns,
  .thread-columns {
    grid-template-columns: 1fr;
  }
}

@media (prefers-reduced-motion: reduce) {
  .reveal-in,
  .nav-pill,
  .cta-primary,
  .cta-secondary {
    animation: none;
    transition: none;
  }
}
</style>
