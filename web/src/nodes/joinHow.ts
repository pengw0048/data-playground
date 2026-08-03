// The engine accepts a wider `how` vocabulary than the Join card offers, so a stored value has to be
// resolved to the option naming the same join before the card can display it.

export const JOIN_HOW_OPTIONS = ['inner', 'left', 'right', 'outer']

const ENGINE_JOINS = ['inner', 'left', 'right', 'full', 'cross']

/** The join the engine runs for a stored `how`: `outer` is `full`, anything unrecognized is `inner`. */
function engineJoinHow(raw: unknown): string {
  const value = String(raw ?? '').trim().toLowerCase()
  const how = value === 'outer' ? 'full' : value
  return ENGINE_JOINS.includes(how) ? how : 'inner'
}

/** The offered option naming that join, or the engine's own keyword when no option names it. */
export function joinHowOption(raw: unknown, options: readonly string[] = JOIN_HOW_OPTIONS): string {
  const how = engineJoinHow(raw)
  if (options.includes(how)) return how
  return how === 'full' && options.includes('outer') ? 'outer' : how
}
