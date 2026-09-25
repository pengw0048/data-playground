# S3-compatible storage for Data Playground

Data Playground's default local mode uses an embedded metadata database and local files. It does not
require an object-store server. The optional Ray Compose and KubeRay validation environments use
**SeaweedFS 4.47** for shared S3 storage. The application continues to use standard S3 clients and the
existing endpoint/credential settings; it has no SeaweedFS-specific data format or SDK dependency.

## Why SeaweedFS

MinIO's community repository was archived on April 25, 2026 and explicitly states that it is no
longer maintained. The old validation images also became unavailable from Docker Hub. Building the
archived server ourselves would restore an executable without restoring upstream maintenance.
See the [MinIO repository](https://github.com/minio/minio).

The replacement was selected against the application's actual storage contract, rather than a
throughput ranking. The comparison was checked on September 25, 2026:

| Candidate | Relevant fit | Decision |
| --- | --- | --- |
| SeaweedFS 4.47 | Supports versioned objects, delete markers, multipart uploads, range reads, and conditional writes; `weed mini` provides a single-process deployment for the disposable harnesses. | Selected and exercised with the project's clients. |
| Garage 2.4.1 | Actively maintained and suitable for other S3 workloads, but its documented API coverage omits bucket versioning and `ListObjectVersions`. Its ordinary PUT path also lacks the conditional write behavior this application needs. | Cannot replace this project's full storage contract. |
| RustFS 1.0.0 | Its first GA release includes broad S3 tests, including versioning and conditional writes, but has a much shorter stable release history. | A future alternative to validate, rather than an additional supported harness now. |

Primary sources: [SeaweedFS 4.47](https://github.com/seaweedfs/seaweedfs/releases/tag/4.47),
[conditional operations](https://github.com/seaweedfs/seaweedfs/wiki/S3-Conditional-Operations),
[object versioning](https://github.com/seaweedfs/seaweedfs/wiki/S3-Object-Versioning),
[Garage API coverage](https://garagehq.deuxfleurs.fr/documentation/reference-manual/s3-compatibility/),
[Garage 2.4.1 PUT implementation](https://github.com/deuxfleurs-org/garage/blob/v2.4.1/src/api/s3/put.rs),
[RustFS 1.0.0 compatibility boundary](https://github.com/rustfs/rustfs/blob/1.0.0/docs/architecture/s3-compatibility-matrix.md).

[Community comparisons](https://www.reddit.com/r/selfhosted/comments/1qcm5r5/what_is_the_best_minio_alternative_right_now/)
contain useful setup experiences but disagree about complexity and reliability, and often refer to
older versions. They informed the shortlist, not the compatibility guarantees below.

## Capabilities that must remain intact

The application persists object version identities and uses conditional namespace markers to
prevent competing installations from claiming the same storage. An endpoint must support more than
successful PUT and GET requests:

- Complete paginated object-version inventory, including non-current versions, delete markers,
  current-version flags, sizes, and ETags.
- Reading and deleting a specific version without accidentally changing another version.
- Enumerating and aborting unfinished multipart uploads, and completing readable multipart objects.
- `If-None-Match: *` creation and `If-Match` updates that reject competing writers with a real
  precondition conflict. Ignoring those headers is unsafe.
- HEAD, ranged GET, CopyObject, and deletion as used by the Python and Arrow clients. In particular,
  switching a file output from overwrite to append must retain the earlier rows.

`python -m hub.s3_storage_check` exercises these capabilities using a disposable prefix in an
already-versioned validation bucket. The Ray harnesses additionally exercise distributed Parquet
reads, result publication, cancellation, restart recovery, and corrupt-result rejection.

One observed compatibility boundary is the empty directory marker whose key ends in `/`:
SeaweedFS 4.47 reports its version as `null`, even in a versioned bucket. The application treats
this zero-byte marker as directory metadata; its existing inventory and exact-delete operations
preserve and accept that explicit identity. The live check verifies those operations and keeps
the requirement for distinct, non-null version IDs on actual data objects. Do not infer AWS-style
directory-marker history from the successful data-object checks.

SeaweedFS's S3 Bucket Replication API is not a substitute for MinIO's replication commands. The
backup procedure and its executable drill are documented separately in
[Backup and restore](BACKUP_RESTORE.md); a readable current object alone does not prove that history
can be restored.

## Deployment and migration boundary

The checked-in environments are disposable validation topologies with isolated networks and test
credentials. Their single-node `weed mini` process is not a claim of production high availability.
See [Ray validation](RAY.md), [Ray Jobs](RAY_JOBS.md), and the
[KubeRay harness](../deploy/kuberay/README.md) for their commands.

Existing external S3 endpoints remain configurable. This change does not migrate a user's MinIO
bucket, metadata database, or data directory. MinIO and SeaweedFS on-disk directories are not
interchangeable. Re-uploading objects can change their version IDs and lose delete-marker history,
so changing a production endpoint requires an independently verified migration that preserves the
identities recorded in the database, or an explicit new installation and re-import. Do not point an
existing metadata database at a latest-objects-only copy and call it a restored installation.
