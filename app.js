const PROMOTIONS = [
  { id: 'ALL', label: 'All Events', color: '#ffffff' },
  { id: 'UFC', label: 'UFC', color: '#e11d2a' },
  { id: 'PFL', label: 'PFL', color: '#2575fc' },
  { id: 'RAF', label: 'RAF Wrestling', color: '#eab308' },
  { id: 'BKFC', label: 'Bare Knuckle FC', color: '#f97316' },
];

const LIVE_WINDOW_MS = 5 * 60 * 60 * 1000; // Treat event as LIVE for 5h after start
const POLL_INTERVAL_MS = 5 * 60 * 1000;    // Auto-check for updated data every 5 minutes

const state = {
  events: [],
  sources: {},
  lastUpdated: null,
  liveSyncAvailable: false,
  activePromo: 'ALL',
  searchQuery: '',
  viewMode: 'grid', // 'grid' | 'timeline'
  timezone: 'local',
  modalEventId: null,
};

// DOM References
const heroSection = document.getElementById('heroSection');
const filterPillsEl = document.getElementById('filterPills');
const eventsContainer = document.getElementById('eventsContainer');
const scheduleSummary = document.getElementById('scheduleSummary');
const sourceBadgesEl = document.getElementById('sourceBadges');
const searchInput = document.getElementById('searchInput');
const clearSearchBtn = document.getElementById('clearSearchBtn');
const timezoneSelect = document.getElementById('timezoneSelect');
const syncNowBtn = document.getElementById('syncNowBtn');
const syncStatusText = document.getElementById('syncStatusText');
const syncDot = document.getElementById('syncDot');
const eventModal = document.getElementById('eventModal');
const modalBody = document.getElementById('modalBody');
const modalCloseBtn = document.getElementById('modalCloseBtn');
const toastEl = document.getElementById('toast');

let toastTimer = null;

// ============================================================================
// Utilities: Escaping, Dates, Timezones, Countdowns, Calendar (.ics)
// ============================================================================

function esc(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function pad2(n) {
  return String(Math.max(0, Math.floor(n))).padStart(2, '0');
}

function getEffectiveTimezone() {
  if (state.timezone === 'local') {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  }
  return state.timezone;
}

function formatLocation(ev) {
  const venue = (ev.venue || '').trim();
  const loc = (ev.location || '').trim();
  if (!venue && !loc) return 'Location TBA';
  if (!venue || venue.toLowerCase() === loc.toLowerCase()) return loc;
  if (!loc) return venue;
  if (loc.toLowerCase().includes(venue.toLowerCase())) return loc;
  return `${venue} · ${loc}`;
}

function formatEventDateParts(isoString) {
  const dt = new Date(isoString);
  const tz = getEffectiveTimezone();

  const weekday = new Intl.DateTimeFormat('en-US', { weekday: 'short', timeZone: tz }).format(dt);
  const weekdayLong = new Intl.DateTimeFormat('en-US', { weekday: 'long', timeZone: tz }).format(dt);
  const monthShort = new Intl.DateTimeFormat('en-US', { month: 'short', timeZone: tz }).format(dt);
  const monthYear = new Intl.DateTimeFormat('en-US', { month: 'long', year: 'numeric', timeZone: tz }).format(dt);
  const dayNum = new Intl.DateTimeFormat('en-US', { day: 'numeric', timeZone: tz }).format(dt);
  const timeStr = new Intl.DateTimeFormat('en-US', {
    hour: 'numeric',
    minute: '2-digit',
    timeZoneName: 'short',
    timeZone: tz,
  }).format(dt);

  return {
    weekday,
    weekdayLong,
    monthShort,
    monthYear,
    dayNum,
    timeStr,
    shortLine: `${weekday}, ${monthShort} ${dayNum}`,
    fullLine: `${weekdayLong}, ${monthShort} ${dayNum} · ${timeStr}`,
  };
}

function getCountdown(isoString, nowMs = Date.now()) {
  const targetMs = new Date(isoString).getTime();
  const diff = targetMs - nowMs;

  if (diff <= 0 && diff > -LIVE_WINDOW_MS) {
    return { status: 'live', days: 0, hours: 0, minutes: 0, seconds: 0, diff };
  }
  if (diff <= -LIVE_WINDOW_MS) {
    return { status: 'ended', days: 0, hours: 0, minutes: 0, seconds: 0, diff };
  }

  const totalSec = Math.floor(diff / 1000);
  const days = Math.floor(totalSec / 86400);
  const hours = Math.floor((totalSec % 86400) / 3600);
  const minutes = Math.floor((totalSec % 3600) / 60);
  const seconds = totalSec % 60;

  return { status: 'upcoming', days, hours, minutes, seconds, diff };
}

function getRelativeBadgeText(cd) {
  if (cd.status === 'live') return 'LIVE NOW';
  if (cd.days === 0) return 'TODAY';
  if (cd.days === 1) return 'TOMORROW';
  if (cd.days <= 7) return `IN ${cd.days} DAYS`;
  return `IN ${cd.days} DAYS`;
}

function formatRelativeAgo(isoString) {
  if (!isoString) return 'Not synced yet';
  const diffSec = Math.max(0, Math.floor((Date.now() - new Date(isoString).getTime()) / 1000));
  if (diffSec < 60) return 'Synced just now';
  const mins = Math.floor(diffSec / 60);
  if (mins < 60) return `Synced ${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `Synced ${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `Synced ${days}d ago`;
}

function showToast(message) {
  toastEl.textContent = message;
  toastEl.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toastEl.classList.add('hidden');
  }, 4200);
}

function downloadICS(ev) {
  const start = new Date(ev.startTime);
  const end = new Date(start.getTime() + 3 * 60 * 60 * 1000);
  const fmt = (d) => d.toISOString().replace(/[-:]/g, '').replace(/\.\d{3}Z$/, 'Z');
  const loc = formatLocation(ev).replace(/,/g, '\\,');
  const summary = `${ev.name} (${ev.promotionName})`.replace(/,/g, '\\,');
  const desc = `Official Event Page: ${ev.url || ''}`.replace(/\n/g, '\\n');

  const ics = [
    'BEGIN:VCALENDAR',
    'VERSION:2.0',
    'PRODID:-//Fight Night Countdown//EN',
    'BEGIN:VEVENT',
    `UID:${ev.id}@fightnightcountdown`,
    `DTSTAMP:${fmt(new Date())}`,
    `DTSTART:${fmt(start)}`,
    `DTEND:${fmt(end)}`,
    `SUMMARY:${summary}`,
    `LOCATION:${loc}`,
    `DESCRIPTION:${desc}`,
    'END:VEVENT',
    'END:VCALENDAR',
  ].join('\r\n');

  const blob = new Blob([ics], { type: 'text/calendar;charset=utf-8' });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = `${ev.id}.ics`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(link.href);
  showToast(`Calendar invite downloaded for ${ev.name}`);
}

// ============================================================================
// Data Loading & Live Publisher Sync
// ============================================================================

function normalizeEvents(rawEvents) {
  const now = Date.now();
  return (rawEvents || [])
    .filter((ev) => {
      const ms = new Date(ev.startTime).getTime();
      return !Number.isNaN(ms) && ms > now - LIVE_WINDOW_MS;
    })
    .sort((a, b) => new Date(a.startTime).getTime() - new Date(b.startTime).getTime());
}

async function loadEvents({ silent = false } = {}) {
  const cacheBust = `t=${Date.now()}`;
  let payload = null;

  // Prefer live server API if running via server.py; gracefully fall back to static GitHub Pages JSON
  try {
    const apiRes = await fetch(`/api/events?${cacheBust}`, { cache: 'no-store' });
    if (apiRes.ok) {
      const ct = apiRes.headers.get('content-type') || '';
      if (ct.includes('application/json')) {
        payload = await apiRes.json();
        state.liveSyncAvailable = Boolean(payload.liveSyncAvailable);
      }
    }
  } catch {
    // Running on static host (e.g. GitHub Pages)
  }

  if (!payload) {
    const staticRes = await fetch(`./data/events.json?${cacheBust}`, { cache: 'no-store' });
    if (!staticRes.ok) {
      throw new Error(`Unable to load events data (HTTP ${staticRes.status})`);
    }
    payload = await staticRes.json();
    state.liveSyncAvailable = false;
  }

  const prevIds = new Set(state.events.map((e) => e.id));
  const nextEvents = normalizeEvents(payload.events);

  state.events = nextEvents;
  state.sources = payload.sources || {};
  state.lastUpdated = payload.lastUpdated || null;

  renderAll();

  if (silent && prevIds.size > 0) {
    const newlyAdded = nextEvents.filter((e) => !prevIds.has(e.id));
    if (newlyAdded.length > 0) {
      showToast(`${newlyAdded.length} newly announced event${newlyAdded.length > 1 ? 's' : ''} added to the schedule!`);
    }
  }
}

async function triggerSyncNow() {
  syncNowBtn.classList.add('is-loading');
  syncNowBtn.disabled = true;
  syncDot.classList.add('syncing');
  syncStatusText.textContent = state.liveSyncAvailable ? 'Syncing publishers...' : 'Checking for updates...';

  try {
    if (state.liveSyncAvailable) {
      const res = await fetch('/api/sync', { method: 'POST' });
      if (!res.ok) throw new Error(`Sync failed (HTTP ${res.status})`);
      const payload = await res.json();
      state.events = normalizeEvents(payload.events);
      state.sources = payload.sources || {};
      state.lastUpdated = payload.lastUpdated || new Date().toISOString();
      renderAll();

      const addedCount = (payload.newEventIds || []).length;
      showToast(
        addedCount > 0
          ? `Synced all 4 publishers: +${addedCount} new event${addedCount > 1 ? 's' : ''} added!`
          : `All 4 publishers up to date (${state.events.length} upcoming events).`
      );
    } else {
      await loadEvents({ silent: false });
      showToast(`Checked latest synced schedule (${state.events.length} upcoming events).`);
    }
  } catch (err) {
    showToast(`Sync issue: ${err.message}`);
  } finally {
    syncNowBtn.classList.remove('is-loading');
    syncNowBtn.disabled = false;
    syncDot.classList.remove('syncing');
    updateSyncStatusLabel();
  }
}

function updateSyncStatusLabel() {
  syncStatusText.textContent = formatRelativeAgo(state.lastUpdated);
}

// ============================================================================
// Filtering & Rendering
// ============================================================================

function getFilteredEvents() {
  const q = state.searchQuery.trim().toLowerCase();
  return state.events.filter((ev) => {
    if (state.activePromo !== 'ALL' && ev.promotion !== state.activePromo) {
      return false;
    }
    if (!q) return true;
    const haystack = [
      ev.name,
      ev.mainEvent,
      ev.promotion,
      ev.promotionName,
      ev.venue,
      ev.location,
    ]
      .join(' ')
      .toLowerCase();
    return haystack.includes(q);
  });
}

function renderPosterMarkup(ev, altPrefix = '') {
  const fallback = `
    <div class="poster-fallback">
      <div class="poster-fallback__promo">${esc(ev.promotionName || ev.promotion)}</div>
      <div class="poster-fallback__name">${esc(ev.name)}</div>
    </div>
  `;
  if (!ev.poster) return fallback;
  return `
    <img
      src="${esc(ev.poster)}"
      alt="${esc(altPrefix + ev.name + ' official poster')}"
      loading="lazy"
      referrerpolicy="no-referrer"
      onerror="this.parentElement.innerHTML = decodeURIComponent('${encodeURIComponent(fallback)}')"
    />
  `;
}

function renderFilterPills() {
  const counts = { ALL: state.events.length, UFC: 0, PFL: 0, RAF: 0, BKFC: 0 };
  for (const ev of state.events) {
    if (counts[ev.promotion] !== undefined) counts[ev.promotion] += 1;
  }

  filterPillsEl.innerHTML = PROMOTIONS.map((p) => {
    const isActive = state.activePromo === p.id;
    const dotStyle = p.id === 'ALL' ? '' : `<span class="filter-pill__dot" style="background:${p.color}"></span>`;
    return `
      <button
        type="button"
        role="tab"
        aria-selected="${isActive}"
        class="filter-pill ${isActive ? 'active' : ''}"
        data-promo="${p.id}"
      >
        ${dotStyle}
        <span>${esc(p.label)}</span>
        <span class="filter-pill__count">${counts[p.id] ?? 0}</span>
      </button>
    `;
  }).join('');
}

function renderHero(filteredEvents) {
  const nextEvent = filteredEvents[0];
  if (!nextEvent) {
    heroSection.innerHTML = `
      <div class="hero__skeleton">
        No upcoming events match your current filter. Try selecting <strong>All Events</strong> or clearing your search.
      </div>
    `;
    return;
  }

  const cd = getCountdown(nextEvent.startTime);
  const dp = formatEventDateParts(nextEvent.startTime);
  const locText = formatLocation(nextEvent);
  const glowColor = PROMOTIONS.find((p) => p.id === nextEvent.promotion)?.color || '#e11d2a';

  const statusPill =
    cd.status === 'live'
      ? `<span class="eyebrow-pill eyebrow-pill--live">LIVE NOW</span>`
      : `<span class="eyebrow-pill">NEXT UP</span>
         <span class="eyebrow-pill eyebrow-pill--soon" data-hero-rel="${esc(nextEvent.id)}">${esc(getRelativeBadgeText(cd))}</span>`;

  const timerHtml =
    cd.status === 'live'
      ? `<div class="hero-live-banner" data-timer-id="${esc(nextEvent.id)}" data-timer-mode="hero">Event In Progress &mdash; Live Now</div>`
      : `
        <div class="hero-timer" data-timer-id="${esc(nextEvent.id)}" data-timer-mode="hero" aria-label="Live countdown to ${esc(nextEvent.name)}">
          <div class="hero-timer__unit">
            <span class="hero-timer__num" data-unit="days">${cd.days}</span>
            <span class="hero-timer__label">Days</span>
          </div>
          <div class="hero-timer__unit">
            <span class="hero-timer__num" data-unit="hours">${pad2(cd.hours)}</span>
            <span class="hero-timer__label">Hours</span>
          </div>
          <div class="hero-timer__unit">
            <span class="hero-timer__num" data-unit="minutes">${pad2(cd.minutes)}</span>
            <span class="hero-timer__label">Minutes</span>
          </div>
          <div class="hero-timer__unit">
            <span class="hero-timer__num" data-unit="seconds">${pad2(cd.seconds)}</span>
            <span class="hero-timer__label">Seconds</span>
          </div>
        </div>
      `;

  heroSection.innerHTML = `
    <div class="hero__glow" style="background: radial-gradient(circle at 75% 35%, ${glowColor}, transparent 62%);"></div>
    <div class="hero__grid">
      <div class="hero__info">
        <div class="hero__badges">
          ${statusPill}
          <span class="promo-badge promo-badge--${esc(nextEvent.promotion)}">${esc(nextEvent.promotionName)}</span>
        </div>

        <h1 class="hero__title">${esc(nextEvent.name)}</h1>

        <div class="hero__meta">
          <div class="meta-row">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <rect x="3" y="4" width="18" height="18" rx="2" />
              <line x1="16" y1="2" x2="16" y2="6" />
              <line x1="8" y1="2" x2="8" y2="6" />
              <line x1="3" y1="10" x2="21" y2="10" />
            </svg>
            <span><strong>${esc(dp.fullLine)}</strong></span>
          </div>
          <div class="meta-row">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z" />
              <circle cx="12" cy="10" r="3" />
            </svg>
            <span>${esc(locText)}</span>
          </div>
        </div>

        ${timerHtml}

        <div class="hero__actions">
          <button type="button" class="btn btn--primary" data-open-event="${esc(nextEvent.id)}">
            Event Details
          </button>
          <button type="button" class="btn btn--secondary" data-download-ics="${esc(nextEvent.id)}">
            Add to Calendar
          </button>
          ${
            nextEvent.url
              ? `<a href="${esc(nextEvent.url)}" target="_blank" rel="noopener noreferrer" class="btn btn--ghost">Official Page &#8599;</a>`
              : ''
          }
        </div>
      </div>

      <div class="hero__poster-frame" data-open-event="${esc(nextEvent.id)}" role="button" tabindex="0" aria-label="View details for ${esc(nextEvent.name)}">
        ${renderPosterMarkup(nextEvent)}
      </div>
    </div>
  `;
}

function renderCardTimer(ev) {
  const cd = getCountdown(ev.startTime);
  if (cd.status === 'live') {
    return `<div class="card-live-pill" data-timer-id="${esc(ev.id)}" data-timer-mode="card">&#9679; LIVE NOW</div>`;
  }
  return `
    <div class="card-timer" data-timer-id="${esc(ev.id)}" data-timer-mode="card">
      <div class="card-timer__cell">
        <span class="card-timer__num" data-unit="days">${cd.days}</span>
        <span class="card-timer__lbl">Days</span>
      </div>
      <div class="card-timer__cell">
        <span class="card-timer__num" data-unit="hours">${pad2(cd.hours)}</span>
        <span class="card-timer__lbl">Hrs</span>
      </div>
      <div class="card-timer__cell">
        <span class="card-timer__num" data-unit="minutes">${pad2(cd.minutes)}</span>
        <span class="card-timer__lbl">Min</span>
      </div>
      <div class="card-timer__cell">
        <span class="card-timer__num" data-unit="seconds">${pad2(cd.seconds)}</span>
        <span class="card-timer__lbl">Sec</span>
      </div>
    </div>
  `;
}

function renderGridCard(ev, orderIndex) {
  const dp = formatEventDateParts(ev.startTime);
  const locText = formatLocation(ev);
  const isFirst = orderIndex === 0;

  return `
    <article class="event-card ${isFirst ? 'event-card--next' : ''}" data-open-event="${esc(ev.id)}" tabindex="0" role="button" aria-label="${esc(ev.name)}">
      <div class="event-card__poster">
        ${renderPosterMarkup(ev)}
        <div class="event-card__poster-overlay"></div>
        <div class="event-card__top-tags">
          <span class="promo-badge promo-badge--${esc(ev.promotion)}">${esc(ev.promotionName)}</span>
          <span class="order-pill ${isFirst ? 'order-pill--next' : ''}">${isFirst ? 'NEXT UP' : `#${orderIndex + 1}`}</span>
        </div>
      </div>

      <div class="event-card__body">
        <div class="event-card__date-row">
          <span class="event-card__weekday">${esc(dp.shortLine)}</span>
          <span>${esc(dp.timeStr)}</span>
        </div>

        <h3 class="event-card__title">${esc(ev.name)}</h3>

        <div class="event-card__location">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z" />
            <circle cx="12" cy="10" r="3" />
          </svg>
          <span>${esc(locText)}</span>
        </div>

        ${renderCardTimer(ev)}
      </div>
    </article>
  `;
}

function renderTimelineRow(ev, orderIndex) {
  const dp = formatEventDateParts(ev.startTime);
  const locText = formatLocation(ev);
  const isFirst = orderIndex === 0;

  return `
    <article class="timeline-row" data-open-event="${esc(ev.id)}" tabindex="0" role="button" aria-label="${esc(ev.name)}">
      <div class="timeline-row__datebox">
        <span class="timeline-row__dow">${esc(dp.weekday)}</span>
        <span class="timeline-row__day">${esc(dp.dayNum)}</span>
        <span class="timeline-row__mon">${esc(dp.monthShort)}</span>
      </div>

      <div class="timeline-row__poster">
        ${renderPosterMarkup(ev)}
      </div>

      <div class="timeline-row__info">
        <div class="timeline-row__top">
          <span class="promo-badge promo-badge--${esc(ev.promotion)}">${esc(ev.promotionName)}</span>
          ${isFirst ? `<span class="order-pill order-pill--next">NEXT UP</span>` : `<span class="order-pill">#${orderIndex + 1}</span>`}
          <span class="timeline-row__time">${esc(dp.timeStr)}</span>
        </div>
        <h3 class="timeline-row__title">${esc(ev.name)}</h3>
        <div class="event-card__location">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z" />
            <circle cx="12" cy="10" r="3" />
          </svg>
          <span>${esc(locText)}</span>
        </div>
      </div>

      <div class="timeline-row__timer">
        ${renderCardTimer(ev)}
      </div>
    </article>
  `;
}

function renderSchedule(filteredEvents) {
  const promoLabel = PROMOTIONS.find((p) => p.id === state.activePromo)?.label || 'All Events';
  scheduleSummary.innerHTML = `Showing <strong>${filteredEvents.length}</strong> upcoming ${
    state.activePromo === 'ALL' ? 'combat sports' : esc(promoLabel)
  } event${filteredEvents.length === 1 ? '' : 's'}${
    state.searchQuery.trim() ? ` matching &ldquo;<strong>${esc(state.searchQuery.trim())}</strong>&rdquo;` : ''
  }`;

  if (filteredEvents.length === 0) {
    eventsContainer.innerHTML = `
      <div class="empty-state">
        <h3>No matching events found</h3>
        <p>Try switching back to All Events or clearing your search query.</p>
        <button type="button" class="btn btn--secondary" id="resetFiltersBtn">Reset Filters</button>
      </div>
    `;
    const resetBtn = document.getElementById('resetFiltersBtn');
    if (resetBtn) {
      resetBtn.addEventListener('click', () => {
        state.activePromo = 'ALL';
        state.searchQuery = '';
        searchInput.value = '';
        clearSearchBtn.classList.add('hidden');
        renderAll();
      });
    }
    return;
  }

  // Group chronologically by Month + Year while preserving global chronological order index
  const groups = [];
  const groupMap = new Map();
  filteredEvents.forEach((ev, idx) => {
    const dp = formatEventDateParts(ev.startTime);
    if (!groupMap.has(dp.monthYear)) {
      const grp = { label: dp.monthYear, items: [] };
      groupMap.set(dp.monthYear, grp);
      groups.push(grp);
    }
    groupMap.get(dp.monthYear).items.push({ ev, orderIndex: idx });
  });

  eventsContainer.innerHTML = groups
    .map((grp) => {
      const innerHtml =
        state.viewMode === 'timeline'
          ? `<div class="events-timeline">${grp.items.map((x) => renderTimelineRow(x.ev, x.orderIndex)).join('')}</div>`
          : `<div class="events-grid">${grp.items.map((x) => renderGridCard(x.ev, x.orderIndex)).join('')}</div>`;

      return `
        <div class="month-group">
          <div class="month-group__header">
            <h2 class="month-group__title">${esc(grp.label)}</h2>
            <span class="month-group__line"></span>
            <span class="month-group__count">${grp.items.length} event${grp.items.length === 1 ? '' : 's'}</span>
          </div>
          ${innerHtml}
        </div>
      `;
    })
    .join('');
}

function renderSourcesFooter() {
  const entries = [
    { key: 'UFC', label: 'UFC.com', url: 'https://www.ufc.com/events' },
    { key: 'PFL', label: 'PFLMMA.com', url: 'https://www.pflmma.com/events' },
    { key: 'RAF', label: 'RealAmericanFreestyle.com', url: 'https://www.realamericanfreestyle.com' },
    { key: 'BKFC', label: 'BKFC.com', url: 'https://www.bkfc.com/events' },
  ];

  sourceBadgesEl.innerHTML = entries
    .map((src) => {
      const meta = state.sources[src.key] || {};
      const ok = meta.status === 'ok' || !meta.status;
      const count = meta.count ?? state.events.filter((e) => e.promotion === src.key).length;
      return `
        <a href="${esc(meta.url || src.url)}" target="_blank" rel="noopener noreferrer" class="source-badge">
          <span class="source-badge__dot ${ok ? '' : 'source-badge__dot--warn'}"></span>
          <span>${esc(src.label)}</span>
          <span class="source-badge__count">${count}</span>
        </a>
      `;
    })
    .join('');
}

function openEventModal(eventId) {
  const ev = state.events.find((e) => e.id === eventId);
  if (!ev) return;
  state.modalEventId = ev.id;

  const dp = formatEventDateParts(ev.startTime);
  const cd = getCountdown(ev.startTime);
  const locText = formatLocation(ev);

  const slots = [
    { label: 'Main Card / Start', val: dp.fullLine },
  ];
  if (ev.prelimsTime) {
    slots.push({ label: 'Prelims', val: formatEventDateParts(ev.prelimsTime).fullLine });
  }
  if (ev.earlyPrelimsTime) {
    slots.push({ label: 'Early Prelims', val: formatEventDateParts(ev.earlyPrelimsTime).fullLine });
  }
  slots.push({ label: 'Location', val: locText });
  if (ev.source) {
    slots.push({ label: 'Official Source', val: ev.source });
  }

  const timerBlock =
    cd.status === 'live'
      ? `<div class="hero-live-banner" data-timer-id="${esc(ev.id)}" data-timer-mode="modal">Event In Progress &mdash; Live Now</div>`
      : `
        <div class="hero-timer" data-timer-id="${esc(ev.id)}" data-timer-mode="modal" style="max-width:100%; margin: 16px 0;">
          <div class="hero-timer__unit">
            <span class="hero-timer__num" data-unit="days">${cd.days}</span>
            <span class="hero-timer__label">Days</span>
          </div>
          <div class="hero-timer__unit">
            <span class="hero-timer__num" data-unit="hours">${pad2(cd.hours)}</span>
            <span class="hero-timer__label">Hours</span>
          </div>
          <div class="hero-timer__unit">
            <span class="hero-timer__num" data-unit="minutes">${pad2(cd.minutes)}</span>
            <span class="hero-timer__label">Minutes</span>
          </div>
          <div class="hero-timer__unit">
            <span class="hero-timer__num" data-unit="seconds">${pad2(cd.seconds)}</span>
            <span class="hero-timer__label">Seconds</span>
          </div>
        </div>
      `;

  modalBody.innerHTML = `
    <div class="modal__poster">
      ${renderPosterMarkup(ev)}
    </div>
    <div class="modal__content">
      <div class="hero__badges">
        <span class="promo-badge promo-badge--${esc(ev.promotion)}">${esc(ev.promotionName)}</span>
        <span class="eyebrow-pill eyebrow-pill--soon">${esc(getRelativeBadgeText(cd))}</span>
      </div>
      <h2 id="modalTitle" class="modal__title">${esc(ev.name)}</h2>
      ${timerBlock}
      <div class="modal__schedule-list">
        ${slots
          .map(
            (s) => `
          <div class="modal__slot">
            <div class="modal__slot-label">${esc(s.label)}</div>
            <div class="modal__slot-val">${esc(s.val)}</div>
          </div>
        `
          )
          .join('')}
      </div>
      <div class="modal__actions">
        <button type="button" class="btn btn--primary" data-download-ics="${esc(ev.id)}">
          Add to Calendar (.ics)
        </button>
        ${
          ev.url
            ? `<a href="${esc(ev.url)}" target="_blank" rel="noopener noreferrer" class="btn btn--secondary">Open Official Event Page &#8599;</a>`
            : ''
        }
      </div>
    </div>
  `;

  eventModal.classList.remove('hidden');
  document.body.style.overflow = 'hidden';
}

function closeEventModal() {
  state.modalEventId = null;
  eventModal.classList.add('hidden');
  document.body.style.overflow = '';
}

function renderAll() {
  const filtered = getFilteredEvents();
  renderFilterPills();
  renderHero(filtered);
  renderSchedule(filtered);
  renderSourcesFooter();
  updateSyncStatusLabel();
}

// ============================================================================
// Live 1-Second Ticker (Updates every countdown in place with zero flicker)
// ============================================================================

function tickAllTimers() {
  const nowMs = Date.now();
  let needsFullRerender = false;
  const eventById = new Map(state.events.map((e) => [e.id, e]));

  document.querySelectorAll('[data-timer-id]').forEach((el) => {
    const ev = eventById.get(el.dataset.timerId);
    if (!ev) return;
    const cd = getCountdown(ev.startTime, nowMs);

    if (cd.status === 'ended') {
      needsFullRerender = true;
      return;
    }

    if (cd.status === 'live') {
      if (el.querySelector('[data-unit="seconds"]')) {
        needsFullRerender = true;
      }
      return;
    }

    const dEl = el.querySelector('[data-unit="days"]');
    const hEl = el.querySelector('[data-unit="hours"]');
    const mEl = el.querySelector('[data-unit="minutes"]');
    const sEl = el.querySelector('[data-unit="seconds"]');
    if (dEl) dEl.textContent = String(cd.days);
    if (hEl) hEl.textContent = pad2(cd.hours);
    if (mEl) mEl.textContent = pad2(cd.minutes);
    if (sEl) sEl.textContent = pad2(cd.seconds);
  });

  if (needsFullRerender) {
    state.events = normalizeEvents(state.events);
    renderAll();
  }
}

// ============================================================================
// Event Listeners & Boot
// ============================================================================

filterPillsEl.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-promo]');
  if (!btn) return;
  state.activePromo = btn.dataset.promo;
  renderAll();
});

searchInput.addEventListener('input', () => {
  state.searchQuery = searchInput.value;
  clearSearchBtn.classList.toggle('hidden', !state.searchQuery);
  renderAll();
});

clearSearchBtn.addEventListener('click', () => {
  state.searchQuery = '';
  searchInput.value = '';
  clearSearchBtn.classList.add('hidden');
  searchInput.focus();
  renderAll();
});

document.querySelectorAll('.view-toggle__btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    state.viewMode = btn.dataset.view;
    document.querySelectorAll('.view-toggle__btn').forEach((b) => {
      const active = b.dataset.view === state.viewMode;
      b.classList.toggle('active', active);
      b.setAttribute('aria-pressed', String(active));
    });
    renderSchedule(getFilteredEvents());
  });
});

timezoneSelect.addEventListener('change', () => {
  state.timezone = timezoneSelect.value;
  renderAll();
  if (state.modalEventId) {
    openEventModal(state.modalEventId);
  }
});

syncNowBtn.addEventListener('click', triggerSyncNow);

// Delegate card clicks, calendar button clicks, and keyboard activation
document.addEventListener('click', (e) => {
  const icsBtn = e.target.closest('[data-download-ics]');
  if (icsBtn) {
    e.stopPropagation();
    const ev = state.events.find((x) => x.id === icsBtn.dataset.downloadIcs);
    if (ev) downloadICS(ev);
    return;
  }

  const openEl = e.target.closest('[data-open-event]');
  if (openEl && !e.target.closest('a, button:not([data-open-event])')) {
    openEventModal(openEl.dataset.openEvent);
    return;
  }

  if (e.target.closest('[data-close-modal="true"]')) {
    closeEventModal();
  }
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !eventModal.classList.contains('hidden')) {
    closeEventModal();
    return;
  }
  if ((e.key === 'Enter' || e.key === ' ') && document.activeElement?.dataset?.openEvent) {
    e.preventDefault();
    openEventModal(document.activeElement.dataset.openEvent);
  }
});

modalCloseBtn.addEventListener('click', closeEventModal);

// Boot
async function init() {
  try {
    await loadEvents();
  } catch (err) {
    heroSection.innerHTML = `<div class="hero__skeleton">Could not load events schedule: ${esc(err.message)}</div>`;
    syncStatusText.textContent = 'Sync unavailable';
  }

  setInterval(tickAllTimers, 1000);
  setInterval(updateSyncStatusLabel, 30000);
  setInterval(() => loadEvents({ silent: true }).catch(() => {}), POLL_INTERVAL_MS);
}

init();
