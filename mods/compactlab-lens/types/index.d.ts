export type LensProfileName = 'cost' | 'balanced' | 'time'

/** Per-day impact of one profile against one reference, as `compactlab simulate` writes it to lens.json. */
export type LensImpact = {
  usd_per_day: number
  usd_per_active_day: number | null
  min_per_day: number
  wait_min_per_day: number
  turn_gain_min_per_day: number
  compactions_per_day: number
  ref_compactions_per_day: number
}

export type LensProfile = {
  threshold: number
  action: string
  flags: string[]
  vs_default: LensImpact | null
  vs_current: LensImpact | null
}

export type LensGroup = {
  harness: string
  model: string
  role: string
  default_threshold: number
  current_threshold: number | null
  effective_max: number | null
  max_window: number | null
  days: number
  active_days: number | null
  no_action_needed: boolean
  profiles: Record<LensProfileName, LensProfile>
}

/** When the service (or `compactlab recalibrate`) last calibrated, and its interval in seconds (null: manual). */
export type LensCalibration = { at: number; every_s: number | null }

/** What the lens file said for this session's model: a group, or why there is none. */
export type LensFacts =
  | { status: 'ok'; path: string; group: LensGroup; isDemo: boolean; calibration: LensCalibration | null }
  | { status: 'missing' | 'no-group' | 'unreadable'; path: string }

/** What the engine reports for this session, refreshed after each turn. */
export type LensLive = {
  model: string
  tokens: number
  window: number
  /** The compaction window in force (the breakdown's rawMaxTokens), or null when unknown. */
  compactWindow: number | null
  /** How that window was settled (`auto`, `settings`, `env`, ...). */
  source: string | null
  /** Context tokens after each turn of this session, oldest first. */
  curve: number[]
  compactions: number
}

declare module 'claude-code' {
  interface PluginState {
    'compactlab-lens': {
      facts: Shaped<LensFacts | null>
      live: Shaped<LensLive | null>
      profile: LensProfileName | null
      isOpen: boolean
    }
  }
}
