// ── Alert payloads (unchanged wire format from the scanner) ──────────────────

export interface Alert {
  symbol: string
  direction: 'long' | 'short' | 'neutral'
  timestamp: string
  price: number
  trigger: string
  triggers_fired: string[]
  score: number
  market_regime: string
  conditions: Record<string, { value: unknown; pass: boolean; status: string }>
  vwap: number | null
  ema3: number | null
  ema9: number | null
  pct_change?: number | null
  rvol?: number | null
  /** a system setup code (from /api/v2/setups `system`) or a custom setup id (cs_*) */
  setup?: string
  setup_label?: string
  setup_color?: string
  custom?: boolean
  trigger_label?: string
  trigger_note?: string
  trigger_value?: number | null
  session?: string
  /** producer on the unified feed (7777 /ws/alerts) */
  source?: FeedId
  seq?: number
  tier?: number | null
  entry_trigger?: string
  fresh_break?: boolean | null
  fade_type?: boolean | null
  closes_5m?: number | null
  size_hint?: 'full' | 'three_quarter' | 'half'
  suggested_stop?: number | null
  stop_pct?: number | null
  stop_ok?: boolean
  management?: string[]
  warnings?: string[]
  gates?: { name: string; passed: boolean; value: number | null; reason: string; mandatory?: boolean }[]
  context?: Record<string, number | boolean | null>
  /** set by the engine: "default" or an 8-hex hash of the non-default parameters in force */
  config_hash?: string
  modified?: boolean
}

export interface Bar { t: string; o: number; h: number; l: number; c: number; v: number }

export type ConnectionStatus = 'connected' | 'reconnecting' | 'disconnected' | 'off'
export type SortDir = 'asc' | 'desc'

// ── Layout / windows ─────────────────────────────────────────────────────────

export type LinkColor = 'none' | 'red' | 'green' | 'blue' | 'yellow' | 'purple'
export const LINK_COLORS: Exclude<LinkColor, 'none'>[] = ['red', 'green', 'blue', 'yellow', 'purple']

export type WindowType = 'scanner' | 'chart' | 'toplist' | 'news' | 'stockinfo' | 'watchlist' | 'clock' | 'setupcheck'
/** alert producers on the unified feed */
export type FeedId = 'system' | 'custom'
export type ToneName = 'ping' | 'chime' | 'buzz' | 'off'

/** Window bounds inside the workspace. x, y, w, h, minW, minH, maxH are FRACTIONS of the
 *  workspace (0..1) so a saved screen keeps its arrangement on any monitor or scaling factor.
 *  `i` is the window id (the server validator matches it against `windows` keys). */
export interface GridItem {
  i: string
  x: number; y: number; w: number; h: number
  z: number
  minW?: number; minH?: number; maxH?: number
  maximized?: boolean
  restore?: { x: number; y: number; w: number; h: number }
}

export interface WindowBase {
  id: string
  type: WindowType
  title?: string
  link: LinkColor
  muted: boolean
  sound: { tone: ToneName; tts: boolean }
}

export interface ScannerConfig extends WindowBase {
  type: 'scanner'
  /** producers to show; empty = every source. (The pre-unified-feed `feed` field is migrated away on load.) */
  sources: FeedId[]
  setups: string[]
  /** With an empty `setups`: true = the user has picked none yet, so the window shows
   *  nothing (a new window starts this way); false/absent = every setup (the old default). */
  noSetups?: boolean
  /** @deprecated retired trigger filter; dropped by the screen migration, never read */
  triggers?: string[]
  direction: 'all' | 'long' | 'short'
  minScore: number
  symbolFilter: string
  columns: string[]
  rowTint: boolean
  maxRows: number
  /** user-dragged column widths in px, by column id */
  colWidths?: Record<string, number>
}

export type ChartTimeframe = '1m' | '5m' | '15m' | '30m' | '1H' | '4H' | '1D' | '1W'
export interface ChartOverlays {
  vwap: boolean; ema9: boolean; ema21: boolean; volume: boolean
  sma50: boolean; sma100: boolean; sma200: boolean; pdHL: boolean; pmHL: boolean
}
export interface ChartConfig extends WindowBase {
  type: 'chart'
  symbol: string | null
  timeframe: ChartTimeframe
  extended: boolean
  overlays: ChartOverlays
}

export type ToplistName =
  | 'rvol' | 'gainers_close' | 'losers_close' | 'gainers_open' | 'losers_open'
  | 'movers_5m' | 'pm_gainers' | 'pm_losers' | 'pm_volume' | 'hod_lod'
/** A server-ranked toplist as the Config panel sees it: which universe filter
 *  scopes it and how many rows a new window of it opens with. Only the six
 *  server-computed lists appear; pm_* come from /api/premarket and hod_lod is
 *  built from the event stream, so neither is filterable this way. */
export interface ToplistDef {
  name: ToplistName
  label: string
  universe: string
  rows: number
  /** How much of the profile this list can apply. 'full' = every condition;
   *  'static' = membership only, because the list has no current bar to
   *  evaluate the dynamic half against; 'none' = the assigned profile is all
   *  dynamic conditions, so nothing applies. */
  scope: 'full' | 'static' | 'none'
}
export interface ToplistsPayload { toplists: ToplistDef[] }

export interface ToplistConfig extends WindowBase {
  type: 'toplist'
  list: ToplistName
  limit: number
  heat: boolean
  colWidths?: Record<string, number>
}

export interface NewsConfig extends WindowBase {
  type: 'news'
  mode: 'market' | 'linked'
  symbol: string | null
  hours: number
  limit: number
  thumbnails: boolean
}

export interface StockInfoConfig extends WindowBase { type: 'stockinfo'; symbol: string | null }
export interface WatchlistConfig extends WindowBase { type: 'watchlist'; watchlistId: string | null; columns: string[]; colWidths?: Record<string, number> }
export interface ClockConfig extends WindowBase { type: 'clock'; showSpy: boolean }
/** Setup check: every setup, what it did on one stock in the last few minutes, and why. */
export interface SetupCheckConfig extends WindowBase { type: 'setupcheck'; symbol: string | null; minutes: number }

export type WindowConfig =
  | ScannerConfig | ChartConfig | ToplistConfig | NewsConfig
  | StockInfoConfig | WatchlistConfig | ClockConfig | SetupCheckConfig

export interface Screen {
  id: string
  name: string
  version: 1
  /** grid resolution the layout was saved in (1 = 12 cols x 28px, 2 = 24 cols x 14px) */
  grid?: number
  createdAt: string
  updatedAt: string
  locked: boolean
  /** Indicators applied when adding charts to this screen. */
  chartOverlays?: ChartOverlays
  layout: GridItem[]
  windows: Record<string, WindowConfig>
}

// ── Backend payloads (/api/v2) ───────────────────────────────────────────────

// ── setups (Config > Setups) ─────────────────────────────────────────────────

export interface TriggerOption { key: string; label: string; direction: string | null }
export interface TriggerParam {
  key: string; label: string; default: number; min: number | null; max: number | null; step: number | null; unit: string; desc: string
  /** When non-empty, the only allowed values: render a dropdown. */
  choices?: number[]
  /** Words for each choice, same order (a unit or a mode rather than a number). */
  choice_labels?: string[]
}
export interface TriggerDef {
  id: string; name: string; category: string; desc: string
  direction: 'long' | 'short' | 'both' | 'neutral'
  option_label: string; options: TriggerOption[]; params: TriggerParam[]
  sessions: string[]; source: 'native' | 'system'; default_options: string[]
}
export interface SetupTrigger { id: string; options: string[]; params: Record<string, number>; repeat_sec?: number }
export interface CustomSetup {
  id: string; name: string; color: string; enabled: boolean
  mode: 'or' | 'and' | 'atleast'; direction: 'all' | 'long' | 'short'; sessions: string[]
  /** For mode 'atleast': how many of the selected alerts must fire. */
  min_triggers?: number
  /** Shared, named list of dynamic conditions, ANDed with `parameters`. */
  parameter_set?: string
  repeat_sec: number; and_window_min: number; size_hint: 'full' | 'three_quarter' | 'half'
  triggers: SetupTrigger[]; notes: string; pending_filters: string[]; source: string
  /** Universe filter this setup is screened against; '' or absent = no filter.
   *  Holds ONLY session-fixed conditions (price, ADV, ATR%, float, market cap). */
  universe_profile?: string
  /** This setup's own dynamic conditions: what the stock is doing right now
   *  (RVOL, distance from VWAP, % change). Private to the setup, ANDed, and
   *  evaluated when it would fire. */
  parameters?: ProfileCondition[]
  createdAt?: string; updatedAt?: string
  summary?: SetupSummary
}
export interface SetupSummary { universe: string; mode: string; direction: string; sessions: string; repeat: string; alerts: string[]; parameters?: string[]; pending_filters: string[] }
/** A built-in setup provided by an engine plugin. The list may be empty. */
export interface SystemSetupInfo { code: string; name: string; default_name: string; direction?: 'long' | 'short' | 'both' | 'neutral' | string }
export interface SetupsPayload {
  system: SystemSetupInfo[]
  custom: CustomSetup[]
  catalog: TriggerDef[]
  stats: { since: string | null; setups: Record<string, Record<string, number>> }
  live: boolean
}
export interface CheckGate { name: string; passed: boolean; value: number | null; reason: string; mandatory?: boolean }
export interface CheckTrigger { key: string; label: string; source: string; fired_last_bar: boolean; value: number | null; note: string; level: number | null; last_eval: string | null; fires_today: number }
export interface CheckResult {
  symbol: string; in_universe: boolean; message?: string
  // system
  setup?: string; fired?: boolean; direction?: string; trigger?: string; tier?: number | null; as_of?: string; price?: number | null
  notes?: string[]; gates?: CheckGate[]; context?: Record<string, number | boolean | null>
  // custom
  triggers?: CheckTrigger[]; bars_1m?: number; candles?: Record<string, number>; vwap?: number | null; rvol?: number | null; session_open?: number | null; enabled?: boolean
}

export interface ClockInfo {
  now_et: string
  session: 'pre' | 'rth' | 'post' | 'closed'
  regime: string
  spy: { price: number | null; vwap: number | null; chg_pct: number | null } | null
  next_change_et: string | null
  /** present when the backend is replaying a past session */
  replay?: { date: string; speed: number; cursor_et: string | null } | null
  /** which provider, and whether bars are still arriving (older scanners omit it) */
  data?: { provider: string | null; last_bar_et: string | null; last_bar_age_s: number | null } | null
}

export interface ToplistRow {
  symbol: string; price: number | null; value: number | null
  chg_pct: number | null; rvol: number | null; volume: number | null
}
export interface ToplistPayload { list: string; as_of: string; rows: ToplistRow[] }

export interface PremarketItem { symbol: string; price: number; change_pct: number; premarket_volume: number; prev_close: number }
export interface PremarketPayload {
  gainers: PremarketItem[]; losers: PremarketItem[]; volume: PremarketItem[]
  symbols_total: number; symbols_active: number; fetched_at?: string; session_date?: string; error?: string
}

export interface HodLodEvent { seq: number; type: 'HOD' | 'LOD'; symbol: string; price: number; ts: string }
export interface EventsPayload { seq: number; events: HodLodEvent[] }

export interface SnapshotRow {
  price: number | null; prior_close: number | null; chg_pct: number | null; rth_chg_pct: number | null
  rvol: number | null; vwap: number | null; dist_vwap_pct: number | null
  hod: number | null; lod: number | null; gap_pct: number | null
}
export interface SnapshotPayload { as_of: string; rows: Record<string, SnapshotRow> }

export interface StockInfo {
  found: boolean; symbol: string; as_of?: string
  price?: number | null; prior_close?: number | null; prior_high?: number | null; prior_low?: number | null
  prior_open?: number | null; chg_pct?: number | null; rth_chg_pct?: number | null; gap_pct?: number | null
  prior_day_chg_pct?: number | null; session_open?: number | null; hod?: number | null; lod?: number | null
  pm_high?: number | null; pm_low?: number | null; pm_vol?: number | null; vwap?: number | null
  dist_vwap_pct?: number | null; rvol?: number | null; adv20?: number | null; mom_15m_pct?: number | null
  day_range_pos?: number | null; ema9?: number | null; ema21?: number | null; dist_ema9_pct?: number | null
  vwap_crosses_30m?: number | null; sma50?: number | null; sma100?: number | null; sma200?: number | null
  ema8_d1?: number | null; atr_d1?: number | null; rrs_d1?: number | null; rrs_sector_d1?: number | null
  chart_quality?: number | null; sector_etf?: string | null
  universe?: { last_price?: number | null; avg_vol_20d?: number | null; avg_dollar_vol_20d?: number | null; atr_pct?: number | null } | null
}

export interface Fundamentals {
  symbol: string; ok: boolean; pending?: boolean; fetched_at?: string; error?: string
  name?: string | null; sector?: string | null; industry?: string | null
  market_cap?: number | null; shares_outstanding?: number | null; float_shares?: number | null
  short_pct_float?: number | null; short_ratio?: number | null; next_earnings?: string | null
  website?: string | null; summary?: string | null
}

export interface NewsItem {
  id: string; headline: string; summary: string; url: string; source: string
  symbols: string[]; image: string | null; created_at: string
  /** full article body (HTML from the wire); rendered only inside a sandboxed iframe */
  content?: string
}
export interface NewsPayload { fetched_at: string; stale?: boolean; error?: string; widened_hours?: number; items: NewsItem[] }

export interface UniverseMeta { last_price?: number; avg_vol_20d?: number; avg_dollar_vol_20d?: number; atr_pct?: number; sector_etf?: string }
export interface UniverseMetaPayload { symbols: string[]; meta: Record<string, UniverseMeta> }

export interface Watchlist { id: string; name: string; symbols: string[]; updatedAt: string }

// ── Config panel (/api/v2/settings) ─────────────────────────────────────────

export interface ParamDef {
  key: string; label: string; default: number; desc: string; setups: string[]
  /** Optional: the gate (a key of the setup's gate stats) this parameter drives. */
  gate?: string
  group: string; type: 'float' | 'int' | 'time' | 'pct'; unit: string
  min: number | null; max: number | null; step: number | null
}
export interface GateStat { pass: number; fail: number }
export interface SettingsStats { since: string; setups: Record<string, { evals: number; fired: number; gates: Record<string, GateStat> }> }
export interface PresetInfo { name: string; saved_at?: string; hash?: string; n_modified?: number }
export interface HistoryEntry { ts: string; source: string; note: string; hash: string; changes: Record<string, [number, number]> }
export interface SettingsPayload {
  schema: ParamDef[]
  values: Record<string, number>
  defaults: Record<string, number>
  modified: Record<string, number>
  hash: string
  presets: PresetInfo[]
  history: HistoryEntry[]
  stats: SettingsStats
  error?: string
}

// ── universe profiles ────────────────────────────────────────────────────────
// A profile is a named AND-list of conditions. A setup points at one, and it is
// checked when the setup would fire, before the alert is emitted.

export interface ConditionOption { key: string; label: string }
export interface ConditionParam {
  key: string; label: string; default: number
  min: number | null; max: number | null; step: number | null; unit: string; desc: string
  /** When non-empty, the only allowed values: render a dropdown. */
  choices?: number[]
}
export interface ConditionDef {
  id: string; name: string; category: string; desc: string
  kind: 'static' | 'dynamic'
  unit: string; ops: string[]; default_op: string; default_value: number
  min: number | null; max: number | null; step: number | null
  option_label: string; default_option: string
  options: ConditionOption[]; params: ConditionParam[]
  availability: 'block' | 'pass'
}
export interface ProfileCondition {
  id: string; op: string; value: number; option: string; params: Record<string, number>
}
export interface UniverseProfile {
  id: string; name: string; desc: string; color: string
  conditions: ProfileCondition[]
  source: string; createdAt?: string; updatedAt?: string
  hash?: string; summary?: string[]
  /** 'universe' (static conditions) or 'parameters' (dynamic). */
  kind?: 'universe' | 'parameters'
}
export interface ProfileStats {
  since: string | null
  setups: Record<string, { checked: number; blocked: number; by_condition: Record<string, number> }>
}
export interface ProfilesPayload {
  profiles: UniverseProfile[]
  assignments: Record<string, string>
  system_keys: string[]
  members: Record<string, number | null>
  catalog: ConditionDef[]
  stats: ProfileStats
  live: boolean
  universe_size: number
  parameter_sets: UniverseProfile[]
}
export interface MembersResult {
  id: string; name: string; universe_size: number; count: number
  symbols: string[]; truncated: boolean
  static_conditions: number; dynamic_conditions: number
}

// ── /api/v2/check/{symbol} ─────────────────────────────────────────────────────
export type CheckStatus = 'sent' | 'blocked' | 'waiting' | 'repeat' | 'suppressed' | 'quiet'
export interface CheckEvent {
  /** Epoch seconds of the bar's START (add 60 for the close, as the Scanner shows). */
  ts: number; source: string; setup: string; direction: string
  outcome: Exclude<CheckStatus, 'quiet'>; trigger: string; reasons: string[]
}
export interface CheckNow { direction: string; ok: boolean | null; reasons: string[]; ts?: number }
export interface CheckRow { id: string; name: string; source: string; status: CheckStatus; events: CheckEvent[]; now: CheckNow[] }
export interface SetupCheckPayload {
  symbol: string; found: boolean; message?: string; price?: number | null; last_bar?: number | null
  minutes?: number; since?: number; generated?: number; recording?: boolean; setups: CheckRow[]
}
