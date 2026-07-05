import { Component, type ReactNode } from 'react'
import { color } from '../theme/tokens'

// A real product never white-screens. Any render error is caught and shown inline.
export class ErrorBoundary extends Component<
  { children: ReactNode; fallback?: (err: Error, reset: () => void) => ReactNode; compact?: boolean },
  { err: Error | null }
> {
  state = { err: null as Error | null }

  static getDerivedStateFromError(err: Error) {
    return { err }
  }

  componentDidCatch(err: Error) {
    // eslint-disable-next-line no-console
    console.error('[ErrorBoundary]', err)
  }

  reset = () => this.setState({ err: null })

  render() {
    if (this.state.err) {
      if (this.props.fallback) return this.props.fallback(this.state.err, this.reset)
      return (
        <div style={{ padding: this.props.compact ? 14 : 24, color: color.text2, fontSize: 12.5, lineHeight: 1.5 }}>
          <div style={{ fontWeight: 600, color: color.failed, marginBottom: 6 }}>Something went wrong here.</div>
          <div className="dp-mono" style={{ fontSize: 11, color: color.text3, whiteSpace: 'pre-wrap', maxHeight: 120, overflow: 'auto' }}>
            {this.state.err.message}
          </div>
          <button onClick={this.reset} style={{ marginTop: 10, padding: '6px 12px', border: `1px solid ${color.border}`, borderRadius: 8, background: 'hsl(var(--card))', fontSize: 12, fontWeight: 600 }}>
            Dismiss
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
