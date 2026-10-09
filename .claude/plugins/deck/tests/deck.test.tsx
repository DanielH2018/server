import { expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

import type { DeckSnapshot } from '../types'

const SURFACES = ['terminal', 'desktop'] as const

const CLEAR: DeckSnapshot = {
  host: 'daniel-server',
  read_at: 1_791_500_000,
  hold: { sha: '', planes: [] },
  manual_planes: null,
  ci: { state: 'green', sha: 'abcdef12', url: 'https://ci/1' },
  runs: [{ kind: 'land', pr: '3700', tag: '', elapsed_s: 42, verdict: null }],
  last_verdict: null,
  worktrees: [
    { path: '/repo/.claude/worktrees/fanout-1', branch: 'worktree-fanout-1', claims: [3676] },
  ],
  claims: [
    { number: 3676, worktree: 'worktree-fanout-1', live: true, reason: '', age_days: 0 },
    { number: 3500, worktree: 'worktree-on-the-box', live: true, reason: '', age_days: 1 },
  ],
  blockers: [],
  errors: [],
}

const HELD: DeckSnapshot = {
  ...CLEAR,
  hold: { sha: 'deadbeefcafe', planes: ['setup:k3s'] },
  blockers: ['hold_sha is set (deadbeef), waiting on setup:k3s'],
}

const BAND = {
  component: 'AbovePrompt',
  props: {
    hasSurvey: false,
    isWorking: false,
    maxRows: 10,
    bodyColumns: 120,
    scroll: { offset: 0, bodyRows: 10 },
    view: {},
  },
} as const

const PANE = {
  component: 'Pane',
  requestId: 'deck',
  props: {
    title: 'Deck',
    isFocused: false,
    bodyColumns: 60,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 30 },
    view: {},
  },
} as const

const COMPOSE = {
  model: 'claude-opus-5-5',
  promptModel: 'claude-opus-5-5',
  surfaces: ['terminal'],
  tools: [],
  outputStyle: null,
  traits: [],
} as const

/** Runs `/deck <args>` as the person typing it at the prompt would. */
function deck($: Engine, args: string) {
  return $.command.run({
    command: 'deck',
    args,
    origin: { kind: 'composer' },
    presentation: { isFullscreen: true, columns: 200 },
  })
}

const START = { cwd: '/repo', surface: 'terminal', isInteractive: true } as const

/**
 * Stands in for the engine beneath the mod: the probe answers whatever `answer` holds when it
 * runs, and the system prompt starts as one section of its own.
 */
function world(on: On, answer: { snap: DeckSnapshot | 'fail' }) {
  const clock = mock.clock(on)
  mock.store(on)
  const ran = (exitCode: number, stdout: string) => ({
    value: { exitCode, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
  })
  on('process.run', (_$, e) => {
    if (e.argv[0] === 'git') {
      return ran(0, '/repo\n')
    }
    if (e.argv[0] === 'test') {
      return ran(0, '')
    }
    if (answer.snap === 'fail') {
      return ran(2, '')
    }
    return ran(answer.snap.blockers.length > 0 ? 1 : 0, JSON.stringify(answer.snap))
  })
  const panes = new Set<string>()
  const pane = (id: string) => ({ id, title: 'Deck', isShown: true, isFocused: false, isPlaced: true })
  on('ui.open', (_$, e) => {
    panes.add(e.id)
    return { value: { isPlaced: true } as const }
  })
  on('ui.close', (_$, e) => {
    panes.delete(e.id)
    return { value: undefined }
  })
  on('ui.panes', () => ({ value: [...panes].map(pane) }))
  on('command.register', (_$, e) => ({ value: { command: e.name } }))
  on('ui.render', () => ({ type: 'engine', ref: 0 }) as const)
  on('prompt.compose', () => ({ sections: [{ id: 'base', text: 'base', scope: 'shared' }] }))
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  return clock
}

test('a hold shows in the band and the system prompt, and both go once it clears', async ($, on) => {
  const answer: { snap: DeckSnapshot | 'fail' } = { snap: HELD }
  const clock = world(on, answer)
  await $.session.start(START)
  await deck($, 'refresh')

  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ plugin: 'deck', surface, ...BAND })
    expect((await ui.find({ type: 'Text', text: /Landing blocked/ }))?.text).toContain('hold_sha is set (deadbeef)')
    await ui.unmount()
  }
  const held = await $.prompt.compose(COMPOSE)
  expect(held.sections.map(s => s.id)).toEqual(['base', 'deck:landing-blocked'])
  expect(held.sections[1]?.text).toContain('setup:k3s')

  // The timer alone must clear it: no /deck refresh here.
  answer.snap = CLEAR
  await clock.advance(60_000)

  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ plugin: 'deck', surface, ...BAND })
    expect(await ui.find({ type: 'Text', text: /Landing blocked/ })).toBeUndefined()
    await ui.unmount()
  }
  const clear = await $.prompt.compose(COMPOSE)
  expect(clear.sections.map(s => s.id)).toEqual(['base'])
})

test('the band and the prompt section each turn off and back on', async ($, on) => {
  world(on, { snap: HELD })
  await $.session.start(START)
  await deck($, 'refresh')

  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ plugin: 'deck', surface, ...BAND })
    await ui.press({ key: 'hide' })
    expect(await ui.find({ type: 'Text', text: /Landing blocked/ })).toBeUndefined()
    await ui.unmount()
    await deck($, 'band on')
    const shown = await $.ui.mount({ plugin: 'deck', surface, ...BAND })
    expect(await shown.find({ type: 'Text', text: /Landing blocked/ })).toBeDefined()
    await shown.unmount()
  }

  await deck($, 'context off')
  expect((await $.prompt.compose(COMPOSE)).sections.map(s => s.id)).toEqual(['base'])
  // The toggle is in $.store, so a new session reads it back.
  await $.session.start(START)
  await deck($, 'refresh')
  expect((await $.prompt.compose(COMPOSE)).sections.map(s => s.id)).toEqual(['base'])
  await deck($, 'context on')
  expect((await $.prompt.compose(COMPOSE)).sections.map(s => s.id)).toContain('deck:landing-blocked')
})

test('the pane lists runs and worktrees, and says what it could not read', async ($, on) => {
  const answer: { snap: DeckSnapshot | 'fail' } = { snap: CLEAR }
  world(on, answer)
  await $.session.start(START)
  await deck($, 'refresh')

  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ plugin: 'deck', surface, ...PANE })
    expect(await ui.find({ type: 'Text', text: /land PR 3700 42s/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /worktree-fanout-1 #3676/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /manual planes: unknown here/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /#3500 worktree-on-the-box/ })).toBeDefined()
    await ui.unmount()
  }

  answer.snap = 'fail'
  await deck($, 'refresh')
  const ui = await $.ui.mount({ plugin: 'deck', surface: 'terminal', ...PANE })
  expect(await ui.find({ type: 'Text', text: /probe.py landing failed/ })).toBeDefined()
  await ui.unmount()
  // A failed read keeps the last good snapshot rather than inventing a blocker.
  expect((await $.prompt.compose(COMPOSE)).sections.map(s => s.id)).toEqual(['base'])
})

test('/deck opens the pane and the same command closes it', async ($, on) => {
  world(on, { snap: CLEAR })
  await $.session.start(START)
  expect((await deck($, '')).text).toBe('Deck pane opened.')
  expect((await deck($, '')).text).toBe('Deck pane closed.')
  expect((await deck($, 'band maybe')).text).toContain('Usage')
})

test('a session in a checkout without probe.py runs no probe', async ($, on) => {
  const clock = mock.clock(on)
  mock.store(on)
  const probeRuns: string[] = []
  on('process.run', (_$, e) => {
    if (e.argv[0] === 'test') {
      return { value: { exitCode: 1, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    if (e.argv[0] === 'uv') {
      probeRuns.push(e.argv.join(' '))
    }
    return { value: { exitCode: 0, stdout: '/elsewhere\n', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('command.register', (_$, e) => ({ value: { command: e.name } }))
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  await $.session.start(START)
  await clock.advance(180_000)
  expect(probeRuns).toEqual([])
})
