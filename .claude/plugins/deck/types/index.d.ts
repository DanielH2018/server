// The document `probe.py landing --json` prints. `scripts/diagnostics/probe_lib/landing_blockers.py`
// owns the shape; a field is null when its source could not be read.
export type DeckRun = {
  kind: string
  pr: string
  tag: string
  elapsed_s: number
  verdict: string | null
}

// One owed `manual_plane` role. `line` is the SessionStart banner's sentence; the rest are its
// parts. `apply` is "" for a role no printed playbook applies, and `by_hand` says what to do.
// `agents` holds the join-play commands that apply the agent half after `apply` (#5627).
export type DeckManualPlane = {
  role: string
  line: string
  age: string
  apply: string
  by_hand: string
  warning: string
  agents: string[]
  clear: string
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

// A tag of the promoted artifact: the commit its manifest names and its digest.
export type DeckArtifact = {
  commit: string
  digest: string
}

// The promote workflow's hold and tags. Each field is null when its read failed or was not
// made; `last_good` carries empty strings while the tag is absent.
export type DeckPromote = {
  deploy_hold: string | null
  prod: DeckArtifact | null
  last_good: DeckArtifact | null
}

export type DeckSnapshot = {
  host: string
  read_at: number
  hold: { sha: string; planes: string[] } | null
  manual_planes: DeckManualPlane[] | null
  ci: { state: string; sha: string; url: string } | null
  runs: DeckRun[] | null
  last_verdict: { log: string; verdict: string | null } | null
  worktrees: DeckWorktree[] | null
  claims: DeckClaim[] | null
  promote: DeckPromote
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
