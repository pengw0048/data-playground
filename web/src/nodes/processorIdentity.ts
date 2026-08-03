import type { ProcessorDescriptor } from '../types/api'

export function exactProcessor(
  processors: ProcessorDescriptor[], processor: unknown, version: unknown,
): ProcessorDescriptor | undefined {
  return processors.find((candidate) => (
    candidate.id === processor && candidate.version === version
  ))
}

export function configuredProcessorRef(processor: unknown, version: unknown): string | undefined {
  if (typeof processor !== 'string' || !processor) return undefined
  return `${processor}@${typeof version === 'string' && version ? version : '?'}`
}

export function processorModeLabel(mode: unknown): string {
  switch (mode) {
    case 'map': return 'Per row'
    case 'map_batches': return 'In batches'
    case 'filter': return 'Filter rows'
    case 'flat_map':
    case 'flat_map_generator': return 'Expand rows'
    case 'callable': return 'Whole dataset'
    case 'aggregate': return 'Aggregate rows'
    default: return typeof mode === 'string' && mode ? mode : 'Defined by the Library'
  }
}
