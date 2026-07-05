# Data Playground — 6-Role Review

_综合自 6 个角色的 57 条发现（usage-ux:9, design:10, code:9, system-design:8, security:8, extensibility:13）。评审结论未经二次对抗验证；安全项已单独实证。_

## 总体

Data Playground is a genuinely working product, not a demo — there is a real DuckDB out-of-core lowering engine, a per-run subprocess backend, a CRDT collab layer, and a coherent token system. But it straddles two incompatible identities: the kernel docstring says "one kernel per open canvas session" while the code ships multi-user auth, Postgres, workspace shares, and a process-global singleton catalog/DuckDB connection — so a serious-productivity framing collides with single-tenant internals. The frontend is polished but has confirmed interaction bugs (multi-select spawns N stuck action shelves, note edit-mode can't scroll, ports afford nothing visually) that all trace to hand-rolled primitives and per-node selection logic. Security is the weakest lens: the execution and data routes have no auth at all even with DP_AUTH_SECRET set. Extensibility is aspirational — the advertised SPI contract file is dead code and every built-in node is defined twice.

## 设计体系结论

Recommendation: adopt Radix UI (or its actively-maintained successor Base UI) as a BEHAVIOR-ONLY primitives layer, and keep theme/tokens.ts as the single visual source of truth. Reject the other options concretely: "Figma UI3" is a visual design language, not an installable React package — it can inform tokens but ships zero components, so it cannot answer the "which library" question. MUI/AntD impose Material/Ant visual languages and heavy bundles that would fight the tuned canvas look and force a rewrite. shadcn drags in Tailwind, a full paradigm shift away from this inline-style + token app. Radix/Base UI are unstyled and headless: you drive them with the existing tokens and inline styles, so the look never changes. Migration sketch: (1) wrap Radix Popover behind web/src/ui/Popover.tsx's current props API (anchorRef/open/onClose/width) so no call site changes; (2) do the same for Tooltip and the DropdownMenu used by MoreMenu (NodeCard.tsx:250) and AppMenu (TopBar.tsx:100-176); (3) migrate Dialog/Select last. One primitive at a time, each behind the existing wrapper. What a primitives layer eliminates for free: the hand-rolled Popover.tsx (no focus trap, no keyboard/typeahead nav, clamp-only collision handling at :31), Tooltip.tsx (no delay group, no aria-describedby), and the copy-pasted menu-item hover/roving-focus patterns across NodeCard/TopBar/Toolbar. It does NOT fix the note-scroll bug (that is a React Flow nowheel issue) or the multi-select shelf bug (selection logic) — those are separate. Pair the primitives migration with a thin component layer (Button/IconButton/MenuItem) and add spacing + type scales, since radius is currently bypassed 119 literal times vs 26 token refs and two different "primary" blues exist (#2f6ef0 Share vs #3b7fe0 focus).

## 确认的 Bug（优先级从高到低）

### [M] Execution and data routes have NO authentication — arbitrary file read + code exec
- **修复**：Attach dependencies=[Depends(current_user)] at the APIRouter level (main.py:49) or add it to every state-changing/data route.
- **证据**：`main.py:316 /run, :204 /run/preview, :95 /catalog/register, :122 /data/sample, :238 /destinations/browse — none declare Depends(current_user); grep confirms current_user is wired only to canvas/settings/auth/me routes. No dependencies= on the APIRouter (main.py:49). With DP_AUTH_SECRET set these stay fully open: unauthenticated POST /api/run with a sql/transform node yields file read + code exec.`

### [S] POST /users self-registration is a complete auth bypass
- **修复**：Gate create_user/list_users behind current_user plus an admin role.
- **证据**：`main.py:438-446 create_user and :432-435 list_users have no Depends(current_user); /auth/login (:382-395) then verifies against the password hash the attacker just set. GET /users also leaks all emails.`

### [S] Marquee-select shows one stuck action shelf per selected card
- **修复**：showShelf = hover || busy || (selected && selectedIds.length === 1).
- **证据**：`web/src/nodes/NodeCard.tsx:64 showShelf = selected || hover || busy; :24 selected = selectedIds.includes(id); box-select folds every id into selectedIds at web/src/canvas/Canvas.tsx:133-138. N selected cards render N floating shelves that persist until deselect (the reported 'two bars stuck').`

### [S] Note edit-mode textarea cannot scroll — wheel pans the canvas
- **修复**：Add React Flow's nowheel class to the textarea (and other scrollable node bodies), plus maxHeight + overflow:auto.
- **证据**：`web/src/nodes/kinds/note.tsx:28 textarea className='nodrag dp-mono' (no nowheel); web/src/canvas/Canvas.tsx:275 panOnScroll; grep for 'nowheel' over web/src returns zero hits, so every in-node scroll area is affected.`

### [M] GET /settings discloses object-store secret keys in plaintext
- **修复**：Redact secret-bearing global keys from GET /settings, restrict global-scope writes to admin, keep secrets encrypted / out of the metadata DB.
- **证据**：`db.py:60-77 stores accessKeyId/secretAccessKey; get_settings (main.py:576-586) returns every global setting verbatim via json.loads(r.value); put_setting (:589-593) lets any current_user write global scope; metadb stores json.dumps(value) as plaintext Text.`

### [M] Unconfined dataset URIs enable arbitrary local-file read and SSRF
- **修复**：Allowlist URI schemes, confine local paths to workspace roots, disable extension auto-install/auto-load.
- **证据**：`main.py:100-101 uri=os.path.abspath(os.path.expanduser(req.uri)) with no root check; adapters.py:140-151 passes an http(s) uri straight to con.read_parquet/read_csv; db.py:35-36 sets autoinstall/autoload_known_extensions=true so httpfs auto-loads.`

### [L] Global DuckDB lock held for an entire run serializes all users
- **修复**：Run concurrent work on per-run conn.cursor() (shares catalog, allows parallelism) or default heavy runs to the subprocess backend; lock per-materialization, not per-run.
- **证据**：`kernel/kernel/plugins/runner.py:128 db.lock().acquire() before the step loop, released only in finally at :174; db.py:16 single global RLock; preview (executors/preview.py) and /data/sample (main.py:132) also take db.lock(). One long run blocks every preview/sample/run across all users; sync FastAPI handlers pin threadpool workers so the app goes unresponsive.`

### [M] Every collaborator re-persists the full doc on each remote edit (N-way write amplification)
- **修复**：Skip autosave when the change originated from a remote Y update (guard on the applying flag) or elect a single persister; add optimistic-concurrency on version.
- **证据**：`web/src/collab/ydoc.ts:85-93 a remote Y update rebuilds the whole store doc via setState({doc: yToDoc}); the autosave subscriber at store/graph.ts:857-874 has no origin/applying guard so it PUTs on ANY doc change; put_canvas (main.py:481-499) stores version but never compares it — last-write-wins despite the CRDT.`

### [M] AST dunder guard bypassable via str.format in the in-process sandbox
- **修复**：Block format/format_map (or use a restricted string.Formatter) and default untrusted transforms to SubprocessRunner.
- **证据**：`sandbox.py:66-74 walks only ast.Attribute/ast.Name; _SAFE_BUILTINS includes 'format' and 'str' (sandbox.py:35-37), so "{0.__class__.__mro__[-1].__subclasses__}".format(()) reaches object through a string literal. pick_runner defaults to in-process LocalRunner (deps.py:95-104).`

## 分角色要点

### usage-ux
- **[high/S] Gate the action shelf to single-node selection** — Marquee/shift-selecting N cards floats N action shelves at once, each with single-node actions, and they persist until deselect.  
  `web/src/nodes/NodeCard.tsx:64 showShelf, :24 selected, :150 shelf render; Canvas.tsx:133-138 folds all ids into selectedIds`
- **[high/S] Add nowheel so a note's edit-mode textarea can scroll** — panOnScroll swallows the wheel over the textarea; it carries only nodrag dp-mono, so text never scrolls.  
  `web/src/nodes/kinds/note.tsx:28 + Canvas.tsx:275; nowheel appears nowhere in src`
- **[medium/M] Surface the bound dataset identity on the source card** — A bound source shows only a generic 'Change dataset' button and rows/cols/version; the dataset name lives only in the editable title, so renaming erases the on-card link, and the picker never marks the active row.  
  `web/src/nodes/kinds/source.tsx:32-34 (meta=stats), :41-47 (button), :67-88 (no active-state highlight)`
- **[medium/M] Give unconnected ports a hollow shape that morphs to '+' on hover** — Ports look identical wired or not and afford 'add' only by swapping the mouse cursor to copy; no hollow state, no + morph.  
  `web/src/nodes/Port.tsx:18-32 (shape from wire type, cursor-only); plus icon exists at web/src/ui/Icon.tsx:23`
- **[medium/S] Remove the duplicate Settings gear and the inert account avatar** — A standalone Settings gear duplicates the app-menu Settings, and AccountMenu is a static div with no handler (its own comment says 'nothing to switch').  
  `web/src/canvas/TopBar.tsx:68-71 gear duplicates :118; :180-190 AccountMenu static div`

### design
- **[high/L] Adopt headless primitives (Radix/Base UI); keep the custom look; reject MUI/AntD/shadcn** — Hand-rolled Popover/Tooltip/menus reimplement dismiss per use but lack focus trap, in-menu keyboard/typeahead nav, and collision-flip. Wrap Radix/Base UI behind current props APIs; Figma UI3 is a design language, not a package.  
  `web/src/ui/Popover.tsx:23-63 (manual dismiss, clamp-only :31); Tooltip.tsx (no aria); NodeCard.tsx:250 + TopBar.tsx:100-176 menus (no roving focus)`
- **[high/M] Build Button/IconButton/MenuItem primitives; tokenize radius/type/spacing; unify accent blue** — Strong token layer but no component layer: buttons reimplemented ~4 ways plus a copy-pasted menu-item pattern; radius scale bypassed 119 literals vs 26 token refs; two 'primary' blues.  
  `ActionIcon NodeCard.tsx:187, IconBtn TopBar.tsx:192, CodeBtn/Action Inspector.tsx:205/244; two blues TopBar.tsx:65 (#2f6ef0) vs tokens.ts:26 (#3b7fe0)`
- **[medium/M] Remove or finish the vestigial dark-mode plumbing** — PanelTitle takes a dark prop never passed true, DataPanel tags .dp-dark with no matching CSS rule, and --viewer-* vars are single-light with a 'the viewer is light' comment — the dark branches render light dead code.  
  `web/src/index.css:4-27 (single light :root); PanelHost.tsx:88 dark param called without it at :73; DataPanel.tsx:39 dp-dark, no rule`
- **[medium/M] Redesign the action shelf: fit-to-content width, one shared elevation** — The shelf spans full 232px width with a flex spacer (dead center = the 'dull' look) and carries its own border+shadow butting the card's — a double-line seam reading as a detached pill.  
  `web/src/nodes/NodeCard.tsx:150-159 (absolute top:100%, left:0 right:0, own border+shadow), :179 flex:1 spacer vs card radius 12 tokens.ts:77`
- **[medium/S] Stop showing run status/actions on annotation notes** — A note is stripped from the graph yet defaults to status 'draft' and the Inspector shows ○ draft, Run/View-data actions, and an empty Ports section.  
  `web/src/nodes/kinds/note.tsx:81 status:'draft'; Inspector.tsx:79-82 status glyph+label, :132-142 generic actions`

### code
- **[high/S] Gate the action shelf to single selection** — showShelf = selected || hover || busy renders one floating bar per box-selected card (the reported 'two bars stuck'); per-node Run/View-data is meaningless on a multi-selection.  
  `web/src/nodes/NodeCard.tsx:64 + :24 + :150; Canvas.tsx:133-138`
- **[high/S] Add nowheel to the note editor (and view-mode wrapper)** — With panOnScroll, wheel over the textarea pans the canvas; add nowheel plus maxHeight + overflow:auto so long notes scroll.  
  `web/src/nodes/kinds/note.tsx:28 vs Canvas.tsx:275; grep nowheel = 0`
- **[medium/M] Stop rewriting the whole node array to the store every drag frame** — onNodesChange calls setNodes(doc.nodes.map(...)) on every position change, mutating doc.nodes and re-running the reconcile effect that rebuilds ALL RF node objects per frame — O(n) per mousemove on a large canvas.  
  `web/src/canvas/Canvas.tsx:127-131 (setNodes on every position change), :92-112 reconcile deps [doc.nodes, selectedIds]`
- **[medium/S] Show the bound dataset identity separately from the editable title** — Picking a table calls rename(id, t.name) then shows only counts + a 'Change dataset' button; renaming the node erases the only on-card binding indicator.  
  `web/src/nodes/kinds/source.tsx:27-29 (rename=t.name), :32-34 (counts), :41-47 (button)`
- **[medium/M] Make ports reflect connection state and morph to + on hover** — Port fill is keyed off wire TYPE, never actual connection, and there is no hover state — the only add affordance is the copy cursor.  
  `web/src/nodes/Port.tsx:18-31 (fill by type, cursor:'copy'), :41 onClick only, no onMouseEnter`

### system-design
- **[high/L] Resolve 'one kernel per session' vs the shared multi-user server** — Docstring says one-kernel-per-session but code ships auth, shares, Postgres; Deps is a process-global singleton with one InMemoryCatalog, so all users share one catalog and run_index. This is the root of the global-lock and secret-leak findings.  
  `main.py:1-5 docstring; deps.py:198-208 _deps global; deps.py:89 single InMemoryCatalog; main.py:96-116 shared register; deps.py:105 global run_index`
- **[high/L] Stop holding the global DuckDB lock for a whole run** — LocalRunner acquires the single global RLock before its step loop and releases only in finally; one long run serializes every preview/sample/run across all users and pins sync threadpool workers.  
  `plugins/runner.py:128 acquire, :174 release; db.py:16 single RLock; executors/preview.py + main.py:132 also lock`
- **[high/M] Skip autosave on remote Y updates; add version-based concurrency** — A peer's Y update rebuilds the whole store doc and the guardless autosave subscriber PUTs it, so N editors cause N full-doc writes per edit; put_canvas never compares version.  
  `web/src/collab/ydoc.ts:85-93; store/graph.ts:857-874 (no applying guard); main.py:481-499 no conflict check`
- **[medium/M] Persist run state so in-flight runs survive a kernel restart** — Run status lives only in the runner's in-memory dict; after restart /run/{id} 404s, the client retries 6 times then marks the node stale, and a RunRecord is written only by the terminal hook, so an interrupted run vanishes unrecorded.  
  `plugins/runner.py:51 in-memory runs; main.py:338-343 404; store/graph.ts:914-931 gives up after 6; deps.py:66-71 persist only on terminal on_complete`
- **[medium/M] Break up the 702-line main.py monolith and its function-local imports** — One module owns catalog/preview/run/auth/users/canvas/collab/static with 15 in-function 'from kernel import ...' statements dodging import cycles.  
  `main.py 702 lines; 15 in-function imports e.g. :128, :233, :274`

### security
- **[high/M] Require auth on the execution and data routes** — /run, /run/preview, /catalog/register, /data/sample, /destinations/* have no current_user dependency; with DP_AUTH_SECRET set they stay fully open — unauthenticated POST /api/run yields file read + code exec + file write.  
  `main.py:95/122/204/238/316 no Depends; current_user wired only to canvas/settings/auth at :449-593; no dependencies= on APIRouter`
- **[high/S] Gate user creation and listing behind auth/admin** — POST /users creates a user with an attacker-chosen password and GET /users leaks all emails, both unauthenticated; /auth/login then succeeds against the just-set hash — full login bypass.  
  `main.py:438-446 create_user, :432-435 list_users no Depends; login verifies at :382-395, auth.py:73-83`
- **[high/M] Stop disclosing object-store secret keys via GET /settings; encrypt at rest** — accessKeyId/secretAccessKey are stored as plaintext JSON and GET /settings returns every global setting verbatim; PUT /settings lets any user write global scope.  
  `db.py:60-77; main.py:576-586 get_settings, :589-593 put_setting; metadb.py:348-354 plaintext`
- **[high/M] Restrict dataset/source URIs to stop arbitrary file read and SSRF** — catalog/register accepts any absolute path with no confinement and the adapter passes http(s) URIs to read_parquet while httpfs auto-loads — arbitrary local-file read and blind SSRF to internal endpoints.  
  `main.py:100-101 no root check; adapters.py:140-151; db.py:35-36 autoload=true`
- **[medium/M] Close the sandbox str.format dunder bypass; default untrusted to subprocess** — _reject_dunder inspects only Attribute/Name nodes, but 'format'/'str' are safe builtins, so a format-string reaches object.__subclasses__; the default LocalRunner executes in the kernel process.  
  `sandbox.py:66-74; _SAFE_BUILTINS includes format/str at :35-37; deps.py:95-104 defaults LocalRunner`

### extensibility
- **[high/M] Fix or delete plugins/base.py — the SPI 'contract' is dead code with wrong signatures** — base.py calls itself the extensibility contract the core 'depends only on', but it is imported nowhere; DatasetAdapter requires sample() (never called) while the real methods scan/write/fingerprint/matches are absent, and Runner.run has the wrong arity. A plugin author following it ships a broken adapter/runner.  
  `grep 'plugins.base' over kernel = 0 imports; grep '.sample(' over kernel = 0 calls; base.py:26-34, :86-96 vs engine.py:173 scan, backends.py:30 run(...,target_node_id,placement)`
- **[high/L] Collapse the double node-spec (backend nodespecs.py vs frontend kinds/*.tsx)** — Every built-in kind is defined twice; registerGenericNodes skips kinds with a hand-built card, so /api/nodes ports are ignored for the 15 built-ins. The copies already drift: the sql node's frontend accepts sql-view but the backend accepts only dataset+sample, so frontend canConnect and backend validation disagree.  
  `nodespecs.py:88 sql _in() default = dataset,sample vs web/src/nodes/kinds/sql.tsx:34 accepts dataset,sample,sql-view; generic.tsx:101 'a hand-built card wins'`
- **[medium/M] Wire up or drop the inert CapabilityProvider extension point** — reg.add_capability just appends; predicate/columns are never invoked (grep = 0), tagging uses hardcoded regexes, and the viewer tab is a separate bespoke frontend registration — so 'add a capability' means forking core plus writing bespoke React.  
  `deps.py:53-54 append-only; capabilities.py:35-44 regex; grep '.predicate(|.columns(' = 0; frontend tab web/src/nodes/capabilities.tsx:42-47`
- **[medium/M] Document and type the plugin node lowering contract** — add_node(spec, lower) is the main way to add a compute node, but lower is undocumented/untyped; the engine calls lower(engine, node, inputs) expecting a Relation or {port: Relation}, forcing authors to reverse-engineer LoweringEngine internals.  
  `deps.py:33-44 add_node; engine.py:182-185 node_lowerings[t](self, node, inputs); no Protocol in base.py`
- **[medium/M] Add SPI/plugin version negotiation — manifest version is display-only** — dataplay.toml version is stored and surfaced but never checked against any core API version (no version constant exists), so a stale plugin loads then crashes at runtime with no honest compat error.  
  `deps.py:158-173 validates only name/version presence; grep 'api_version|spec_version|min_core' = 0 relevant hits`

## 横切主题

- Hand-rolled UI primitives reimplement solved behavior buggily: Popover/Tooltip/menus lack focus trap, keyboard nav, and collision-flip, and the note-scroll + shelf bugs share this DIY root — a headless primitives layer plus Button/IconButton/MenuItem components would erase a whole class of these.
- Split source of truth is pervasive: node specs live in both backend nodespecs.py and frontend kinds/*.tsx (already drifting on sql), collab state lives in both the Zustand store and the Y.Doc with dual undo stacks, and the SPI contract in base.py contradicts the real interfaces — each pair invites silent divergence.
- A single-tenant core wears a multi-user costume: a process-global DuckDB connection, one shared InMemoryCatalog, and an unbounded global run_index are exposed behind auth, shares, and Postgres, producing the global-lock stall, the shared-catalog leak, and the secret-disclosure findings.
- Auth is applied by omission, not by design: it is attached route-by-route to canvas/settings only, so the highest-impact routes (/run, /data, /catalog, /users) are open by default — an allowlist boundary (router-level dependency, collab write allowlist) would flip the safe default.
- Selection and per-frame state are treated as free: selectedIds folds every marquee'd node so single-node chrome multiplies, and drag writes the whole node array to the store every mousemove — both are O(n) per interaction that a single-selection gate and drag-local state fix cheaply.
- Annotation notes are forced through the dataflow lifecycle: a note that is stripped from the kernel graph still carries a 'draft' run status and shows Run/ports chrome, and dead dark-mode plumbing renders light — vestigial state that reads as half-implemented to a maintainer who hates exactly that.