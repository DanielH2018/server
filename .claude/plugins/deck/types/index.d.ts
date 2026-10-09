// The document `probe.py landing --json` prints. `scripts/diagnostics/probe_lib/landing.py`
// owns the shape; a field is null when its source could not be read.
export type DeckRun = {
  kind: string
  pr: string
  tag: string
  elapsed_s: number
  verdict: string | null
}

export type DeckWorktree = { path: string; branch: string; claims: number[] }

// One row of `findings.py claims --json`.
export type DeckClaim = {
  number: number
  worktree: string
  live: boolean
  reason: string
  age_days: number
}

export type DeckSnapshot = {
  host: string
  read_at: number
  hold: { sha: string; planes: string[] } | null
  manual_planes: string[] | null
  ci: { state: string; sha: string; url: string } | null
  runs: DeckRun[] | null
  last_verdict: { log: string; verdict: string | null } | null
  worktrees: DeckWorktree[] | null
  claims: DeckClaim[] | null
  blockers: string[]
  errors: string[]
}

declare module 'claude-code' {
  interface PluginState {
    deck: {
      snapshot: DeckSnapshot | null
      readError: string | null
      isBandHidden: boolean
      isContextOff: boolean
    }
  }
}
