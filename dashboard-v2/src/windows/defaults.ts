// Per-window-type defaults and grid sizes. Kept free of component imports so
// stores can use it without creating an import cycle through the registry.
import type { FeedId, ScannerConfig, ToplistName, WindowConfig, WindowType } from '../types'

/** Default window size in CSS pixels (converted to workspace fractions when a window is added). */
export interface WindowSize { w: number; h: number; minW: number; minH: number; maxH?: number }

export const WINDOW_TITLES: Record<WindowType, string> = {
  scanner: 'Scanner', chart: 'Chart', toplist: 'Rankings', news: 'News',
  stockinfo: 'Stock Info', watchlist: 'Watchlist', clock: 'Clock', setupcheck: 'Setup check',
}

export const WINDOW_ICONS: Record<WindowType, string> = {
  scanner: '◎', chart: '⌇', toplist: '≡', news: '¶', stockinfo: 'ℹ', watchlist: '☆', clock: '◷', setupcheck: '✓',
}

// Pixels. `w: 0` means "full workspace width" (the clock strip).
export const WINDOW_SIZES: Record<WindowType, WindowSize> = {
  scanner: { w: 940, h: 400, minW: 420, minH: 150 },
  chart: { w: 940, h: 400, minW: 420, minH: 180 },
  toplist: { w: 460, h: 340, minW: 300, minH: 150 },
  news: { w: 620, h: 340, minW: 420, minH: 150 },
  stockinfo: { w: 460, h: 340, minW: 300, minH: 150 },
  watchlist: { w: 460, h: 340, minW: 300, minH: 150 },
  clock: { w: 0, h: 60, minW: 400, minH: 40 },
  setupcheck: { w: 560, h: 420, minW: 360, minH: 180 },
}

export const SCANNER_DEFAULT_COLUMNS: string[] = ['time', 'symbol', 'dir', 'setup', 'price', 'rvol', 'gap']

/** Bring a saved scanner window up to date. Returns null when nothing changes.
 *
 *  - `feed` (one port per producer) became `sources` (a filter on the unified feed).
 *  - Sources are 'system' | 'custom'. "rr" (the old name of the system feed) maps
 *    to 'system'; any other value (a retired producer) is dropped.
 *  - A window that showed ONLY retired sources would silently widen to "all
 *    sources" once they are dropped, so it shows custom setups instead.
 *  - Titles are never touched.
 *  - The retired `triggers` filter (keys of the retired producers) is removed. */
export function migrateScannerWindow(w: WindowConfig): WindowConfig | null {
  if (w.type !== 'scanner') return null
  const sw = w as Omit<ScannerConfig, 'sources'> & { feed?: string; sources?: string[] }
  const rawSources: string[] = Array.isArray(sw.sources) ? sw.sources : sw.feed ? (sw.feed === 'rr' ? ['rr', 'custom'] : [sw.feed]) : []
  const mapped = rawSources.map(s => (s === 'rr' ? 'system' : s)).filter((s): s is FeedId => s === 'system' || s === 'custom')
  const sources = Array.from(new Set(mapped))
  const onlyRetired = rawSources.length > 0 && sources.length === 0
  const { feed: _feed, triggers: _triggers, ...rest } = sw
  void _feed; void _triggers
  const next: ScannerConfig = { ...rest, sources: onlyRetired ? ['custom'] : sources }
  const same = !('feed' in sw) && !('triggers' in sw)
    && rawSources.length === next.sources.length && rawSources.every((s, i) => s === next.sources[i])
  return same ? null : next
}

export const WATCHLIST_DEFAULT_COLUMNS = ['symbol', 'price', 'chg', 'rth', 'rvol', 'vwap']

export const TOPLIST_LABEL: Record<ToplistName, string> = {
  rvol: 'RVOL leaders', gainers_close: 'Gainers (from close)', losers_close: 'Losers (from close)',
  gainers_open: 'Gainers (from open)', losers_open: 'Losers (from open)', movers_5m: '5-min movers',
  pm_gainers: 'Pre-market gainers', pm_losers: 'Pre-market losers', pm_volume: 'Pre-market volume',
  hod_lod: 'New HOD / LOD',
}

const base = (id: string, type: WindowType) => ({
  id, type, link: 'none' as const, muted: false, sound: { tone: 'off' as const, tts: false },
})

export function windowDefaults(type: WindowType, id: string): WindowConfig {
  switch (type) {
    case 'scanner':
      // A new window starts with no setups picked: it shows nothing until you choose.
      return { ...base(id, 'scanner'), type: 'scanner', sources: [], setups: [], noSetups: true, direction: 'all',
        minScore: 0, symbolFilter: '', columns: SCANNER_DEFAULT_COLUMNS, rowTint: true, maxRows: 500 }
    case 'chart':
      return { ...base(id, 'chart'), type: 'chart', symbol: null, timeframe: '5m', extended: false, autoScaleY: true,
        overlays: { vwap: true, ema9: true, ema21: true, volume: true, sma50: false, sma100: false, sma200: false, pdHL: true, pmHL: true } }
    case 'toplist':
      return { ...base(id, 'toplist'), type: 'toplist', list: 'rvol', limit: 25, heat: true }
    case 'news':
      return { ...base(id, 'news'), type: 'news', mode: 'market', symbol: null, hours: 24, limit: 50, thumbnails: true }
    case 'stockinfo':
      return { ...base(id, 'stockinfo'), type: 'stockinfo', symbol: null }
    case 'watchlist':
      return { ...base(id, 'watchlist'), type: 'watchlist', watchlistId: null, columns: WATCHLIST_DEFAULT_COLUMNS }
    case 'clock':
      return { ...base(id, 'clock'), type: 'clock', showSpy: true }
    case 'setupcheck':
      return { ...base(id, 'setupcheck'), type: 'setupcheck', symbol: null, minutes: 5 }
  }
}

/** Windows that follow a link group / publish symbols. */
export const USES_LINK: Record<WindowType, boolean> = {
  scanner: true, chart: true, toplist: true, news: true, stockinfo: true, watchlist: true, clock: false, setupcheck: true,
}
/** Windows that can make noise. */
export const USES_SOUND: Record<WindowType, boolean> = {
  scanner: true, chart: false, toplist: true, news: false, stockinfo: false, watchlist: false, clock: false, setupcheck: false,
}
