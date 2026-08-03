import { mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

export const providerAcceptanceNames = {
  containerA: 'Browser provider collection A',
  containerB: 'Browser provider collection B',
  datasetA: 'Browser provider observations',
  datasetB: 'Browser provider observations',
  relatedDataset: 'Browser provider labels',
}

export function prepareProviderAcceptanceFixture(providerRoot) {
  const root = resolve(providerRoot)
  mkdirSync(root, { recursive: true })
  writeFileSync(resolve(root, 'observations.csv'), 'id,value\n1,alpha\n2,beta\n')
  writeFileSync(resolve(root, 'labels.csv'), 'id,label\n1,one\n2,two\n')
  writeFileSync(resolve(root, 'catalog.json'), JSON.stringify({ resources: [
    {
      placementId: 'browser-collection-a',
      kind: 'container',
      name: providerAcceptanceNames.containerA,
    },
    {
      placementId: 'browser-collection-b',
      kind: 'container',
      name: providerAcceptanceNames.containerB,
    },
    {
      placementId: 'browser-observations-a',
      datasetId: 'browser-canonical-observations',
      kind: 'dataset',
      name: providerAcceptanceNames.datasetA,
      parentPlacementId: 'browser-collection-a',
      uri: 'observations.csv',
      revisionId: 'browser-provider-revision-v1',
      columns: [{ name: 'id', type: 'int' }, { name: 'value', type: 'string' }],
    },
    {
      placementId: 'browser-observations-b',
      datasetId: 'browser-canonical-observations',
      kind: 'dataset',
      name: providerAcceptanceNames.datasetB,
      parentPlacementId: 'browser-collection-b',
      uri: 'observations.csv',
      revisionId: 'browser-provider-revision-v1',
      columns: [{ name: 'id', type: 'int' }, { name: 'value', type: 'string' }],
    },
    {
      placementId: 'browser-labels-a',
      datasetId: 'browser-canonical-labels',
      kind: 'dataset',
      name: providerAcceptanceNames.relatedDataset,
      parentPlacementId: 'browser-collection-a',
      uri: 'labels.csv',
      revisionId: 'browser-provider-labels-v1',
      columns: [{ name: 'id', type: 'int' }, { name: 'label', type: 'string' }],
    },
  ] }))
}
