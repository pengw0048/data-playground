# Data Playground — “接下来做什么” 路线图

_综合自 6 个并行调查员的 64 条原始发现（frontend-features:10, kernel-engine:17, collab-multiuser:11, robustness-tests:9, product-ux:10, todo-sweep:7），每条附 file:line 依据。_

## 交付状态 (2026-07-05)

- ✅ **Quick wins (7/7)** — atomic overwrite, subprocess run-history parity, resilient run polling, s3 storage boot, autosave 403-vs-offline, removed fake metric sparkline, grounded run estimate. (commit `7e15bb8`)
- ✅ **High-value (7/7)** — faithful preview (join/sort/vector), interruptible/lock-freeing execution, CRDT-scoped undo, viewer-role WS enforcement + ShareModal, clipboard/select-all/multi-duplicate, join ON-expression, source CSV parse options. (commit `78c8807`)
- ✅ **Bigger bets (3/4)** — Lance streaming scan (out-of-core), canvas version history + restore, Lance native ANN + external query vector. (+ fixed a latent non-unique canvas-id bug)
- ⏳ **Bigger bet — real per-user identity (BB.2)**: NOT shipped. The current shared-password gate is a *documented, intentional* internal-tool simplification, and a real per-user credential/SSO model forces product decisions (per-user passwords vs OIDC; provisioning; reset flow) + is a security-sensitive, hard-to-reverse change. Needs an owner decision before implementation — see below.

## 总览判断

The kernel is a genuinely real out-of-core DuckDB lowering engine with working object-store I/O, a content-addressed cache, run history, and a live CRDT collab layer — this is a serious product, not a demo. But its two headline promises are currently violated in the exact places users hit at scale: "the sample is faithful to the real run" is false for join/sort/vector-search, and "out-of-core" is false for Lance (full materialization), while the collab and permission layers can silently lose or leak a user's work. The single most important theme to pursue next is closing the correctness/trust gaps that make real features quietly behave differently than advertised — data-safe writes, faithful previews, enforceable sharing, and interruptible/bounded execution — before adding surface area.


---

## ⚡ Quick wins（S 成本，先做）

_S-effort fixes that stop silent data loss, un-wedge the UI, or delete fake affordances — the maintainer's 'no half-baked things' bar is served most cheaply here._

### 1. Write to a temp path then os.replace on success (overwrite safety)

`kernel/kernel/plugins/adapters.py (DuckDBAdapter.write)` · bug · 成本 **S** · 置信 **high**

- **做什么**：Overwrite writes go straight to the final target via write_parquet/write_csv/feather/COPY. A mid-write failure or a cancelled run (subprocess hard-kill) truncates the pre-existing file and the old data is gone. Write to a sibling temp file and os.replace on success.
- **价值**：Silent, unrecoverable loss of a user's materialized dataset on a re-run failure is the most catastrophic outcome for a tool trusted with real data.
- **证据**：`adapters.py:184-194 writes directly to target; temp+rename exists nowhere except subrun.py status JSON; SubprocessRunner.cancel proc.terminate() (subprocess_runner.py:123-131) can kill mid-write.`

### 2. Record run history for the subprocess execution backend

`kernel/kernel/deps.py + subprocess_runner.py` · bug · 成本 **S** · 置信 **high**

- **做什么**：on_complete (which persists a finished run to history) is wired only onto the in-process LocalRunner. Choosing Settings → Execution = isolated process silently stops recording run history for every canvas.
- **价值**：Run history / lineage is a headline feature; having it vanish based on a backend toggle is an invisible data-integrity regression.
- **证据**：`deps.py:98 sets on_complete only on LocalRunner; SubprocessRunner never calls metadb.record_run; record_run's sole caller is deps._persist_run; run-history test (test_kernel.py:699) uses the in-process runner, so the gap is untested.`

### 3. Retry the run-status poller on a transient error instead of abandoning it

`web/src/store/graph.ts (pollRun)` · bug · 成本 **S** · 置信 **high**

- **做什么**：pollRun stops polling permanently on the first failed status request; a single network blip or brief kernel restart mid-run leaves the node spinning in 'running' forever with no resolution. Retry a few times with backoff before giving up.
- **价值**：Transient fetch failures are routine; permanently wedging the run UI on one dropped request looks broken and forces a reload that loses run context.
- **证据**：`graph.ts:833-837 `catch { return }` returns without rescheduling tick (only reschedules at 867/869); runs[nodeId].status stays 'running' so the card indicator never resolves.`

### 4. Fix DP_STORAGE_URL=s3:// bricking kernel boot while object-store writes work elsewhere

`kernel/kernel/storage.py + deps.py` · bug · 成本 **S** · 置信 **high**

- **做什么**：make_storage raises NotImplementedError for s3://gs:// and is called eagerly in Deps.__init__, so the documented way to point outputs at S3 crashes get_deps() and makes the kernel unusable — even though object-store read/write is fully implemented via httpfs and destinations._default_root accepts s3:// roots.
- **价值**：A single documented env var fatally taking down the kernel is a sharp deploy edge that contradicts the codebase's own 'fail loudly, never silently write local' claim.
- **证据**：`storage.py:46-47 raises for s3/gs; deps.py:87-88 calls make_storage(workspace) in __init__ (deps.py:200-206); contrast working paths destinations.py:103-118 and adapters.py:159-194.`

### 5. Distinguish permission-denied (403) from offline in autosave

`web/src/store/graph.ts (autosave catch)` · bug · 成本 **S** · 置信 **high**

- **做什么**：The autosave catch treats ANY save failure as 'offline' (kernelUp=false) and marks the doc saved. A 403 (viewer, or access just revoked) is reported as a network problem while edits are silently dropped. Detect the 403 and surface a real 'you no longer have edit access' state.
- **价值**：Misclassifying 'you can't save this' as 'network is down' actively misleads users into thinking their work is safe while it is being lost.
- **证据**：`graph.ts:796-799 single catch → set({ saved: true, kernelUp: false }) for all errors; server 403 at main.py:463-464.`

### 6. Remove or implement the fake metric 'sparkline'

`web/src/nodes/kinds/metric.tsx + panels/DataPanel.tsx` · stubbed · 成本 **S** · 置信 **high**

- **做什么**：The metric card advertises 'value + sparkline' but nothing renders a series: the viewer shows one number and the backend emits exactly one scalar row. Either compute a real grouped/time-bucketed series and draw it, or drop the word 'sparkline'.
- **价值**：Exactly the demo-ish micro-copy that undermines trust — it promises a visualization that does not exist.
- **证据**：`metric.tsx:15,38 say '· value + sparkline'; DataPanel.tsx:181-189 MetricValue renders only rows[0].value; engine.py:245-248 emits a single-row SELECT metric, value.`

### 7. Ground the run estimate and don't skip the confirm gate on unknown size

`kernel/kernel/main.py (_row_estimate) + plugins/runner.py + web/panels/RunPanel.tsx` · thin · 成本 **S** · 置信 **high**

- **做什么**：The prominent '~Xs' estimate is rows × never-measured constants; _row_estimate returns only the FIRST source's count (ignoring join sides and filter selectivity) and falls back to a literal 1000 when count() throws — which also silently defeats the 5M-row confirm gate on the exact inputs whose size is hardest to know. Show only the real row count (drop invented seconds) and err toward requiring confirmation when size is unknown.
- **价值**：The project explicitly purged 'cost/placement theater' yet still shows a precise-looking time that can be off by orders of magnitude, and the confirm gate is the only guard against accidentally launching a massive full pass.
- **证据**：`runner.py:30-33 uncalibrated _OP_SECONDS_PER_1K, runner.py:64-69 seconds formula, runner.py:34/67 _CONFIRM_ROWS gate; main.py:285-298 first-source-only with `except: pass; return 1000`; adapters.py:146-150 count returns None on error; RunPanel.tsx:32 renders ~{fmtTime(est.seconds)}.`


---

## 🎯 High-value（M 成本，修复核心信任契约）

_M-effort work that repairs the product's two core trust contracts (faithful previews, safe collaboration) and the ingestion/editing table stakes a node-based data tool must have._

### 1. Stop faking faithful previews for join / sort / vector-search

`kernel/kernel/executors/engine.py + preview.py` · bug · 成本 **M** · 置信 **high**

- **做什么**：Preview bounds each source to its first 2000 rows independently, then runs the real relational op on the truncated inputs. A join joins two non-corresponding prefixes (often showing zero matches vs. the real run); sort/vector-search show the top-K of an arbitrary 2000-row prefix. Only {aggregate,write,opaque,loop,section} are excluded, so these three silently mislead. Either flag them not-sample-previewable (like aggregate) or preview over a real random sample and label it approximate.
- **价值**：'What you see on the sample is faithful to what runs at scale' is the product's headline guarantee; a join preview showing 0 rows will make users think their pipeline is broken.
- **证据**：`engine.py:139-141 each source .limit(sample_k); engine.py:105-115 _inputs resolves each join input's own source; engine.py:210-221 join over truncated relations; engine.py:25 NOT_PREVIEWABLE_KINDS excludes join/sort/vector-search; preview.py:15,29 sample_k=2000; docstring engine.py:6-7 promises the sample is faithful.`

### 2. Bound and interrupt user code that holds the global DuckDB lock

`kernel/kernel/plugins/runner.py + executors/{preview,engine,section}.py` · bug · 成本 **M** · 置信 **high**

- **做什么**：run_with_timeout is wired only into preview, and even there the 'timed-out' worker thread keeps running and keeps holding the process-global db.lock() — a runaway transform (`while True`) or a section script that never calls run() wedges every later preview/run/sample until the kernel restarts. On the full-run path there is no time budget at all, and LocalRunner checks the cancel Event only between steps (nothing calls DuckDB's connection.interrupt()), so Cancel flips the UI to idle while the step keeps executing and the lock stays pinned. Add a real wall-clock budget and interrupt in-flight work; keep the 'cancelled' status honest.
- **价值**：For a shared collaborative kernel, one bad cell taking down everyone's data plane with no recovery but a restart — and a Cancel button that lies while work continues — is a severe availability bug.
- **证据**：`preview.py:32-47 work() acquires db.lock() inside a thread sandbox.run_with_timeout can't kill (sandbox.py:104-122); schema.py:26-27 documents the wedge; runner.py:126 wraps the whole run in db.lock(), user code at engine.py:300-307/340-344 and section.py:153 exec has no timeout; runner.py:129 cancel checked only between steps and runner.py:241-244 sets status='cancelled' unconditionally; `grep interrupt` across kernel/ returns nothing; web graph.ts:587-589 optimistically sets idle.`

### 3. Make undo/redo CRDT-aware so it stops erasing peers' live edits

`web/src/store/graph.ts + collab/ydoc.ts` · bug · 成本 **M** · 置信 **high**

- **做什么**：Undo/redo replace the whole store doc with a stale full-doc snapshot, which is then diffed into the Y.Doc and DELETES any node/edge a peer added after the snapshot — for everyone. Undo is not scoped to the local user's ops. Replace the snapshot stack with a Yjs UndoManager scoped to local (origin='store') edits.
- **价值**：Silent data loss during co-editing is the worst failure for a collaboration product — one Cmd-Z can erase a teammate's in-progress work with no warning or recovery.
- **证据**：`graph.ts:302-320 push/restore full-doc snapshots in past/future; ydoc.ts:52-53,67 pushDocToY deletes any id not in the pushed doc, fed by the store subscription at ydoc.ts:93-98; remote edits applied via setState (ydoc.ts:86) never enter the undo stack.`

### 4. Enforce the viewer / read-only role end-to-end

`kernel/kernel/main.py + metadb.py + web/panels/ShareModal.tsx + canvas/Canvas.tsx` · stubbed · 成本 **M** · 置信 **high**

- **做什么**：The 'viewer' role exists in the model and canvas_role() returns it, but nothing honors it: ShareModal only ever assigns 'editor', the frontend never consumes CanvasFile.role to gate editing, and ws_collab admits any non-None role then blindly fans out every doc update, so a viewer's CRDT edits reach editors who autosave them. Additionally 'workspace' visibility returns 'editor' for everyone (make-visible silently means let-everyone-edit). Assign viewer in ShareModal, disable editing when role is viewer, gate collab relay by write-role server-side, and add a workspace-viewer tier.
- **价值**：A read-only share that actually lets the recipient edit (and persists it via other editors) is a broken trust boundary; 'viewer' is the most common sharing tier and today it is fiction.
- **证据**：`metadb.py:64,167-178 role/canvas_role can return viewer; metadb.py:175-176 workspace→'editor'; main.py:586-608 ws_collab only checks canvas_role is None then fans out everything; contrast put_canvas 403 at main.py:458-464; ShareModal.tsx:25 hardcodes 'editor', :54-60 presents workspace under neutral 'Visibility'; client.ts:168 CanvasFile.role unused; Canvas.tsx:81-84 connectCollab runs regardless of role.`

### 5. Add copy / cut / paste, select-all, and multi-node duplicate for canvas nodes

`web/src/canvas/Canvas.tsx (keyboard) + store/graph.ts` · missing · 成本 **M** · 置信 **high**

- **做什么**：There is no clipboard: no Cmd+C/X/V, no Cmd+A select-all, and duplicate only copies a single node via menus. Add an in-app clipboard that copies selected nodes + their internal edges, remaps ids and offsets position on paste (works across open canvases), make duplicate operate over selectedIds, and add Cmd+A.
- **价值**：Copy/paste of configured nodes and subgraphs is the single most-used editing action on a node canvas ('ComfyUI for data'); its absence blocks reusing pipeline fragments across the multi-file workspace.
- **证据**：`Canvas.tsx:205-241 handles only z/y/Escape/Delete/Backspace/b/d (no c/v/x/a); graph.ts:479-492 duplicate is single-id, invoked only from NodeCard.tsx:289 and Inspector.tsx:140; removeSelected (graph.ts:436-455) already iterates selectedIds, so delete-many vs duplicate-one is asymmetric; navigator.clipboard used only for the share link.`

### 6. Support joins on differently-named keys and ON expressions (not just USING)

`kernel/kernel/executors/engine.py (join lowering)` · thin · 成本 **M** · 置信 **high**

- **做什么**：The join node only emits JOIN … USING (cols), requiring identical key names on both sides — you cannot join a.user_id = b.uid or on a composite/expression condition. Same-named non-key columns also collide, since the de-dup runs only at the preview display layer, not in the real relation, so a downstream select/sql sees ambiguity.
- **价值**：Equi-join on identically-named keys is a small subset of real joins; without ON left=right this node can't express most production joins.
- **证据**：`engine.py:210-221 builds JOIN … USING ({cols}); only escape is cross join; _dedupe_names (engine.py:96-99,427-437) runs solely in rows()/preview, not in _lower.`

### 7. Add format/parse options to the source node and read heterogeneous directories/prefixes

`kernel/kernel/nodespecs.py + plugins/adapters.py` · missing · 成本 **M** · 置信 **high**

- **做什么**：The source node exposes only a uri and the adapter uses pure auto-detection with no overrides — semicolon delimiters, no-header, custom NULL tokens, non-UTF8, JSON records vs ndjson, and Hive-partitioned parquet can't be read correctly. A directory source also returns only the first extension that has files, and object-store prefixes hardcode '/**/*.parquet', so an s3:// prefix of CSV/JSON parts reads nothing. Add CSV/JSON knobs, hive_partitioning, and mixed-type directory handling.
- **价值**：Ingesting messy real data and partitioned/heterogeneous data lakes is table stakes; auto-detect-only means a large class of files silently import wrong, partial, or not at all.
- **证据**：`nodespecs.py:61-63 source params = uri only; adapters.py:114-128 read_csv/read_json with no options; adapters.py:134-141 _read_dir returns on first matching extension; adapters.py:120 hardcodes /**/*.parquet for any object-store prefix.`


---

## 🚀 Bigger bets（L 成本，战略投入）

_L-effort, strategic investments that make the product's core positioning (out-of-core, vector-native, multi-user, auditable) actually hold under real workloads._

### 1. Stream Lance full scans into DuckDB instead of materializing the whole dataset

`kernel/kernel/plugins/adapters.py (LanceAdapter.scan)` · thin · 成本 **L** · 置信 **high**

- **做什么**：A full (limit=None) Lance scan calls ds.to_table() and loads the entire dataset into RAM before handing it to DuckDB, unlike the streaming Parquet path — so any real-scale Lance run/write can OOM and defeat the out-of-core guarantee. Feed Lance via ds.scanner(...).to_reader() into con.from_arrow(reader).
- **价值**：Lance is a core adapter and the product is positioned as a bigger-than-RAM engine; the embeddings/large-media tables users reach Lance for are exactly the case that silently falls over.
- **证据**：`adapters.py:214-221 scan does ds.to_table(columns, limit); docstring adapters.py:199-203 admits 'a full scan currently materializes the dataset … not yet streaming'; reached from engine.py:138 with no limit on full runs.`

### 2. Make per-user identity real so the owner/editor/viewer ACL is enforceable

`kernel/kernel/auth.py + main.py (/auth/login)` · thin · 成本 **L** · 置信 **high**

- **做什么**：With auth enabled, login checks one shared DP_AUTH_PASSWORD and then trusts whatever userId the request body claims, so any password holder can sign in as any other user and edit their private canvases. The full owner/editor/viewer + share/unshare ACL only isolates outsiders, not co-holders — the complete Share UI implies per-person control that isn't enforced. Wire a per-user credential/SSO into the existing /auth/login plumbing.
- **价值**：A serious multi-user product with a Figma-style Share dialog invites the assumption that private means private; making the identity factor real turns the already-built ACL from decorative into enforceable.
- **证据**：`auth.py:8-13,63-66 shared-password caveat; main.py:378-389 auth_login signs whatever userId the body asserts; full ACL at main.py:458-507; web ShareModal.tsx presents per-person sharing.`

### 3. Add canvas-level version history and restore

`kernel/kernel/metadb.py + main.py (put_canvas)` · missing · 成本 **L** · 置信 **high**

- **做什么**：Every autosave overwrites Canvas.doc in place (every ~400ms); there is no server-side snapshot log and no way to restore an earlier state after a bad edit. Only per-node client-side history and run history exist. Add periodic/named canvas snapshots with a restore endpoint, and reject stale writes via the already-stored but unused version integer (optimistic concurrency).
- **价值**：'Undo the mistake I made yesterday' and auditability are table stakes for a serious data product; without versioning a single bad autosave (or a lost-update race between two tabs/editors) is unrecoverable.
- **证据**：`metadb.py:46-55 single doc column, no history table; main.py:458-471 put_canvas overwrites doc/version unconditionally with no If-Match check; metadb.py:52 version stored but never compared; autosave graph.ts:786-800 fires every 400ms so bad states persist immediately.`

### 4. Push vector search into Lance's ANN index and allow an external query vector

`kernel/kernel/executors/engine.py + plugins/adapters.py + nodespecs.py + web/nodes/kinds/vectorSearch.tsx` · thin · 成本 **L** · 置信 **medium**

- **做什么**：vector-search always brute-force full-scans cosine over every row and can only use an existing row (queryRow) as the query — even on a Lance dataset with a native vector index it ignores it, and there's no way to supply an arbitrary/text query vector. Push nearest= into LanceAdapter.scan, accept an external query vector, and surface the query-row selector on the card (today queryRow is only reachable via the Inspector).
- **价值**：Lance/vector is a called-out capability; brute-force-over-the-whole-dataset defeats the point of an ANN index, won't scale, and the card can't even express the feature's defining input.
- **证据**：`engine.py:364-384 scans all rows with list_cosine_similarity ORDER BY … LIMIT k; LanceAdapter.scan (adapters.py:214-221) does only column/limit pushdown; nodespecs.py:119-124 offers only column/queryRow/k; vectorSearch.tsx:8-19 reads only column and k (queryRow absent from web/src).`
