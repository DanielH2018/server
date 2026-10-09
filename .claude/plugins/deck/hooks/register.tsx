// The deck mod: CLAUDE.md's *When to wait* state, live (#3676).
//
// Every fact comes from `probe.py landing --json`, run once a minute. That command computes
// the blocker list itself, so the band, the system-prompt section and the pane cannot disagree
// with it or with each other. The mod reads and gates nothing: when the probe fails, the pane
// says so and the band and section keep the last good snapshot.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { DeckSnapshot } from '../types'

const PANE = 'deck'
const REFRESH_MS = 60_000
const PROBE_TIMEOUT_MS = 45_000
const PROBE = ['uv', 'run', 'python', 'scripts/diagnostics/probe.py', 'landing', '--json']

const snapshot = atom({ plugin: 'deck', key: 'snapshot' } as const, null)
const readError = atom({ plugin: 'deck', key: 'readError' } as const, null)
const isBandHidden = atom({ plugin: 'deck', key: 'isBandHidden' } as const, false)
const isContextOff = atom({ plugin: 'deck', key: 'isContextOff' } as const, false)

// `$.store` keys: the toggles survive the session, the snapshot does not.
const STORE_PANE = 'isPaneOpen'
const STORE_BAND = 'isBandHidden'
const STORE_CONTEXT = 'isContextOff'

const USAGE =
  'Usage: /deck (open or close the pane), /deck band on|off, /deck context on|off, /deck refresh'

/** Formats the blocker list as the system-prompt section the model reads. */
export function blockedSection(blockers: readonly string[]): string {
  return [
    '# Landing is blocked',
    '',
    "CLAUDE.md's *When to wait* applies to landing or deploying a change of your own: name the",
    'blocker below that applies, then stop. Each blocker names its own way out, and taking it',
    '(applying an owed plane, fixing a red master) is not blocked.',
    'The deck mod read these from `probe.py landing`. Run it for the current state and the',
    'apply commands.',
    '',
    ...blockers.map(b => `- ${b}`),
  ].join('\n')
}

// Module variables: a hot reload starts them over, which costs one extra probe run.
let inFlight: Promise<void> | undefined
let root = ''

/**
 * Finds the checkout the session runs in and whether it carries the probe. A session outside
 * the server repo answers false, and the mod then starts no timer there.
 */
async function findProbe($: EngineInterface): Promise<boolean> {
  const top = await $.process.run(['git', 'rev-parse', '--show-toplevel'])
  if (top.exitCode !== 0) {
    return false
  }
  root = top.stdout.trim()
  const probe = await $.process.run(['test', '-f', `${root}/scripts/diagnostics/probe.py`])
  return probe.exitCode === 0
}

/** Runs the probe once and stores its snapshot; a call made while one runs joins it. */
function refresh($: EngineInterface): Promise<void> {
  inFlight ??= readProbe($).finally(() => {
    inFlight = undefined
  })
  return inFlight
}

async function readProbe($: EngineInterface): Promise<void> {
  try {
    const ran = await $.process.run(PROBE, {
      cwd: root || undefined,
      timeoutMs: PROBE_TIMEOUT_MS,
    })
    // Exit 1 means "something blocks"; both 0 and 1 print the document.
    const parsed = JSON.parse(ran.stdout) as DeckSnapshot
    await update($, snapshot, () => parsed)
    await update($, readError, () => null)
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err)
    await update($, readError, () => `probe.py landing failed: ${message.slice(0, 200)}`)
  }
}

async function setToggle(
  $: EngineInterface,
  key: string,
  value: boolean,
): Promise<void> {
  await $.store.set(key, value)
  if (key === STORE_BAND) {
    await update($, isBandHidden, () => value)
  } else if (key === STORE_CONTEXT) {
    await update($, isContextOff, () => value)
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'deck',
      description: 'Landing blockers, runs in flight and other worktrees (band, context: on|off)',
    })
    const bandHidden = (await $.store.get(STORE_BAND)) === true
    const contextOff = (await $.store.get(STORE_CONTEXT)) === true
    await update($, isBandHidden, () => bandHidden)
    await update($, isContextOff, () => contextOff)
    if ((await $.store.get(STORE_PANE)) === true) {
      void $.ui.open({ id: PANE, title: 'Deck' })
    }
    if (await findProbe($)) {
      void refresh($)
      $.clock.every(REFRESH_MS, () => refresh($))
    }

    return next(e)
  })

  on('command.run', { command: 'deck' }, async ($, e) => {
    const [what, value] = e.args.trim().split(/\s+/)
    if (!what) {
      const isOpen = (await $.ui.panes()).some(pane => pane.id === PANE)
      if (isOpen) {
        await $.ui.close({ id: PANE })
        await $.store.set(STORE_PANE, false)
        return { text: 'Deck pane closed.' }
      }
      await $.store.set(STORE_PANE, true)
      await $.ui.open({ id: PANE, title: 'Deck' })
      void refresh($)
      return { text: 'Deck pane opened.' }
    }
    if (what === 'refresh') {
      await refresh($)
      return { text: 'Deck refreshed.' }
    }
    if ((what === 'band' || what === 'context') && (value === 'on' || value === 'off')) {
      const key = what === 'band' ? STORE_BAND : STORE_CONTEXT
      await setToggle($, key, value === 'off')
      return { text: `Deck ${what} ${value}.` }
    }
    return { text: USAGE }
  })

  on('ui.close', async ($, e, next) => {
    if (e.id === PANE && e.origin.kind === 'person') {
      await $.store.set(STORE_PANE, false)
    }
    return next(e)
  })

  on('prompt.compose', async ($, e, next) => {
    const composed = await next(e)
    const snap = await read($, snapshot)
    if (!snap || snap.blockers.length === 0 || (await read($, isContextOff))) {
      return composed
    }
    return {
      sections: [
        ...composed.sections,
        { id: 'deck:landing-blocked', text: blockedSection(snap.blockers), scope: 'session' },
      ],
    }
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const snap = await read($, snapshot)
    if (
      e.props.hasSurvey ||
      !snap ||
      snap.blockers.length === 0 ||
      (await read($, isBandHidden))
    ) {
      return next(e)
    }
    const { Box, Button, Text } = $.ui.resolve(e)
    const more = snap.blockers.length > 1 ? ` (+${snap.blockers.length - 1} more, /deck)` : ''

    return (
      <Box key="deck-band">
        <Text key="blocked" color="red" wrap="truncate-end">
          Landing blocked: {snap.blockers[0]}
          {more}{' '}
        </Text>
        <Button key="hide" label="Hide" onPress={() => setToggle($, STORE_BAND, true)} />
      </Box>
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const snap = await read($, snapshot)
    const error = await read($, readError)
    const lines: { key: string; text: string; color?: string; dim?: boolean }[] = []
    const add = (text: string, color?: string, dim?: boolean) =>
      lines.push({ key: `l${lines.length}`, text, color, dim })

    if (!snap) {
      add(error ?? 'Reading probe.py landing...', undefined, true)
    } else {
      add('Landing blockers', undefined)
      if (snap.blockers.length === 0) {
        add('  none', 'green')
      }
      for (const b of snap.blockers) {
        add(`  ✗ ${b}`, 'red')
      }
      const hold = snap.hold === null ? 'unknown' : snap.hold.sha.slice(0, 8) || 'none'
      const ci = snap.ci === null ? 'unknown' : `${snap.ci.state} ${snap.ci.sha}`
      const owed =
        snap.manual_planes === null ? 'unknown here' : String(snap.manual_planes.length)
      add(`hold: ${hold}   master CI: ${ci}   manual planes: ${owed}`, undefined, true)
      for (const plane of snap.manual_planes ?? []) {
        add(`  owed: ${plane.line}`, 'yellow')
      }
      add('')
      add('In flight')
      if (snap.runs === null) {
        add('  unknown', undefined, true)
      } else if (snap.runs.length === 0) {
        add('  nothing', undefined, true)
      }
      for (const r of snap.runs ?? []) {
        const what = r.pr ? `PR ${r.pr}` : r.tag.slice(0, 40)
        add(`  ${r.kind} ${what} ${r.elapsed_s}s ${r.verdict ?? ''}`.trimEnd())
      }
      if (snap.last_verdict) {
        add(`Last landing: ${snap.last_verdict.verdict ?? 'no VERDICT line'}`, undefined, true)
      }
      add('')
      add('Worktrees')
      for (const t of snap.worktrees ?? []) {
        const claims = t.claims.map(n => `#${n}`).join(' ')
        add(`  ${t.branch || t.path} ${claims}`.trimEnd())
      }
      const local = new Set((snap.worktrees ?? []).map(t => t.branch))
      const elsewhere = (snap.claims ?? []).filter(c => !local.has(c.worktree))
      if (elsewhere.length > 0) {
        add('Claims held by worktrees not on this host')
        for (const c of elsewhere) {
          add(`  #${c.number} ${c.worktree}${c.live ? '' : ' (stale here)'}`, undefined, !c.live)
        }
      }
      for (const err of snap.errors) {
        add(err, 'yellow', true)
      }
      if (error) {
        add(error, 'yellow', true)
      }
      add(`read ${new Date(snap.read_at * 1000).toISOString()} on ${snap.host}`, undefined, true)
    }

    return (
      <Box key="deck" flexDirection="column">
        {lines.map(l => (
          <Text key={l.key} color={l.color} dimColor={l.dim} wrap="truncate-end">
            {l.text || ' '}
          </Text>
        ))}
      </Box>
    )
  })
}
