/** One monotonic ownership epoch for top-level navigation destinations. */
export type NavigationToken = number

let currentToken = 0

export function startNavigation(): NavigationToken {
  currentToken += 1
  return currentToken
}

export function ownsNavigation(token: NavigationToken): boolean {
  return token === currentToken
}
