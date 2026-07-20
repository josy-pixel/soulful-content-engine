import { createClient } from '@supabase/supabase-js'

const url = import.meta.env.VITE_SUPABASE_URL
const anon = import.meta.env.VITE_SUPABASE_ANON_KEY

// Guarded so the app still renders before Supabase is wired.
export const supabase = url && anon ? createClient(url, anon) : null
export const isSupabaseReady = Boolean(supabase)
