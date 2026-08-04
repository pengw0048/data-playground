// Canvas file key contract: every newly created Canvas document id is an opaque, immutable,
// full-strength 128-bit random UUID (crypto.randomUUID). Clients mint the key before POST so
// local drafts and response-loss retries reuse the exact attempted identity. Legacy ids remain
// readable and writable; uniqueness and authorization stay server-authoritative.

export function newCanvasFileKey(): string {
  return globalThis.crypto.randomUUID()
}
