import { useEffect, useMemo, useRef, useState } from 'react'
import {
  createChart, ColorType, CrosshairMode, LineStyle,
  type IChartApi, type ISeriesApi, type IPriceLine, type SeriesMarker, type UTCTimestamp,
} from 'lightweight-charts'
import type { Bar, ChartConfig, ChartOverlays, ChartTimeframe } from '../../types'
import { api } from '../../lib/api'
import { toSecET, isExtended, etDate } from '../../lib/time'
import { calcEMA, calcSMA, pmHighLow, priorDayHL, type Pt } from '../../lib/chartMath'
import { readChartPalette, THEME_EVENT, type ChartPalette } from '../../lib/theme'
import { useLinkedSymbol, useLinkedAlert, linkSymbol } from '../../stores/linkStore'
import { useSetups } from '../../stores/setupsStore'
import { useScreens } from '../../stores/screensStore'
import { CompanyLogo, Empty, Field, SymbolInput, Toggle } from '../../components/primitives'
import { useFundamentals } from '../../lib/useFundamentals'
import { usePoll } from '../../lib/usePoll'

const TF_API: Record<ChartTimeframe, string> = {
  '1m': '1min', '5m': '5min', '15m': '15min', '30m': '30min', '1H': '1hour', '4H': '4hour', '1D': '1day', '1W': '1week',
}
const TFS: ChartTimeframe[] = ['1m', '5m', '15m', '30m', '1H', '4H', '1D', '1W']
/** Frames drawn per session: VWAP, extended-hours toggle and PD / PM levels apply. */
const SESSION_TFS: ChartTimeframe[] = ['1m', '5m', '15m', '30m', '1H']
// Each study keeps one color everywhere (line, axis label, toolbar chip): --ind-* tokens.
const OVERLAY_CHIPS: { key: keyof ChartOverlays; label: string; session?: boolean; color: keyof ChartPalette; token: string }[] = [
  { key: 'vwap', label: 'VWAP', session: true, color: 'vwap', token: '--ind-vwap' },
  { key: 'ema9', label: 'EMA9', color: 'ema9', token: '--ind-ema9' },
  { key: 'ema21', label: 'EMA21', color: 'ema21', token: '--ind-ema21' },
  { key: 'volume', label: 'Vol', color: 'dim', token: '--text-dim' },
  { key: 'sma50', label: 'SMA50', color: 'sma50', token: '--ind-sma50' },
  { key: 'sma100', label: 'SMA100', color: 'sma100', token: '--ind-sma100' },
  { key: 'sma200', label: 'SMA200', color: 'sma200', token: '--ind-sma200' },
  { key: 'pdHL', label: 'PD', session: true, color: 'pd', token: '--ind-pd' },
  { key: 'pmHL', label: 'PM', session: true, color: 'pm', token: '--ind-pm' },
]

// daily bars cached per symbol for SMA levels + prior-day H/L (60 s)
const dailyCache = new Map<string, { t: number; bars: Bar[] }>()
async function getDaily(symbol: string): Promise<Bar[]> {
  const hit = dailyCache.get(symbol)
  if (hit && Date.now() - hit.t < 60_000) return hit.bars
  const bars = await api.bars(symbol, '1day')
  dailyCache.set(symbol, { t: Date.now(), bars })
  return bars
}

/** Session VWAP that restarts every trading day (multi-day intraday frames). */
function calcSessionVWAP(bars: Bar[]): Pt[] {
  let tpv = 0, vol = 0, day = ''
  return bars.map(b => {
    const d = etDate(b.t)
    if (d !== day) { day = d; tpv = 0; vol = 0 }
    tpv += ((b.h + b.l + b.c) / 3) * b.v
    vol += b.v
    return { time: toSecET(b.t), value: vol > 0 ? tpv / vol : b.c }
  })
}

type Line = ISeriesApi<'Line'>
interface Refs {
  chart: IChartApi
  candles: ISeriesApi<'Candlestick'>
  volume: ISeriesApi<'Histogram'>
  lines: Partial<Record<'vwap' | 'ema9' | 'ema21' | 'sma50' | 'sma100' | 'sma200', Line>>
  priceLines: IPriceLine[]
}

const ts = (t: string) => toSecET(t) as UTCTimestamp

export function ChartWindow({ win }: { win: ChartConfig }) {
  const symbol = useLinkedSymbol(win, win.symbol)
  const selAlert = useLinkedAlert(win, symbol)
  const hostRef = useRef<HTMLDivElement>(null)
  const refs = useRef<Refs | null>(null)
  const fitKeyRef = useRef<string>('')
  const hadDataRef = useRef(false)
  const [dailyFor, setDailyFor] = useState<{ sym: string; bars: Bar[] } | null>(null)
  const daily = useMemo<Bar[]>(() => (symbol && dailyFor?.sym === symbol ? dailyFor.bars : []), [dailyFor, symbol])
  const fund = useFundamentals(symbol)
  const tf: ChartTimeframe = TFS.includes(win.timeframe) ? win.timeframe : '5m'
  const session = SESSION_TFS.includes(tf)          // VWAP / EXT / PD / PM make sense
  const pollMs = session ? 30_000 : 120_000

  // Every response carries the symbol and frame it was fetched for. usePoll keeps
  // its last result when the symbol changes, so without the key a slow fetch left
  // the old stock's candles under the new stock's name. Only a matching response
  // is drawn; until it arrives the chart is empty and says it is loading.
  const dataKey = `${symbol}|${tf}`
  const { data: resp } = usePoll(
    async (): Promise<{ key: string; bars: Bar[]; error: string | null }> => {
      const key = `${symbol}|${tf}`
      if (!symbol) return { key, bars: [], error: null }
      try { return { key, bars: await api.bars(symbol, TF_API[tf]), error: null } }
      catch (e) { return { key, bars: [], error: e instanceof Error ? e.message : String(e) } }
    },
    pollMs, !!symbol, [symbol, tf],
  )
  const current = resp?.key === dataKey ? resp : null
  const bars = current?.bars ?? null
  const error = current?.error ?? null

  useEffect(() => {
    if (!symbol) return
    let alive = true
    const sym = symbol
    getDaily(sym).then(b => { if (alive) setDailyFor({ sym, bars: b }) }).catch(() => { if (alive) setDailyFor({ sym, bars: [] }) })
    return () => { alive = false }
  }, [symbol])

  // create the chart ONCE per mount; everything else is applyOptions/setData
  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    const pal = readChartPalette()
    const chart = createChart(host, {
      layout: { background: { type: ColorType.Solid, color: pal.bg }, textColor: pal.text, fontSize: 10, fontFamily: 'Roboto Mono, ui-monospace, monospace' },
      grid: { vertLines: { color: pal.grid }, horzLines: { color: pal.grid } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: pal.border, scaleMargins: { top: 0.06, bottom: 0.22 } },
      timeScale: { borderColor: pal.border, timeVisible: true, secondsVisible: false, rightOffset: 3 },
      handleScroll: true, handleScale: true,
    })
    const candles = chart.addCandlestickSeries({
      upColor: pal.up, downColor: pal.down, borderUpColor: pal.up, borderDownColor: pal.down, wickUpColor: pal.up, wickDownColor: pal.down,
    })
    const volume = chart.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: 'vol', lastValueVisible: false, priceLineVisible: false })
    chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } })
    refs.current = { chart, candles, volume, lines: {}, priceLines: [] }

    const ro = new ResizeObserver(entries => {
      const r = entries[0]?.contentRect
      if (r && r.width > 0 && r.height > 0) chart.applyOptions({ width: Math.floor(r.width), height: Math.floor(r.height) })
    })
    ro.observe(host)

    const onTheme = () => {
      const p = readChartPalette()
      chart.applyOptions({ layout: { background: { type: ColorType.Solid, color: p.bg }, textColor: p.text }, grid: { vertLines: { color: p.grid }, horzLines: { color: p.grid } }, rightPriceScale: { borderColor: p.border }, timeScale: { borderColor: p.border } })
      candles.applyOptions({ upColor: p.up, downColor: p.down, borderUpColor: p.up, borderDownColor: p.down, wickUpColor: p.up, wickDownColor: p.down })
      const L = refs.current?.lines
      L?.vwap?.applyOptions({ color: p.vwap }); L?.ema9?.applyOptions({ color: p.ema9 }); L?.ema21?.applyOptions({ color: p.ema21 })
      L?.sma50?.applyOptions({ color: p.sma50 }); L?.sma100?.applyOptions({ color: p.sma100 }); L?.sma200?.applyOptions({ color: p.sma200 })
    }
    window.addEventListener(THEME_EVENT, onTheme)
    return () => { window.removeEventListener(THEME_EVENT, onTheme); ro.disconnect(); chart.remove(); refs.current = null }
  }, [])

  // data + overlays
  const visibleBars = useMemo(() => {
    if (!bars) return []
    const sorted = bars.slice().sort((a, b) => toSecET(a.t) - toSecET(b.t))
    return session && !win.extended ? sorted.filter(b => !isExtended(b.t)) : sorted
  }, [bars, session, win.extended])

  useEffect(() => {
    const r = refs.current
    if (!r) return
    const pal = readChartPalette()
    const o = win.overlays

    r.chart.timeScale().applyOptions({ timeVisible: tf !== '1D' && tf !== '1W' })
    r.candles.setData(visibleBars.map(b => ({ time: ts(b.t), open: b.o, high: b.h, low: b.l, close: b.c })))
    r.volume.setData(o.volume ? visibleBars.map(b => ({ time: ts(b.t), value: b.v, color: b.c >= b.o ? pal.upSoft : pal.downSoft })) : [])
    r.volume.applyOptions({ visible: o.volume })

    const line = (key: keyof Refs['lines'], color: string, width: 1 | 2 = 1, style = LineStyle.Solid, title = ''): Line => {
      let s = r.lines[key]
      if (!s) {
        s = r.chart.addLineSeries({
          color, lineWidth: width, lineStyle: style, title,
          lastValueVisible: !!title, priceLineVisible: false, crosshairMarkerVisible: false,
        })
        r.lines[key] = s
      }
      return s
    }
    const setLine = (key: keyof Refs['lines'], on: boolean, color: string, data: () => Pt[], width: 1 | 2 = 1, style = LineStyle.Solid, title = '') => {
      const s = line(key, color, width, style, title)
      s.applyOptions({ visible: on, color })
      s.setData(on ? data().map(p => ({ time: p.time as UTCTimestamp, value: p.value })) : [])
    }
    const rth = session ? visibleBars.filter(b => !isExtended(b.t)) : visibleBars
    // VWAP is the level the engine gates on: solid, 2px, labelled on the axis, restarts each day.
    setLine('vwap', o.vwap && session, pal.vwap, () => calcSessionVWAP(rth), 2, LineStyle.Solid, 'VWAP')
    setLine('ema9', o.ema9, pal.ema9, () => calcEMA(visibleBars, 9, toSecET), 1, LineStyle.Solid, '9')
    setLine('ema21', o.ema21, pal.ema21, () => calcEMA(visibleBars, 21, toSecET), 1, LineStyle.Solid, '21')
    // SMAs are period-based on the chart's own bars (SMA200 on 5m = 200 five-minute
    // bars, on 1D = 200 sessions), so intraday frames carry several days of history.
    setLine('sma50', o.sma50, pal.sma50, () => calcSMA(visibleBars, 50, toSecET), 1, LineStyle.Solid, '50')
    setLine('sma100', o.sma100, pal.sma100, () => calcSMA(visibleBars, 100, toSecET), 1, LineStyle.Solid, '100')
    setLine('sma200', o.sma200, pal.sma200, () => calcSMA(visibleBars, 200, toSecET), 2, LineStyle.Solid, '200')

    for (const pl of r.priceLines) r.candles.removePriceLine(pl)
    r.priceLines = []
    const addLevel = (price: number | null | undefined, title: string, color: string, style = LineStyle.Dotted) => {
      if (price == null || !isFinite(price)) return
      r.priceLines.push(r.candles.createPriceLine({ price, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title }))
    }
    if (session) {
      const today = visibleBars.length ? etDate(visibleBars[visibleBars.length - 1].t) : etDate(new Date())
      if (o.pdHL) {
        const pd = priorDayHL(daily, today)
        addLevel(pd?.high, 'PDH', pal.pd); addLevel(pd?.low, 'PDL', pal.pd); addLevel(pd?.close, 'PDC', pal.pd, LineStyle.SparseDotted)
      }
      if (o.pmHL && bars) {
        const pm = pmHighLow(bars.filter(b => etDate(b.t) === today))
        addLevel(pm?.high, 'PMH', pal.pm); addLevel(pm?.low, 'PML', pal.pm)
      }
    }
    // The alert selected in a linked Scanner window: an arrow on the bar that
    // contains it (the 1-minute alert bar, or the 5m / daily bar holding it).
    const markers: SeriesMarker<UTCTimestamp>[] = []
    if (selAlert && visibleBars.length) {
      const at = toSecET(selAlert.timestamp)
      let bar: number | null = null
      for (const b of visibleBars) { const t = toSecET(b.t); if (t <= at) bar = t; else break }
      if (bar != null) {
        // Three cases: a neutral alert (volume spike, RVOL cross) has no side,
        // so it gets a dot, never a down arrow.
        const long = selAlert.direction === 'long'
        const short = selAlert.direction === 'short'
        const name = selAlert.setup_label || (selAlert.setup ? useSetups.getState().label(selAlert.setup) : '') || 'alert'
        markers.push({ time: bar as UTCTimestamp, position: long ? 'belowBar' : 'aboveBar', shape: long ? 'arrowUp' : short ? 'arrowDown' : 'circle', color: pal.accent, text: name })
      }
    }
    r.candles.setMarkers(markers)

    // Fit only when a fresh data set arrives (new symbol/timeframe, or the first
    // bars after an empty state). Refreshes and overlay toggles must keep the
    // user's zoom and scroll position. Session frames open on the latest
    // session (the earlier days are there for the moving averages; scroll left).
    const fitKey = `${symbol}|${tf}|${win.extended}`
    if (visibleBars.length && (fitKeyRef.current !== fitKey || !hadDataRef.current)) {
      const n = visibleBars.length
      const lastDay = etDate(visibleBars[n - 1].t)
      let firstOfDay = n - 1
      while (firstOfDay > 0 && etDate(visibleBars[firstOfDay - 1].t) === lastDay) firstOfDay--
      if (session && firstOfDay > 0) r.chart.timeScale().setVisibleLogicalRange({ from: firstOfDay - 2, to: n + 3 })
      else r.chart.timeScale().fitContent()
      fitKeyRef.current = fitKey
    }
    hadDataRef.current = visibleBars.length > 0
  }, [visibleBars, bars, daily, win.overlays, session, symbol, tf, win.extended, selAlert])

  const set = (patch: Partial<ChartConfig>) => useScreens.getState().updateWindow(win.id, patch)
  const toggleOverlay = (k: keyof ChartOverlays) => set({ overlays: { ...win.overlays, [k]: !win.overlays[k] } })
  const smaNote = (period: number) => (visibleBars.length && visibleBars.length < period ? ` (needs ${period} ${tf} bars, have ${visibleBars.length})` : '')

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div className="wf-toolbar wf-nodrag">
        {symbol && <CompanyLogo symbol={symbol} website={fund?.website} />}
        <SymbolInput value={symbol} small onCommit={s => linkSymbol(win, s)} />
        <div className="chart-tf">
          {TFS.map(t => <button key={t} className={tf === t ? 'on' : ''} onClick={() => set({ timeframe: t })}>{t}</button>)}
        </div>
        <button className={`chip${win.extended ? ' on' : ''}`} title="Extended hours" onClick={() => set({ extended: !win.extended })} disabled={!session}>EXT</button>
        <span className="flex-spacer" />
        {OVERLAY_CHIPS.map(c => (
          <button key={c.key} className={`chip ind${win.overlays[c.key] ? ' on' : ''}`} style={{ ['--ind' as string]: `var(${c.token})` }}
            onClick={() => toggleOverlay(c.key)} disabled={!!c.session && !session}
            title={c.key === 'pdHL' ? 'Prior day high / low / close' : c.key === 'pmHL' ? 'Premarket high / low' : c.key.startsWith('sma') ? `SMA ${c.label.slice(3)} of ${tf} bars${smaNote(Number(c.label.slice(3)))}` : c.label}>
            {c.label}
          </button>
        ))}
      </div>
      <div style={{ flex: 1, position: 'relative', minHeight: 0 }}>
        <div ref={hostRef} className="chart-host" />
        {!symbol && <div style={{ position: 'absolute', inset: 0 }}><Empty title="No symbol">Pick a link color or type a symbol.</Empty></div>}
        {symbol && !current && <div className="chart-loading pulse">Loading {symbol} {tf}…</div>}
        {symbol && error && <div style={{ position: 'absolute', top: 6, left: 8 }} className="down">{error}</div>}
        {symbol && bars && bars.length === 0 && !error && <div style={{ position: 'absolute', inset: 0 }}><Empty title={`No ${tf} bars for ${symbol}`}>{session ? 'Before 04:00 ET there is nothing to draw yet.' : 'Symbol may not have history for this frame.'}</Empty></div>}
      </div>
    </div>
  )
}

export function ChartSettings({ win, onChange }: { win: ChartConfig; onChange(p: Partial<ChartConfig>): void }) {
  const o = win.overlays
  const saveChartDefaults = useScreens(s => s.saveChartDefaults)
  const t = (k: keyof ChartOverlays, label: string) => <Toggle checked={o[k]} onChange={v => onChange({ overlays: { ...o, [k]: v } })} label={label} />
  return (
    <>
      <div className="grid2">
        <Field label="Symbol (when unlinked)"><SymbolInput value={win.symbol} onCommit={s => onChange({ symbol: s })} /></Field>
        <Field label="Timeframe">
          <div className="chart-tf">{TFS.map(tf => <button key={tf} className={win.timeframe === tf ? 'on' : ''} onClick={() => onChange({ timeframe: tf })}>{tf}</button>)}</div>
        </Field>
      </div>
      <Field label="Intraday overlays">{t('vwap', 'Session VWAP')}{t('ema9', 'EMA 9')}{t('ema21', 'EMA 21')}{t('volume', 'Volume')}</Field>
      <Field label="Moving averages (on this timeframe's bars)">{t('sma50', 'SMA 50')}{t('sma100', 'SMA 100')}{t('sma200', 'SMA 200')}</Field>
      <Field label="Levels">{t('pdHL', 'Prior-day high / low / close (PDH / PDL / PDC)')}{t('pmHL', 'Premarket high / low (PMH / PML)')}</Field>
      <Field label="New charts on this screen"><button className="btn sm" onClick={() => saveChartDefaults(o)}>Save these indicators as defaults</button></Field>
      <Toggle checked={win.extended} onChange={extended => onChange({ extended })} label="Show extended hours (04:00-20:00 ET)" />
    </>
  )
}
