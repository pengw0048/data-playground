import type { NodeSpec } from '../nodes/registry'
import { Icon, type IconName } from '../ui/Icon'

const KIND_ICON: Record<string, IconName> = {
  source: 'db',
  write: 'export',
  filter: 'filter',
  select: 'columns',
  sample: 'sample',
  sort: 'sort',
  dedup: 'check',
  window: 'sort',
  fill: 'columns',
  unnest: 'union',
  unpivot: 'columns',
  pivot: 'columns',
  join: 'join',
  union: 'union',
  aggregate: 'sigma',
  transform: 'fx',
  sql: 'sql',
  'vector-search': 'search',
  chart: 'chart',
  metric: 'sigma',
  assert: 'check',
  note: 'note',
  code: 'code',
  section: 'grid',
}

const CATEGORY_ICON: Record<NodeSpec['category'], IconName> = {
  io: 'db',
  shape: 'columns',
  compute: 'fx',
  query: 'sql',
  inspect: 'eye',
  control: 'code',
}

export function nodeTypeIconName(spec: Pick<NodeSpec, 'kind' | 'category'>): IconName {
  return KIND_ICON[spec.kind] ?? CATEGORY_ICON[spec.category] ?? 'grid'
}

export function NodeTypeIcon({ spec, size = 15 }: {
  spec: Pick<NodeSpec, 'kind' | 'category'>
  size?: number
}) {
  return <Icon name={nodeTypeIconName(spec)} size={size} />
}
