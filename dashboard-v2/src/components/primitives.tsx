import { useMemo, useRef, useState, type ReactNode } from 'react'
import { useUniverse } from '../stores/universeStore'
import { SYMBOL_ACTIONS } from '../plugins'
import { logoUrls } from '../lib/useFundamentals'

export function Toggle({ checked, onChange, label }: { checked: boolean; onChange(v: boolean): void; label: ReactNode }) {
  return (
    <label className="row" style={{ cursor: 'pointer', gap: 6, fontSize: 12 }}>
      <input type="checkbox" checked={checked} onChange={e => onChange(e.target.checked)} style={{ accentColor: 'var(--accent)' }} />
      <span>{label}</span>
    </label>
  )
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return <div className="field"><label>{label}</label>{children}</div>
}

export function Select<T extends string>({ value, onChange, options, small }: {
  value: T; onChange(v: T): void; options: { value: T; label: string }[]; small?: boolean
}) {
  return (
    <select className={`input${small ? ' sm' : ''}`} value={value} onChange={e => onChange(e.target.value as T)}>
      {options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  )
}

/** Multi-select as toggle chips. Empty selection = "all". */
export function ChipMultiSelect({ value, onChange, options, allLabel = 'All' }: {
  value: string[]; onChange(v: string[]): void; options: { value: string; label: string }[]; allLabel?: string
}) {
  const toggle = (v: string) => onChange(value.includes(v) ? value.filter(x => x !== v) : [...value, v])
  // An empty selection means "no filter", so the first chip is lit and reads
  // All when nothing is picked, and turns into the way back once something is.
  // It deliberately does NOT select every option: an explicit full list would
  // freeze the filter, so a setup added later would silently stop appearing.
  const none = value.length === 0
  return (
    <div className="row wrap" style={{ gap: 4 }}>
      <button className={`chip${none ? ' on' : ''}`} disabled={none}
        title={none ? 'No filter: all of these are shown, including any added later'
          : `Deselect all ${value.length} and go back to showing everything`}
        onClick={() => onChange([])}>
        {none ? allLabel : `Clear (${value.length})`}
      </button>
      {options.map(o => (
        <button key={o.value} className={`chip${value.includes(o.value) ? ' on' : ''}`} onClick={() => toggle(o.value)}>{o.label}</button>
      ))}
    </div>
  )
}

/** Column picker: ordered toggles. */
export function ColumnPicker({ value, onChange, all }: {
  value: string[]; onChange(v: string[]): void; all: { id: string; label: string }[]
}) {
  const toggle = (id: string) => {
    if (value.includes(id)) onChange(value.filter(x => x !== id))
    else onChange(all.filter(c => value.includes(c.id) || c.id === id).map(c => c.id))
  }
  return (
    <div className="row wrap" style={{ gap: 4 }}>
      {all.map(c => (
        <button key={c.id} className={`chip${value.includes(c.id) ? ' on' : ''}`} onClick={() => toggle(c.id)}>{c.label}</button>
      ))}
    </div>
  )
}

/** Symbol input with universe autocomplete. Enter commits (uppercased). The
 *  text is selected on focus and after Enter, so the next symbol can be typed
 *  straight over the last one. */
export function SymbolInput({ value, onCommit, placeholder = 'Symbol', small, autoFocus, clearOnCommit }: {
  value?: string | null; onCommit(sym: string): void; placeholder?: string; small?: boolean; autoFocus?: boolean; clearOnCommit?: boolean
}) {
  const symbols = useUniverse(s => s.symbols)
  const [text, setText] = useState(value ?? '')
  const [open, setOpen] = useState(false)
  const [hi, setHi] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)
  // Deferred a frame: selecting inside onFocus is undone by the click's own
  // mouseup, and after Enter the committed text has not rendered yet.
  const selectAll = () => requestAnimationFrame(() => inputRef.current?.select())
  // derived state: when the bound value changes from outside, mirror it into the field
  const [prevValue, setPrevValue] = useState(value)
  if (value !== prevValue) { setPrevValue(value); setText(value ?? '') }
  const matches = useMemo(() => {
    const q = text.trim().toUpperCase()
    if (!q) return []
    const starts = symbols.filter(s => s.startsWith(q))
    const contains = symbols.filter(s => !s.startsWith(q) && s.includes(q))
    return [...starts, ...contains].slice(0, 8)
  }, [text, symbols])
  const commit = (s: string) => {
    const v = s.trim().toUpperCase()
    if (!v) return
    onCommit(v)
    setOpen(false)
    setText(clearOnCommit ? '' : v)
    selectAll()
  }
  return (
    <div style={{ position: 'relative' }} className="wf-nodrag">
      <input
        ref={inputRef}
        className={`input mono${small ? ' sm' : ''}`}
        style={{ width: small ? 76 : 96, textTransform: 'uppercase' }}
        value={text}
        placeholder={placeholder}
        autoFocus={autoFocus}
        onChange={e => { setText(e.target.value); setOpen(true); setHi(0) }}
        onFocus={() => { setOpen(true); selectAll() }}
        onBlur={() => setTimeout(() => setOpen(false), 120)}
        onKeyDown={e => {
          if (e.key === 'Enter') commit(matches[hi] ?? text)
          else if (e.key === 'ArrowDown') { e.preventDefault(); setHi(h => Math.min(h + 1, matches.length - 1)) }
          else if (e.key === 'ArrowUp') { e.preventDefault(); setHi(h => Math.max(h - 1, 0)) }
          else if (e.key === 'Escape') setOpen(false)
        }}
      />
      {open && matches.length > 0 && (
        <div className="menu" style={{ minWidth: 110, left: 0 }}>
          {matches.map((m, i) => (
            <button key={m} className={`menu-item mono${i === hi ? ' on' : ''}`} onMouseDown={e => { e.preventDefault(); commit(m) }}>{m}</button>
          ))}
        </div>
      )}
    </div>
  )
}

/** The per-symbol actions contributed by UI plugins (src/plugins.ts). Renders
 *  nothing when no plugin is installed. */
export function SymbolActions({ symbol, small = true }: { symbol: string; small?: boolean }) {
  if (!SYMBOL_ACTIONS.length) return null
  return <>{SYMBOL_ACTIONS.map((Action, i) => <Action key={i} symbol={symbol} small={small} />)}</>
}

/** Company logo from the fundamentals website; falls back through the candidate
 *  URLs, then to a lettered tile. */
export function CompanyLogo({ symbol, website, large }: { symbol: string; website: string | null | undefined; large?: boolean }) {
  const urls = useMemo(() => logoUrls(website), [website])
  const [idx, setIdx] = useState(0)
  const [prevKey, setPrevKey] = useState(urls.join('|'))
  if (urls.join('|') !== prevKey) { setPrevKey(urls.join('|')); setIdx(0) }
  const cls = `logo${large ? ' lg' : ''}`
  if (idx >= urls.length) return <span className={`${cls} logo-fallback`} title={symbol}>{symbol.slice(0, large ? 4 : 2)}</span>
  return <img className={cls} src={urls[idx]} alt="" title={symbol} loading="lazy" onError={() => setIdx(i => i + 1)} />
}

export function Empty({ title, children }: { title: ReactNode; children?: ReactNode }) {
  return <div className="wf-empty"><b>{title}</b>{children && <span>{children}</span>}</div>
}
