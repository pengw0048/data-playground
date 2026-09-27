import { beforeEach, expect, it } from 'vitest'
import { resultViewKey, useResultViewStore } from './resultView'

beforeEach(() => useResultViewStore.setState({ views: {} }))

it('remembers only the twenty most recently opened or navigated saved results', () => {
  const { remember } = useResultViewStore.getState()
  for (let index = 0; index < 20; index++) remember(`result-${index}`, { offset: index })
  remember('result-0', { mode: 'full' })
  remember('result-20', { offset: 50, previousOffsets: [0] })
  const { views } = useResultViewStore.getState()
  expect(Object.keys(views)).toHaveLength(20)
  expect(views['result-1']).toBeUndefined()
  expect(views['result-0']).toEqual({ mode: 'full', offset: 0, previousOffsets: [] })
  expect(views['result-20']).toEqual({ offset: 50, previousOffsets: [0] })
})

it('does not store a provisional mode before the principal and exact artifact are known', () => {
  useResultViewStore.getState().remember(resultViewKey('alice', undefined, 'node', 'out', undefined), {
    mode: 'sample',
  })
  useResultViewStore.getState().remember(resultViewKey(undefined, 'run', 'node', 'out', '/result'), {
    mode: 'full',
  })
  expect(useResultViewStore.getState().views).toEqual({})
})
