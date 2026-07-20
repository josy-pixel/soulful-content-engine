import { useState } from 'react'
import { isSupabaseReady } from './lib/supabase.js'

const NAV = [
  { key: 'dashboard', label: 'Dashboard', icon: '▦' },
  { key: 'clients', label: 'Clients', icon: '☺' },
  { key: 'generate', label: 'Generate', icon: '✦' },
  { key: 'library', label: 'Content Library', icon: '▤' },
  { key: 'approvals', label: 'Approvals', icon: '✓' },
  { key: 'scheduling', label: 'Scheduling', icon: '◷' },
  { key: 'trends', label: 'Trends', icon: '↗' },
  { key: 'reports', label: 'Reports', icon: '▣' },
]

export default function App() {
  const [active, setActive] = useState('dashboard')

  return (
    <div className="min-h-screen flex bg-slate-50 text-slate-800">
      <aside className="w-60 shrink-0 bg-white border-r border-slate-200 flex flex-col">
        <div className="px-5 py-5 flex items-center gap-2">
          <span className="text-brand text-2xl">✦</span>
          <div className="leading-tight">
            <div className="font-bold">Soulful</div>
            <div className="text-xs text-slate-400">Content Engine · v2</div>
          </div>
        </div>
        <nav className="flex-1 px-2">
          {NAV.map((n) => (
            <button
              key={n.key}
              onClick={() => setActive(n.key)}
              className={
                'w-full flex items-center gap-3 px-3 py-2 rounded-lg text-sm mb-1 text-right ' +
                (active === n.key ? 'bg-brand-soft text-brand font-semibold' : 'text-slate-600 hover:bg-slate-100')
              }
            >
              <span className="w-5 text-center">{n.icon}</span>
              {n.label}
            </button>
          ))}
        </nav>
        <div className="px-5 py-4 text-xs text-slate-400">Powered by Claude</div>
      </aside>

      <main className="flex-1 p-8">
        <h1 className="text-2xl font-bold capitalize mb-1">{active}</h1>
        <p className="text-slate-500 mb-6">Phase 1 scaffold — screens are wired next.</p>

        {!isSupabaseReady && (
          <div className="rounded-xl border border-amber-200 bg-amber-50 text-amber-800 px-4 py-3 text-sm">
            Supabase is not connected yet. Add <code>VITE_SUPABASE_URL</code> and{' '}
            <code>VITE_SUPABASE_ANON_KEY</code> to <code>.env</code> to enable data.
          </div>
        )}
      </main>
    </div>
  )
}
