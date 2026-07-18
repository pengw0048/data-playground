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
