import { create } from 'zustand'
import type { ChartOverlays, GridItem, Screen, WindowConfig, WindowType } from '../types'
import { api } from '../lib/api'
import { id as newId } from '../lib/ids'
import { WINDOW_SIZES, migrateScannerWindow, windowDefaults } from '../windows/defaults'
import preMarket from '../../defaults/screens/pre-market.json'
import priceAction from '../../defaults/screens/price-action.json'

/** The shipped screens. Both are seeded on a first run (no screens saved yet), in
 *  this order, and stay available as starter layouts that add a NEW screen; a
 *  template is never applied over an existing one. */
export const SCREEN_TEMPLATES = {
  'pre-market': { label: 'Pre-Market', desc: 'Pre-market gainers, losers and volume driving linked charts, news and stock info', screen: preMarket },
  'price-action': { label: 'Price Action', desc: 'Alerts beside intraday and daily charts and News, plus a second link group of rankings and a 1-minute chart', screen: priceAction },
} as const
export type ScreenTemplate = keyof typeof SCREEN_TEMPLATES

const ACTIVE_KEY = 'scanner-v2-active-screen'
const SAVE_DEBOUNCE_MS = 750
export const GRID_VERSION = 3          // 3 = free-floating windows, bounds as workspace fractions
const CASCADE_PX = 28                  // offset of a newly added window from the last one

// Reference workspace for migrating old grid layouts: a 1920x1081 browser inner
// size minus the 40px top bar (--head-h in index.css).
const REF_W = 1920
const REF_H = 1081 - 40

interface ScreensState {
  screens: Record<string, Screen>
  order: string[]
  activeId: string | null
  loaded: boolean
  loadError: string | null
  saveState: 'idle' | 'saving' | 'error'
  /** live workspace size in px, reported by <Workspace>; used to size new windows */
  ws: { w: number; h: number }
  setWorkspaceSize(w: number, h: number): void
  load(): Promise<void>
  setActive(id: string): void
  createScreen(name: string): string
  /** Add a starter layout as a new screen and switch to it. */
  createFromTemplate(tpl: ScreenTemplate): string
  renameScreen(id: string, name: string): void
  duplicateScreen(id: string, name?: string): string
  deleteScreen(id: string): void
  setLocked(locked: boolean): void
  saveChartDefaults(overlays: ChartOverlays): void
  addWindow(type: WindowType, partial?: Partial<WindowConfig>): string
  removeWindow(id: string): void
  updateWindow(id: string, patch: Record<string, unknown>): void
  updateBounds(id: string, patch: Partial<Omit<GridItem, 'i'>>): void
  bringToFront(id: string): void
  toggleMaximize(id: string): void
}

// ── migrations ───────────────────────────────────────────────────────────────

/** grid 1 (12 cols x 28px) -> grid 2 (24 cols x 14px): everything doubles. */
function migrateV1toV2(s: Screen): Screen {
  const k = 2
  return {
    ...s, grid: 2,
    layout: s.layout.map(l => ({
      ...l, x: l.x * k, w: l.w * k, y: l.y * k, h: l.h * k,
      ...(l.minW != null ? { minW: l.minW * k } : {}), ...(l.minH != null ? { minH: l.minH * k } : {}),
      ...(l.maxH != null ? { maxH: l.maxH * k } : {}),
    })),
  }
}

/** grid 2 (react-grid-layout: 24 cols, 14px rows, 6px margins) -> grid 3 (workspace fractions). */
function migrateV2toV3(s: Screen): Screen {
  const COLS = 24, ROW = 14, M = 6
  const colW = (REF_W - M * (COLS - 1)) / COLS
  const xpx = (c: number) => c * (colW + M)
  const wpx = (c: number) => Math.max(1, c * colW + (c - 1) * M)
  const ypx = (r: number) => r * (ROW + M)
  const hpx = (r: number) => Math.max(1, r * (ROW + M) - M)
  const px = s.layout.map(l => ({
    l, x: xpx(l.x), y: ypx(l.y), w: wpx(l.w), h: hpx(l.h),
    minW: l.minW != null ? wpx(l.minW) : undefined,
    minH: l.minH != null ? hpx(l.minH) : undefined,
    maxH: l.maxH != null ? hpx(l.maxH) : undefined,
  }))
  // If the grid layout was taller than the reference workspace, squeeze vertically so
  // every window fits without scrolling instead of clipping the bottom ones.
  const bottom = px.reduce((m, p) => Math.max(m, p.y + p.h), 0)
  const ky = bottom > REF_H ? REF_H / bottom : 1
  const layout: GridItem[] = px.map((p, i) => ({
    i: p.l.i,
    x: clamp01(p.x / REF_W), y: clamp01((p.y * ky) / REF_H),
    w: clamp01(p.w / REF_W), h: clamp01((p.h * ky) / REF_H),
    z: i + 1,
    ...(p.minW != null ? { minW: clamp01(p.minW / REF_W) } : {}),
    ...(p.minH != null ? { minH: clamp01((p.minH * ky) / REF_H) } : {}),
    ...(p.maxH != null ? { maxH: clamp01((p.maxH * ky) / REF_H) } : {}),
  }))
  return { ...s, grid: GRID_VERSION, layout }
}

const clamp01 = (v: number) => Math.min(1, Math.max(0, v))

/** Bring any saved screen up to the current grid version. */
export function migrateGrid(s: Screen): { screen: Screen; changed: boolean } {
  let cur = s
  let changed = false
  if ((cur.grid ?? 1) < 2) { cur = migrateV1toV2(cur); changed = true }
  if ((cur.grid ?? 1) < 3) { cur = migrateV2toV3(cur); changed = true }
  // defensive: every item needs a z
  if (cur.layout.some(l => typeof l.z !== 'number')) {
    cur = { ...cur, layout: cur.layout.map((l, i) => ({ ...l, z: typeof l.z === 'number' ? l.z : i + 1 })) }
    changed = true
  }
  // Size limits now come from the window type (Workspace), so stored minW/minH/maxH
  // are dropped. A clock strip squeezed below its content height by an old grid
  // migration (minH == maxH) is given room again.
  const CLOCK_MIN_H = 48 / REF_H
  if (cur.layout.some(l => l.minW != null || l.minH != null || l.maxH != null || (cur.windows[l.i]?.type === 'clock' && l.h < CLOCK_MIN_H))) {
    cur = { ...cur, layout: cur.layout.map(l => {
      const { minW: _a, minH: _b, maxH: _c, ...rest } = l
      void _a; void _b; void _c
      return cur.windows[l.i]?.type === 'clock' && rest.h < CLOCK_MIN_H ? { ...rest, h: CLOCK_MIN_H } : rest
    }) }
    changed = true
  }
  // scanner windows: `feed` -> `sources`, retired sources/triggers dropped
  const migratedWins: Record<string, WindowConfig> = {}
  let winChanged = false
  for (const [id, w] of Object.entries(cur.windows)) {
    const m = migrateScannerWindow(w)
    migratedWins[id] = m ?? w
    if (m) winChanged = true
  }
  if (winChanged) { cur = { ...cur, windows: migratedWins }; changed = true }
  return { screen: cur, changed }
}

// ── server persistence (debounced per screen) ───────────────────────────────
const timers = new Map<string, ReturnType<typeof setTimeout>>()
function schedulePersist(get: () => ScreensState, set: (p: Partial<ScreensState>) => void, id: string) {
  const t = timers.get(id)
  if (t) clearTimeout(t)
  timers.set(id, setTimeout(async () => {
    timers.delete(id)
    const screen = get().screens[id]
    if (!screen) return
    set({ saveState: 'saving' })
    try {
      await api.layouts.save(screen)
      set({ saveState: 'idle' })
    } catch (e) {
      console.warn('layout save failed', e)
      set({ saveState: 'error' })
    }
  }, SAVE_DEBOUNCE_MS))
}

const touch = (screen: Screen): Screen => ({ ...screen, updatedAt: new Date().toISOString() })

function freshScreen(name: string): Screen {
  const now = new Date().toISOString()
  return { id: newId('scr'), name, version: 1, grid: GRID_VERSION, createdAt: now, updatedAt: now, locked: false, layout: [], windows: {} }
}

function seedFromDefault(src: unknown): Screen {
  // Re-key the shipped default so several installs never share ids.
  const tpl = migrateGrid(src as Screen).screen
  const s = freshScreen(tpl.name || 'Main')
  const map = new Map<string, string>()
  for (const w of Object.values(tpl.windows ?? {})) map.set(w.id, newId('w'))
  s.windows = Object.fromEntries(Object.values(tpl.windows ?? {}).map(w => {
    const nid = map.get(w.id)!
    return [nid, { ...windowDefaults(w.type, nid), ...w, id: nid } as WindowConfig]
  }))
  s.layout = (tpl.layout ?? []).filter(l => map.has(l.i)).map(l => ({ ...l, i: map.get(l.i)! }))
  return s
}

const maxZ = (layout: GridItem[]) => layout.reduce((m, l) => Math.max(m, l.z ?? 0), 0)

export const useScreens = create<ScreensState>()((set, get) => {
  const mutateActive = (fn: (s: Screen) => Screen) => {
    const { activeId, screens } = get()
    if (!activeId || !screens[activeId]) return
    const next = touch(fn(screens[activeId]))
    set({ screens: { ...screens, [activeId]: next } })
    schedulePersist(get, set, activeId)
  }

  return {
    screens: {},
    order: [],
    activeId: null,
    loaded: false,
    loadError: null,
    saveState: 'idle',
    ws: { w: REF_W, h: REF_H },

    setWorkspaceSize(w, h) {
      if (w > 0 && h > 0) set(st => (st.ws.w === w && st.ws.h === h ? st : { ws: { w, h } }))
    },

    async load() {
      let list: Screen[] = []
      let loadError: string | null = null
      try {
        list = await api.layouts.list()
      } catch (e) {
        loadError = e instanceof Error ? e.message : String(e)
      }
      list = list.filter(s => s && s.version === 1 && s.id && s.windows && Array.isArray(s.layout))
      list = list.map(s => {
        const m = migrateGrid(s)
        if (m.changed && !loadError) api.layouts.save(m.screen).catch(() => { /* retried on next change */ })
        return m.screen
      })
      if (list.length === 0) {
        list = Object.values(SCREEN_TEMPLATES).map(t => ({ ...seedFromDefault(t.screen), name: t.label }))
        if (!loadError) for (const seed of list) api.layouts.save(seed).catch(() => { /* surfaced via saveState later */ })
      }
      const screens: Record<string, Screen> = {}
      for (const s of list) screens[s.id] = s
      let active: string | null = null
      try { active = localStorage.getItem(ACTIVE_KEY) } catch { /* ignore */ }
      if (!active || !screens[active]) active = list[0].id
      set({ screens, order: list.map(s => s.id), activeId: active, loaded: true, loadError })
    },

    setActive(id) {
      if (!get().screens[id]) return
      set({ activeId: id })
      try { localStorage.setItem(ACTIVE_KEY, id) } catch { /* ignore */ }
    },

    createScreen(name) {
      const s = freshScreen(name.trim() || 'New screen')
      set(st => ({ screens: { ...st.screens, [s.id]: s }, order: [...st.order, s.id] }))
      get().setActive(s.id)
      schedulePersist(get, set, s.id)
      return s.id
    },

    createFromTemplate(key) {
      const t = SCREEN_TEMPLATES[key]
      const s = seedFromDefault(t.screen)
      // "Price action", then "Price action 2", ... so a second copy is not confused with the first
      const names = new Set(Object.values(get().screens).map(x => x.name))
      let name: string = t.label
      for (let n = 2; names.has(name); n++) name = `${t.label} ${n}`
      s.name = name
      set(st => ({ screens: { ...st.screens, [s.id]: s }, order: [...st.order, s.id] }))
      get().setActive(s.id)
      schedulePersist(get, set, s.id)
      return s.id
    },

    renameScreen(id, name) {
      const s = get().screens[id]; if (!s) return
      const next = touch({ ...s, name: name.trim() || s.name })
      set(st => ({ screens: { ...st.screens, [id]: next } }))
      schedulePersist(get, set, id)
    },

    duplicateScreen(id, name) {
      const src = get().screens[id]; if (!src) return id
      const s = freshScreen(name?.trim() || `${src.name} copy`)
      const map = new Map<string, string>()
      for (const wid of Object.keys(src.windows)) map.set(wid, newId('w'))
      s.windows = Object.fromEntries(Object.entries(src.windows).map(([wid, w]) => [map.get(wid)!, { ...w, id: map.get(wid)! }]))
      s.layout = src.layout.map(l => ({ ...l, i: map.get(l.i)! }))
      s.locked = src.locked
      if (src.chartOverlays) s.chartOverlays = { ...src.chartOverlays }
      set(st => ({ screens: { ...st.screens, [s.id]: s }, order: [...st.order, s.id] }))
      get().setActive(s.id)
      schedulePersist(get, set, s.id)
      return s.id
    },

    deleteScreen(id) {
      const { screens, order, activeId } = get()
      if (!screens[id]) return
      const nextScreens = { ...screens }; delete nextScreens[id]
      const nextOrder = order.filter(x => x !== id)
      set({ screens: nextScreens, order: nextOrder })
      const t = timers.get(id); if (t) { clearTimeout(t); timers.delete(id) }
      api.layouts.delete(id).catch(() => { /* ignore */ })
      if (nextOrder.length === 0) {
        get().createScreen('Main')
      } else if (activeId === id) {
        get().setActive(nextOrder[0])
      }
    },

    setLocked(locked) { mutateActive(s => ({ ...s, locked })) },

    saveChartDefaults(overlays) { mutateActive(s => ({ ...s, chartOverlays: overlays })) },

    addWindow(type, partial = {}) {
      const wid = newId('w')
      const size = WINDOW_SIZES[type]
      const { w: W, h: H } = get().ws
      mutateActive(s => {
        // Cascade from the most recently raised window (highest z), clamped to the workspace.
        const top = s.layout.reduce<GridItem | null>((m, l) => (!m || l.z > m.z ? l : m), null)
        const wpx = size.w === 0 ? W : Math.min(size.w, W)
        const hpx = Math.min(size.h, H)
        let x = top ? top.x + CASCADE_PX / W : 0.01
        let y = top ? top.y + CASCADE_PX / H : 0.01
        const wf = wpx / W, hf = hpx / H
        if (x + wf > 1) x = Math.max(0, 1 - wf)
        if (y + hf > 1) y = Math.max(0, 1 - hf)
        if (top && Math.abs(x - top.x) < 0.001 && Math.abs(y - top.y) < 0.001) { x = 0.01; y = 0.01 }   // wrapped around
        const item: GridItem = {
          i: wid, x, y, w: wf, h: hf, z: maxZ(s.layout) + 1,
          minW: size.minW / W, minH: size.minH / H, ...(size.maxH ? { maxH: size.maxH / H } : {}),
        }
        const defaults = windowDefaults(type, wid)
        const cfg = { ...defaults, ...(type === 'chart' && s.chartOverlays ? { overlays: s.chartOverlays } : {}), ...partial, id: wid, type } as WindowConfig
        return { ...s, layout: [...s.layout, item], windows: { ...s.windows, [wid]: cfg } }
      })
      return wid
    },

    removeWindow(id) {
      mutateActive(s => {
        const windows = { ...s.windows }; delete windows[id]
        return { ...s, windows, layout: s.layout.filter(l => l.i !== id) }
      })
    },

    updateWindow(id, patch) {
      mutateActive(s => {
        const w = s.windows[id]; if (!w) return s
        return { ...s, windows: { ...s.windows, [id]: { ...w, ...patch } as WindowConfig } }
      })
    },

    updateBounds(id, patch) {
      mutateActive(s => {
        const cur = s.layout.find(l => l.i === id)
        if (!cur) return s
        const next = { ...cur, ...patch }
        const same = (Object.keys(patch) as (keyof GridItem)[]).every(k => cur[k] === next[k])
        if (same) return s
        return { ...s, layout: s.layout.map(l => (l.i === id ? next : l)) }
      })
    },

    bringToFront(id) {
      const { activeId, screens } = get()
      const s = activeId ? screens[activeId] : null
      const cur = s?.layout.find(l => l.i === id)
      if (!s || !cur || cur.z === maxZ(s.layout)) return
      mutateActive(sc => ({ ...sc, layout: sc.layout.map(l => (l.i === id ? { ...l, z: maxZ(sc.layout) + 1 } : l)) }))
    },

    toggleMaximize(id) {
      mutateActive(s => {
        const cur = s.layout.find(l => l.i === id)
        if (!cur) return s
        let next: GridItem
        if (cur.maximized) {
          const r = cur.restore ?? { x: 0.05, y: 0.05, w: 0.6, h: 0.6 }
          next = { ...cur, ...r, maximized: false, restore: undefined, z: maxZ(s.layout) + 1 }
        } else {
          next = { ...cur, restore: { x: cur.x, y: cur.y, w: cur.w, h: cur.h }, x: 0, y: 0, w: 1, h: 1, maximized: true, z: maxZ(s.layout) + 1 }
        }
        return { ...s, layout: s.layout.map(l => (l.i === id ? next : l)) }
      })
    },
  }
})

export function useActiveScreen(): Screen | null {
  return useScreens(s => (s.activeId ? s.screens[s.activeId] ?? null : null))
}
