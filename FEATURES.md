# Data Playground — 功能清单

_验收评审清单，由对整个代码库的普查生成（每一项都由一个 `file:line` 作为证据支撑）。_
_最后更新：2026-07-06（新增：认证模式下把本地数据集路径限定到白名单 roots；Dockerfile + docker-compose 部署）。图例：✅ 已实现 · 🟡 部分实现（见备注） · ⬜ 未实现（仅有脚手架/规划中，或有意省略）。_

**142 项功能 —— ✅ 122 项已实现 · 🟡 17 项部分实现 · ⬜ 3 项未实现。**

| 领域 | ✅ | 🟡 | ⬜ |
|---|--:|--:|--:|
| 画布与节点交互 | 23 | 3 | 1 |
| 引擎与执行 | 16 | 4 | 0 |
| 数据、适配器与目录 | 26 | 4 | 0 |
| 协作、多用户与认证 | 21 | 1 | 0 |
| 可扩展性与插件 SPI | 14 | 2 | 0 |
| 平台、设计系统与运维 | 22 | 3 | 2 |

## ⚠️ 尚未完全完成（验收重点）

- ⬜ **branch / loop / variable 控制流节点**（画布与节点交互）—— 有意省略，非缺口：控制流统一由 'section' 容器的驱动脚本承担（section.tsx:102）—— branch 与两个 filter 冗余，loop/variable 都能在 section 脚本里表达。残留的 'control' 工具栏分类（Toolbar.tsx:12）没有任何节点注册，空分类会被丢弃（Toolbar.tsx:27），所以该分组不会显示。`web/src/theme/tokens.ts:54`
- 🟡 **Section 容器（嵌套 / 拖入拖出 / 驱动脚本）**（画布与节点交互）—— 渲染为一个有尺寸的框架；拖进去的节点通过屏幕空间的重叠命中测试成为 parentId 子节点（Canvas.tsx:179），也可以再拖出去解除关联。缺口：嵌套只支持一层 —— 把一个 section 拖到另一个 section 上会被明确拒绝（Canvas.tsx:180）。`web/src/nodes/kinds/section.tsx:93`
- 🟡 **能力驱动的查看器标签页**（画布与节点交互）—— media 能力会根据列能力添加一个图片网格标签页。只有 'media' 内置发布了；vectors 标签页已被有意移除（capabilities.tsx:49），所以注册机制是真实的，但内置集合很小。`web/src/nodes/capabilities.tsx:42`
- 🟡 **Agent dock（从意图构建流水线）**（画布与节点交互）—— 有 Plan/Build 两种模式；Build 会应用 LLM 生成的图（applyAgentGraph）并运行终端节点。设计上的缺口：需要在服务端配置 DP_AGENT_MODEL + 供应商 key —— 没有配置模型时它就是 'unavailable'（AgentDock.tsx:32），不存在基于规则的兜底。`web/src/panels/AgentDock.tsx:14`
- 🟡 **运行取消 + 查询中断**（引擎与执行）—— 在各步骤之间可取消，并能在运行自己的游标上中断正在执行的 DuckDB 查询。缺口（已在 db.py:163 说明）：transform 内部的纯 Python 死循环（例如 `while True`）在进程内无法被中断 —— 只有 subprocess runner 的强制 kill 才能停下它。`kernel/kernel/plugins/runner.py:268 cancel() sets Event + scope.interrupt(); kernel/kernel/db.py:75 _Scope.interrupt calls con.interrupt() from another thread`
- 🟡 **内容寻址缓存（未变更的 plan 直接走缓存）**（引擎与执行）—— 仅为进程内的内存 dict，上限为最近 100 条（_MAX_RUNS，runner.py:130）—— 不会在 kernel 重启后持久化，也不会在多个 web 实例/subprocess runner 之间共享（SubprocessRunner 没有缓存）。缓存的是行数 + 输出 uri/table，而非中间关系。`kernel/kernel/plugins/runner.py:76 _plan_hash (node config + source fingerprint + edges/handles); result stored/read at runner.py:141,180; write-skip for idempotent overwrite at runner.py:220`
- 🟡 **成本 / 确认门控**（引擎与执行）—— 门控只有一个固定的源行数阈值（5M）—— 没有字节大小/成本模型。大小未知时返回 needs_confirm=False（runner.py:69），所以一个无法计数的源会按设计绕过门控（理由：无法计数→无法读取→快速失败）。`kernel/kernel/plugins/runner.py:72 needs_confirm = rows >= _CONFIRM_ROWS (5,000,000); enforced at kernel/kernel/routers/runs.py:168 → HTTP 409 unless req.confirmed`
- 🟡 **独立的 loop 节点**（引擎与执行）—— 一个裸的 'loop' 节点在引擎里不做任何真正的迭代 —— 它是一个直通占位符。真正的迭代式控制流通过 `section`（maxRuns 驱动脚本）来实现。环路在一开始就会被拒绝（必须被封装，见 §5.7，engine.py 的 is_acyclic 检查）。`kernel/kernel/executors/engine.py:307 loop/opaque/write just pass through parent on full run and raise NotPreviewable otherwise; compiler maps it to kind='loop' (compiler.py:19)`
- 🟡 **DuckDB 适配器 —— Arrow/Feather/IPC 扫描**（数据、适配器与目录）—— 仅限本地文件；feather 通过 pyarrow 读入 DuckDB。feather 没有对象存储路径，也不支持追加（写入追加会拒绝它，adapters.py:188）。`kernel/kernel/plugins/adapters.py:148`
- 🟡 **写入模式追加（分片文件目录）**（数据、适配器与目录）—— 只支持 parquet/csv/tsv（以及 Lance 原生追加）；JSON/Arrow/Feather 会抛出 NotImplementedError。每次运行追加一个分片文件；读取时用 glob 把它们汇总回来（test_kernel.py:386）。`kernel/kernel/plugins/adapters.py:185`
- 🟡 **原子覆盖写（临时文件 + os.replace）**（数据、适配器与目录）—— 仅对本地写入是原子的 —— 失败/取消的写入不会截断已有数据集（test_kernel.py:438）。对象存储的覆盖写是就地写入（依赖单对象 PUT 的原子性；多文件/目录的对象存储覆盖写不是事务性的）。`kernel/kernel/plugins/adapters.py:203`
- 🟡 **目标位置 —— 对象存储 backend 浏览 + target_uri**（数据、适配器与目录）—— 通过 glob() 浏览某个前缀；对象存储的 mkdir 是空操作（没有真正的文件夹 —— 前缀在写入时才创建，destinations.py:150）。当凭证/桶缺失时，浏览错误会如实暴露出来。`kernel/kernel/destinations.py:61`
- 🟡 **协作 WebSocket 中继（按画布分房间）**（协作、多用户与认证）—— 仅为每实例的内存态。多实例/无状态 web 部署需要把每个画布 sticky routing 到单一实例（已在 main.py:11-13 的 docstring 中承认）；处于不同实例上的对等端不会互相看到对方。对默认的单进程部署来说没问题。`kernel/kernel/main.py:88-135 _collab_rooms is an in-memory dict[canvas_id -> set[WebSocket]]`
- 🟡 **Capabilities —— media/vector 列标注 + 查看器标签页**（可扩展性与插件 SPI）—— media/vector 检测是可用的（在 tag_columns 里用正则匹配 name/type），Media 查看器网格也能渲染。缺口：(1) reg.add_capability 只是向 KernelInfo 声明一个 id+label —— 插件无法添加新的列检测（tag_columns 是硬编码的核心逻辑），也无法添加查看器标签页（那是单独的前端注册），正如模块 docstring 自己承认的（capabilities.py:47-51）。(2) Vector 查看器标签页已被移除（capabilities.tsx:49-50）；vectors 只在单元格里获得一个内联的 chip。`kernel/kernel/plugins/capabilities.py:35-51, web/src/nodes/capabilities.tsx:42-50`
- 🟡 **流水线导入器 SPI（/pipelines/import）**（可扩展性与插件 SPI）—— 扩展点现在诚实了：deps.importer 默认是 NullImporter，所以 /pipelines/import 返回 501 未配置（此前是一个错误的 400），插件通过 reg.set_importer 注册一个真正的导入器。按设计不内置任何导入器（没有通用的流水线格式）。`kernel/kernel/deps.py:98 self.importer=NullImporter(); Registry.set_importer; routers/catalog.py:125 →501`
- 🟡 **shadcn/ui 迁移**（平台、设计系统与运维）—— Radix+cva 的 shadcn 原语已经存在，较新的面板也在使用它们，但迁移明确尚未完成 —— 仍有许多内联样式/遗留变量的组件。index.css:4 写着 'Preflight is disabled … mid-migration'，而 tailwind.config.js 写着 'Preflight ON' —— 一处陈旧、自相矛盾的注释。`web/src/components/ui/*.tsx (button,dialog,input,select,…); index.css:4 vs tailwind.config.js Preflight comment`
- ⬜ **Agent 离线关键词规划器兜底**（平台、设计系统与运维）—— 这是有意为之，不是缺陷：刻意不做任何基于规则、假装成 LLM 的替身；没有 DP_AGENT_MODEL 时 dock 显示 "unavailable"，其余一切照常工作。README 已修正以保持一致（此前曾错误地声称有离线规划器）。`web/src/panels/AgentDock.tsx:13,50-51; README.md:151-152`
- ⬜ **无状态 web 就绪 —— 分布式执行**（平台、设计系统与运维）—— 执行始终在接收请求的那个实例上运行。reconcile_orphaned_runs 假设只有单一执行实例 —— 启动时它会把所有非终态的运行标记为 failed，所以在多实例部署里一个实例的重启会取消掉另一个实例上正在进行的运行。需要按运行的实例归属 + 心跳 + 一个 ExecutionBackend 插件。已作为已知缺口记录在案。`kernel/kernel/metadb.py:401-419 (reconcile_orphaned_runs TODO); README.md:95-99`
- 🟡 **实时协作（按画布分房间）**（平台、设计系统与运维）—— 每个画布一个内存态的广播中继，带在线状态 + 角色门控（viewer 无法中继 yjs 编辑）。最后写入者获胜，并非无冲突（Yjs/CRDT 依赖已经引入，但备注为未来的加固项）。房间是进程本地的 → 部署时必须把 /ws/collab/{canvas_id} sticky-route 到单一实例（没有服务端协调）。`kernel/kernel/main.py:88-135`
- 🟡 **可插拔的执行 backend 选择**（平台、设计系统与运维）—— pick_runner 会尊重 'backend' 设置；内置发布了 LocalRunner + 一个真正的 SubprocessRunner。UI 的 runner 选择器可用，但 pod/Ray/队列 runner 只是文档中记载的插件扩展点，并未内置发布 —— 所以 '专用执行层' 目前只是脚手架。`kernel/kernel/deps.py:116-147; SettingsModal.tsx:95-104`

---

## 画布与节点交互

- ✅ **React Flow 节点画布** —— ReactFlow，带点状背景、panOnScroll、min/max zoom、fitView；本地 rfNodes 状态从 store 协调而来，以保留 RF 自己拥有的 measured/width，让节点保持可见。 `web/src/canvas/Canvas.tsx:268`
- ✅ **统一的节点卡片** —— 共享卡片：强调色条、状态字形、可编辑标题、类型标签、元信息行、紧凑主体，以及一个悬停/选中时出现的操作栏。所有手工构建的类型都通过它来渲染。 `web/src/nodes/NodeCard.tsx:79`
- ✅ **节点 + 能力注册表（插件模型）** —— register(spec,component)；index.ts 会即时 glob kinds/*.tsx，所以新增一个类型只是放进一个文件而已。buildNodeTypes 从注册表派生出 RF 的 nodeTypes。 `web/src/nodes/registry.ts:32`
- ✅ **schema 驱动的通用节点（backend/插件类型）** —— registerGenericNodes() 能在无前端代码的情况下渲染任何 /api/nodes 类型 —— 类型化端口、参数表单字段、必填参数校验 —— 除非已有手工构建的卡片。 `web/src/nodes/generic.tsx:97`
- ✅ **内置节点类型（source/sample/filter/select/transform/sql/join/aggregate/sort/dedup/write/metric/vector-search/section/note/code）** —— 16 个手工构建的类型文件各自调用 register()；backend 的 nodespecs.py 镜像了这些计算类型。'notebook' 在加载时会被迁移为 'transform'（graph.ts:239）。 `web/src/nodes/kinds/source.tsx:94`
- ⬜ **branch / loop / variable 控制流节点** —— 有意省略，非缺口：控制流统一由 'section' 容器的驱动脚本承担（section.tsx:102）—— branch 与两个 filter 冗余，loop/variable 都能在 section 脚本里表达。残留的 'control' 工具栏分类（Toolbar.tsx:12）没有任何节点注册，空分类会被丢弃（Toolbar.tsx:27），所以该分组不会显示。 `web/src/theme/tokens.ts:54`
- 🟡 **Section 容器（嵌套 / 拖入拖出 / 驱动脚本）** —— 渲染为一个有尺寸的框架；拖进去的节点通过屏幕空间的重叠命中测试成为 parentId 子节点（Canvas.tsx:179），也可以再拖出去解除关联。缺口：嵌套只支持一层 —— 把一个 section 拖到另一个 section 上会被明确拒绝（Canvas.tsx:180）。 `web/src/nodes/kinds/section.tsx:93`
- ✅ **类型化端口 + 连线类型** —— 端口的形状+色调编码了连线类型（dataset/selection/sample/sql-view/metric/value；tokens.ts:63）。未连接时是空心的，连上后填充，悬停会变大并显示 '+'。多输出节点通过 config.outputs 声明实例端口（registry.ts:54）。 `web/src/nodes/Port.tsx:13`
- ✅ **连接校验（类型化、单输入、join 双输入）** —— isValidConnection 检查源连线 ∈ 目标 port.accepts（registry.canConnect:89），并拒绝一个已被占用的输入 handle；join 的 a/b handle 允许两个不同的输入。 `web/src/canvas/Canvas.tsx:158`
- ✅ **从端口连出的添加节点菜单** —— 在输出端口上普通点击（Port.tsx:52 派发 dp-port-click；拖拽连接会抑制它）会打开一个菜单，过滤出首个输入能接受该连线的类型（registry.kindsAcceptingWire），然后把新节点连接好。 `web/src/canvas/ConnectMenu.tsx:7`
- ✅ **添加节点工具栏（按分类分组）** —— 底部悬浮工具栏从注册表自动填充，按分类分组并带 portal 弹层；通过 freePosition 放置得不重叠。空分类会被丢弃，所以 'Control flow' 永远不会出现（见 branch/loop）。 `web/src/canvas/Toolbar.tsx:19`
- ✅ **选择 + 框选 + shift/meta 多选** —— selectionOnDrag（左键拖拽框选）、panOnDrag=[1,2]（中键/右键平移）；选择变更被合并进 store.selectedIds（Canvas.tsx:144，graph.setSelection:472）。Cmd/Ctrl+A 全选。 `web/src/canvas/Canvas.tsx:288`
- ✅ **带最终位置提交的节点拖拽** —— 每帧的拖拽变更保持在本地（RF）；只有最终位置（dragging:false）才作为自己的一个撤销步骤提交到 store，避免每次 mousemove 都造成 O(n) 的文档重写和对协作 socket 的洪泛。 `web/src/canvas/Canvas.tsx:128`
- ✅ **复制 / 粘贴 / 剪切 / 复制副本（单个 + 子图）** —— cloneSubgraph 重映射 id、保留内部连线、偏移位置、把状态重置为 draft。注意：这是应用内的模块剪贴板（graph.ts:78），不是操作系统剪贴板 —— 在一个标签页内可用，但跨浏览器标签页/应用不行。 `web/src/store/graph.ts:476`
- ✅ **键盘快捷键** —— Delete/Backspace 删除选中项；B 旁路（尊重 canBypass）；D 禁用；Cmd/Ctrl+A/C/X/V/D；Cmd+Z / Shift+Z / Cmd+Y 撤销-重做；Esc 先关闭面板再清除选择。在输入框内以及全屏代码编辑器/弹窗之上会被抑制。 `web/src/canvas/Canvas.tsx:216`
- ✅ **旁路（直通）** —— 切换 data.bypassed（会清除 disabled）；卡片显示一条虚线强调色外框（NodeCard.tsx:100）。在 ⋯ 菜单和 B 快捷键里都由 spec.canBypass 门控。 `web/src/store/graph.ts:535`
- ✅ **禁用 + 下游传播** —— disable 切换 data.disabled；isDisabled（graph.ts:58）会向上游遍历，所以一个被禁用的节点会关闭它整条下游分支（变暗，被自身禁用的节点上有 DISABLED 徽章，运行/预览被阻止）。 `web/src/store/graph.ts:545`
- ✅ **内联节点重命名** —— 双击标题（或已选中时单击），或通过 dp-rename 事件用 ⋯ 菜单里的 Rename；在 Enter/失焦时提交，Esc 还原。 `web/src/nodes/NodeCard.tsx:207`
- ✅ **每节点的操作栏（查看数据 / 运行 / 历史 / 代码 / 更多）** —— 悬停 / 唯一选中 / 运行中时出现的悬浮操作栏：预览（眼睛图标）、运行/停止（非 source）、历史、编辑代码（仅 transform+sql）以及一个 ⋯ 菜单（重命名/运行详情/复制副本/旁路/禁用/导出/血缘/删除）。操作会带理由地禁用（未连接 source / 参数无效）。 `web/src/nodes/NodeCard.tsx:156`
- ✅ **节点状态字形（draft/latest/stale/running/failed）** —— 状态 token 驱动表头的字形+颜色（NodeCard.tsx:110）；编辑一个节点会把它及其下游标记为 stale（graph.updateConfig:389），一次完成的运行会翻转为 latest 并快照一个版本。 `web/src/theme/tokens.ts:74`
- 🟡 **能力驱动的查看器标签页** —— media 能力会根据列能力添加一个图片网格标签页。只有 'media' 内置发布了；vectors 标签页已被有意移除（capabilities.tsx:49），所以注册机制是真实的，但内置集合很小。 `web/src/nodes/capabilities.tsx:42`
- ✅ **schema 感知的列选择器字段** —— ColumnCombo + useInputColumns 从每节点的输出 schema（在结构变更时获取，graph.refreshSchemas:818）把类型化的列建议喂给各类型卡片（例如 join key、sort/dedup key）。 `web/src/nodes/fields.tsx:1`
- ✅ **空画布状态** —— 当 doc.nodes 为空时显示：'Add a source'（在视口中心放置一个 source）和 'Ask the Agent'。 `web/src/canvas/Canvas.tsx:41`
- ✅ **小地图 + 缩放控件** —— 控件堆叠在一个可平移的 MiniMap 之上（节点用类型强调色着色，点击可重新居中）；放置得不重叠。 `web/src/canvas/Canvas.tsx:296`
- 🟡 **Agent dock（从意图构建流水线）** —— 有 Plan/Build 两种模式；Build 会应用 LLM 生成的图（applyAgentGraph）并运行终端节点。设计上的缺口：需要在服务端配置 DP_AGENT_MODEL + 供应商 key —— 没有配置模型时它就是 'unavailable'（AgentDock.tsx:32），不存在基于规则的兜底。 `web/src/panels/AgentDock.tsx:14`
- ✅ **独立的连线删除 / 重连** —— 双击一条连线可将其移除（通过提交实现可撤销）；把连线端点拖到一个空闲端口可重新布线（onReconnect，校验时忽略被移动的那条边）。节点删除仍走应用层的 Delete 键处理逻辑。 `web/src/canvas/Canvas.tsx:293 onEdgeDoubleClick→removeEdge; :182 onReconnect`
- ✅ **画布上的实时在线状态 / 对等端光标** —— 画布在挂载时加入一个在线状态房间（Canvas.tsx:82），在 mousemove 时广播光标（sendCursor），并渲染对等端的实时光标；图编辑通过 Yjs CRDT 合并（collab）。 `web/src/canvas/PeerCursors.tsx:1`

## 引擎与执行

- ✅ **下降引擎（图 → DuckDB 关系 plan）** —— 每个关系型节点下降为一个 duckdb.DuckDBPyRelation；多输出节点下降为 {port->Relation}，由边的 source_handle 路由（engine.py:81）。 `kernel/kernel/executors/engine.py:150 (LoweringEngine._lower dispatches source/filter/select/sort/dedup/aggregate/sql/join/metric/branch per node type to lazy DuckDB relations)`
- ✅ **核外执行（DuckDB 流式 + 溢出到磁盘）** —— 关系型算子在 DuckDB 内部原生地流式处理/溢出；Python transform 溢出到 DP_SPILL_DIR 下的 Parquet 再读回。溢出文件由 runner 的 finally 做 GC（runner.py:192）。 `kernel/kernel/plugins/runner.py:142 (full=True LoweringEngine); engine.py:364 _transform_spill streams Python-transform output to temp Parquet with 50k-row flush`
- ✅ **忠实的样本预览（join/sort/vector 对完整输入运行）** —— 预览期间这些算子把它们的输入以未采样方式下降（full=True 的子引擎），这样预览的 LIMIT 就成为一个诚实的 top-N，而不是对 2000 行前缀说谎。预览扫描预算是 2000 行（preview.py:15）。 `kernel/kernel/executors/engine.py:118 _faithful_inputs; used by sort (engine.py:220), join (engine.py:261), vector-search (engine.py:423)`
- ✅ **NotPreviewable 的诚实性（P8）** —— 干净地把 '需要一次完整遍历' 和真正的错误区分开；NOT_PREVIEWABLE_KINDS 在 engine.py:26，node_previewable() 在 engine.py:553 会尊重插件的 spec.previewable 和 transform 模式。 `kernel/kernel/executors/engine.py:30 NotPreviewable + raises for aggregate/write/opaque/loop/section/transformed-inputs; preview.py:55 maps it to SampleResult(not_previewable=True) distinct from error`
- ✅ **本地核外 runner** —— 在一个后台守护线程上以进程内方式运行图；发出每节点的状态转换以供实时/DB 支撑的轮询使用。 `kernel/kernel/plugins/runner.py:34 LocalRunner; _execute runs plan steps on a per-run cursor, forces a count for non-sink targets (runner.py:174)`
- ✅ **隔离的 subprocess runner** —— 真正的操作系统进程隔离：崩溃/OOM/段错误都无法拖垮 kernel；取消是一个硬性 terminate()（subprocess_runner.py:165）。可通过 Settings 选为 backend 'local-subprocess'（deps.py:135 pick_runner）。 `kernel/kernel/subprocess_runner.py:28 SubprocessRunner spawns `python -m kernel.subrun`; kernel/kernel/subrun.py:39 child rebuilds Deps and runs the in-process LocalRunner, atomically writing status JSON`
- 🟡 **运行取消 + 查询中断** —— 在各步骤之间可取消，并能在运行自己的游标上中断正在执行的 DuckDB 查询。缺口（已在 db.py:163 说明）：transform 内部的纯 Python 死循环（例如 `while True`）在进程内无法被中断 —— 只有 subprocess runner 的强制 kill 才能停下它。 `kernel/kernel/plugins/runner.py:268 cancel() sets Event + scope.interrupt(); kernel/kernel/db.py:75 _Scope.interrupt calls con.interrupt() from another thread`
- ✅ **每运行的 DuckDB 游标隔离（db.run_scope）** —— 并发的运行/预览不再在一把全局锁上串行化，一个运行被中止的事务/视图清理也无法卡死另一个。被预览（preview.py:37）、runner（runner.py:148）、schema（schema.py:32）、catalog sample 使用。 `kernel/kernel/db.py:84 run_scope() gives each run/preview its own cursor via _base_conn().cursor(); thread-local con at db.py:61; scope exit rolls back + drops only its own views (db.py:106)`
- 🟡 **内容寻址缓存（未变更的 plan 直接走缓存）** —— 仅为进程内的内存 dict，上限为最近 100 条（_MAX_RUNS，runner.py:130）—— 不会在 kernel 重启后持久化，也不会在多个 web 实例/subprocess runner 之间共享（SubprocessRunner 没有缓存）。缓存的是行数 + 输出 uri/table，而非中间关系。 `kernel/kernel/plugins/runner.py:76 _plan_hash (node config + source fingerprint + edges/handles); result stored/read at runner.py:141,180; write-skip for idempotent overwrite at runner.py:220`
- 🟡 **成本 / 确认门控** —— 门控只有一个固定的源行数阈值（5M）—— 没有字节大小/成本模型。大小未知时返回 needs_confirm=False（runner.py:69），所以一个无法计数的源会按设计绕过门控（理由：无法计数→无法读取→快速失败）。 `kernel/kernel/plugins/runner.py:72 needs_confirm = rows >= _CONFIRM_ROWS (5,000,000); enforced at kernel/kernel/routers/runs.py:168 → HTTP 409 unless req.confirmed`
- ✅ **运行估算** —— 故意做得粗略而诚实 —— 不编造每算子的 ETA（已在 runner.py:63 说明）。当没有源可计数时报告 'size unknown'。 `kernel/kernel/plugins/runner.py:62 estimate() reports real source-row count + step count + placement; kernel/kernel/routers/runs.py:127 _row_estimate counts the largest feeding source`
- ✅ **Transform 逃生舱（在 Arrow RecordBatches 上运行 Python）** —— onError='skip' 会丢弃失败的行/批次（engine.py:479）；非可预览的模式（例如不在 PREVIEWABLE_MODES 里的 flat_map_generator）在样本上会拒绝。完整运行会溢出到 Parquet。 `kernel/kernel/executors/engine.py:331 _transform; modes map/filter/flat_map/map_batches applied in _apply_fn (engine.py:472); library-processor path at engine.py:333`
- ✅ **用户单元格代码的软沙箱** —— 明确是一个软性防护，而非安全边界（docstring sandbox.py:1）—— CPython 的 exec 无法隔离；组织级部署需要操作系统层面的隔离。它会阻止 __class__/__subclasses__ 逃逸以及包含 '__' 的字符串字面量。 `kernel/kernel/sandbox.py:82 compile_operator with builtins whitelist (sandbox.py:29), import allow-list (sandbox.py:16), AST dunder rejection (sandbox.py:58), wall-clock timeout (sandbox.py:109)`
- ✅ **向量搜索（Lance 原生 ANN + 暴力余弦兜底）** —— 查询向量来自显式配置或某个选中的行；即使在预览时也在完整输入上运行（忠实）。只有当输入是一个中间没有算子的裸 Lance source 时才走原生 ANN（engine.py:459）。 `kernel/kernel/executors/engine.py:415 _vector_search; native path via adapter.nearest for a bare .lance source (engine.py:447), else list_cosine_similarity brute-force scan (engine.py:454)`
- ✅ **Section 元编程（驱动脚本复合节点）** —— 不可做样本预览（只做完整遍历，engine.py:187）。每次迭代的结果物化到 Parquet，并通过共享的溢出列表做 GC。与 transform 相同的软沙箱 —— 一个从不调用 run() 的脚本（例如 `while True`）不受时间限制（section.py docstring）。 `kernel/kernel/section.py:75 run_section — a Python driver script calls contained nodes by alias with run()/value()/concat()/emit(), maxRuns cap (section.py:87), multi-output ports`
- ✅ **每节点输出 schema（类型化 vs 未类型化端口）** —— 为编辑器的列建议提供支撑。在它自己的游标上运行，没有超时（超时会遗弃一个持锁的 worker）。 `kernel/kernel/executors/schema.py:20 schema_for_graph — metadata-only (schema_only=True, sources scan limit=0) resolves relational columns; code ops in _UNTYPED (schema.py:17) return null`
- 🟡 **独立的 loop 节点** —— 一个裸的 'loop' 节点在引擎里不做任何真正的迭代 —— 它是一个直通占位符。真正的迭代式控制流通过 `section`（maxRuns 驱动脚本）来实现。环路在一开始就会被拒绝（必须被封装，见 §5.7，engine.py 的 is_acyclic 检查）。 `kernel/kernel/executors/engine.py:307 loop/opaque/write just pass through parent on full run and raise NotPreviewable otherwise; compiler maps it to kind='loop' (compiler.py:19)`
- ✅ **分支路由（条件式边路由）** —— 空谓词把所有内容送到 true，`1=0`（空）送到 false。 `kernel/kernel/executors/engine.py:322 _route_branch filters the input by predicate for the true port and NOT(predicate) for the false port; applied per outgoing edge in _inputs (engine.py:113)`
- ✅ **SSRF 安全的对象存储扩展策略** —— 防止一个任意的 https:// uri 静默地拉取 httpfs 并去获取它；s3://gs:// 仍然能用，因为凭证/httpfs 是被有意加载的。与引擎的源扫描相关的安全项。 `kernel/kernel/db.py:41 _apply_session disables autoinstall/autoload of extensions on base conn AND every per-run cursor (db.py:92); httpfs loaded only explicitly in ensure_object_store (db.py:124)`
- ✅ **实时状态 + 运行历史持久化（跨实例 / 重启安全）** —— 让另一个无状态 web 实例或一个重启后的 kernel 能回答一个它没启动过的运行的状态轮询，而不是返回 404。subprocess 子进程会禁用自己的 on_complete，好让父进程只记录一次（subrun.py:34）。 `runner.py:110 _emit fires on_status (DB run_states) each transition; runner.py:203/subprocess_runner.py:148 on_complete persists terminal run; routers/runs.py:184 _status_or_lost falls back to metadb.get_run_state then a synthetic terminal status`

## 数据、适配器与目录

- ✅ **DuckDB 适配器 —— Parquet 扫描（惰性/核外）** —— 默认读取器；也能处理 parquet 分片的对象存储前缀（adapters.py:139）。 `kernel/kernel/plugins/adapters.py:151`
- ✅ **DuckDB 适配器 —— CSV/TSV 扫描** `kernel/kernel/plugins/adapters.py:144`
- ✅ **DuckDB 适配器 —— JSON/NDJSON 扫描** `kernel/kernel/plugins/adapters.py:146`
- 🟡 **DuckDB 适配器 —— Arrow/Feather/IPC 扫描** —— 仅限本地文件；feather 通过 pyarrow 读入 DuckDB。feather 没有对象存储路径，也不支持追加（写入追加会拒绝它，adapters.py:188）。 `kernel/kernel/plugins/adapters.py:148`
- ✅ **文件目录扫描（part-*.<ext> 数据集）** —— 扫描一个由追加模式写入的目录；通过递归 glob 覆盖 parquet/pq/csv/tsv/json。 `kernel/kernel/plugins/adapters.py:153`
- ✅ **CSV 解析选项（分隔符 / 表头覆盖；否则自动检测）** —— source 节点只在设置了时才传递覆盖值（engine.py:164）；分隔符接受 'tab'/'\t'。 `kernel/kernel/plugins/adapters.py:61`
- ✅ **Lance 流式扫描，带列/limit 下推** —— 需要可选的 `lance` extra（在本环境未安装；测试跳过）。列/limit 下推到 Lance scanner；谓词在扫描后于 DuckDB 中应用，不下推到 Lance。 `kernel/kernel/plugins/adapters.py:246`
- ✅ **对象存储扫描/写入（s3:// gs:// gcs:// r2://）经由 DuckDB httpfs** —— httpfs 在 db.ensure_object_store 里显式加载（db.py:124）。对象存储测试在 mock 桶下通过（test_kernel.py:909）。 `kernel/kernel/plugins/adapters.py:130`
- ✅ **对象存储凭证（显式 key / AWS 链 / MinIO/R2 的自定义 endpoint）** —— 从 `objectStore` 设置 CREATE SECRET；回退到 credential_chain。对 S3 兼容主机处理 Endpoint/USE_SSL/URL_STYLE。 `kernel/kernel/db.py:138`
- ✅ **目录 list / get / search** —— 内存态、由 RLock 串行化；搜索匹配 name 或 uri 子串。 `kernel/kernel/plugins/catalog.py:101`
- ✅ **目录血缘（围绕某个 uri 的连通分量图）** —— 在去重后的父/子边上做 BFS；遍历前会合并来自其他实例的边。 `kernel/kernel/plugins/catalog.py:122`
- ✅ **启动时从本地数据目录播种目录** —— 发现 parquet/csv/tsv/json/arrow 文件和 .lance 目录。 `kernel/kernel/plugins/catalog.py:39`
- ✅ **目录注册（用户添加一个数据集，跨重启持久化）** —— 通过 adapter.schema 校验可读性；保存到 `datasets` 全局设置，以便重启后重新注册（test_kernel.py:1387）。 `kernel/kernel/routers/catalog.py:75`
- ✅ **register_output（write 节点注册输出 + 血缘边）** —— runner 提交写入后，用父 uri 和 pipeline='canvas' 注册输出。 `kernel/kernel/plugins/runner.py:253`
- ✅ **目录 DB 支撑 / 跨实例（catalog_entries + catalog_edges）** —— 在 _persist/_add_edge 时写穿；通过 _load_from_db 在读取时合并，所以另一个无状态实例注册的数据集会变得可见（test_kernel.py:1149）。尽力而为（DB 错误被吞掉）；每次 list/get/lineage 都会从 DB 做一次完整的重新合并 —— 没有增量同步，在大型目录下是一个扩展性问题。 `kernel/kernel/plugins/catalog.py:82`
- ✅ **写入格式 parquet / csv / tsv / json / arrow / lance** —— 按扩展名选择格式；JSON 经由 COPY ...（FORMAT JSON, ARRAY true）；Lance 经由流式 record batch。往返测试在 test_kernel.py:405。 `kernel/kernel/plugins/adapters.py:207`
- ✅ **写入模式覆盖** `kernel/kernel/plugins/adapters.py:199`
- 🟡 **写入模式追加（分片文件目录）** —— 只支持 parquet/csv/tsv（以及 Lance 原生追加）；JSON/Arrow/Feather 会抛出 NotImplementedError。每次运行追加一个分片文件；读取时用 glob 把它们汇总回来（test_kernel.py:386）。 `kernel/kernel/plugins/adapters.py:185`
- 🟡 **原子覆盖写（临时文件 + os.replace）** —— 仅对本地写入是原子的 —— 失败/取消的写入不会截断已有数据集（test_kernel.py:438）。对象存储的覆盖写是就地写入（依赖单对象 PUT 的原子性；多文件/目录的对象存储覆盖写不是事务性的）。 `kernel/kernel/plugins/adapters.py:203`
- ✅ **内容寻址的写入跳过（幂等覆盖重跑）** —— 当一个完全相同的覆盖 plan 已经产生过输出且它仍然存在时，跳过重写；追加从不缓存。 `kernel/kernel/plugins/runner.py:218`
- ✅ **目标位置 —— 本地 backend 浏览 + mkdir（受 root 限定）** —— realpath 检查阻止越出目标 root 的遍历；.lance 目录以文件形式显示。 `kernel/kernel/destinations.py:27`
- 🟡 **目标位置 —— 对象存储 backend 浏览 + target_uri** —— 通过 glob() 浏览某个前缀；对象存储的 mkdir 是空操作（没有真正的文件夹 —— 前缀在写入时才创建，destinations.py:150）。当凭证/桶缺失时，浏览错误会如实暴露出来。 `kernel/kernel/destinations.py:61`
- ✅ **目标预设（全局设置 + 默认 workspace 输出）** —— 始终注入默认的 'outputs' 位置；DP_STORAGE_URL 可以让它成为一个 s3/gs 前缀。 `kernel/kernel/destinations.py:110`
- ✅ **向量搜索 —— Lance 原生 ANN（如有向量索引则使用）** —— 仅当输入是一个裸 .lance source 时；需要 `lance` extra；出现任何错误时回退到暴力余弦。暴露 _score = 1 - 余弦距离（adapters.py:257）。 `kernel/kernel/executors/engine.py:447`
- ✅ **向量搜索 —— 在 DuckDB 上暴力余弦** —— list_cosine_similarity ORDER BY DESC LIMIT k；需要一个固定大小的数值 list/array 列。即使在预览时也在完整（未采样）输入上运行以获得忠实的 top-K。 `kernel/kernel/executors/engine.py:453`
- ✅ **向量搜索 —— 按外部向量或按行索引查询** —— queryVector 接受 JSON 字符串或 list；否则使用 queryRow 在数据集里的偏移。 `kernel/kernel/executors/engine.py:428`
- ✅ **SSRF 防护 —— 禁用 DuckDB 扩展的 autoload/autoinstall** —— 阻止一个任意的 https:// uri 静默地拉取 httpfs 并获取远程数据；在每个每运行游标上重新断言。对象存储访问会显式加载 httpfs。 `kernel/kernel/db.py:42`
- ✅ **CORS 限制为 localhost 来源** —— Origin 正则只允许 localhost/127.0.0.1。 `kernel/kernel/main.py:38`
- ✅ **本地路径的数据集 URI 限定** —— 认证模式下，本地数据集路径（/catalog/register、/data 采样、以及引擎的 source 下降）必须落在允许的 root 内（workspace、data_dir 或 DP_DATASET_ROOTS）—— 越界抛 PermissionError（catalog 入口返回 403）。对象存储 URI（s3:// gs:// 等）不受影响。开放单用户模式（受信任）不做限定。 `kernel/kernel/paths.py:24 ensure_local_uri_allowed；接到 routers/catalog.py（register+sample）与 executors/engine.py（source 下降）；test_kernel.py test_local_dataset_path_confined_in_auth_mode`
- ✅ **mem:// 适配器（内存态命名表）** —— 用于把进程内的 DuckDB 表暴露为数据集（主要用于测试/fixture）。 `kernel/kernel/plugins/adapters.py:128`

## 协作、多用户与认证

- ✅ **实时协同编辑（Yjs CRDT）** —— 合并是真正的 CRDT（nodes/edges/meta 映射），而非最后写入者获胜。服务端是一个笨的中继，没有服务端 Y.Doc：收敛依赖于对等端之间的 ysync 握手。'房间里的第一个' 由一个 800ms 计时器决定（ydoc.ts hydrateIfEmpty），在对等端加入缓慢时这个启发式可能出现竞态。 `web/src/collab/ydoc.ts:50-113 (store<->Y.Doc bidirectional bridge, per-field node diffing so a drag never clobbers a config edit); kernel/kernel/tests/test_kernel.py:1164 test_collab_relay_gates_viewer_doc_updates passes`
- ✅ **在线状态 / 对等端光标** —— 光标颜色按浏览器会话随机（collab.ts:9-10），不与用户身份绑定。 `web/src/collab/collab.ts:92-96 sendCursor throttled to 50ms; web/src/canvas/PeerCursors.tsx maps flow-coords to each viewer's screen via viewport; kernel test_collab_relay_broadcasts_and_leave (test_kernel.py:1054)`
- 🟡 **协作 WebSocket 中继（按画布分房间）** —— 仅为每实例的内存态。多实例/无状态 web 部署需要把每个画布 sticky routing 到单一实例（已在 main.py:11-13 的 docstring 中承认）；处于不同实例上的对等端不会互相看到对方。对默认的单进程部署来说没问题。 `kernel/kernel/main.py:88-135 _collab_rooms is an in-memory dict[canvas_id -> set[WebSocket]]`
- ✅ **协作通道上的 viewer 角色写入门控** —— 封堵了这样一条洗白路径：一个 editor 对等端本可以把某个 viewer 的编辑合并+自动保存越过只读边界。 `kernel/kernel/main.py:115-116 drops inbound 'yjs' doc updates from a viewer while still relaying its presence; test_collab_relay_gates_viewer_doc_updates (test_kernel.py:1164) confirms yjs dropped, presence + editor edits relayed`
- ✅ **协作 WebSocket 认证门控** —— 在开放模式下（没有 DP_AUTH_SECRET），ws 不设门控，所有人都可以编辑 —— 这是为单用户/受信任实例设计的。 `kernel/kernel/main.py:97-103 verifies dp_session cookie + canvas_role, closes 1008 if no role; test_collab_ws_requires_auth_when_enabled (test_kernel.py:1065)`
- ✅ **运行状态 WebSocket 认证门控** `kernel/kernel/main.py:69-71 closes 1008 without a valid session; test_run_ws_requires_auth_when_enabled (test_kernel.py:1079)`
- ✅ **按用户的身份（scrypt 密码 + 签名的会话 cookie）** —— 密码以 scrypt$salt$hash 存储（仅用标准库）。DP_AUTH_PASSWORD 只用于引导（首次初始化时播种默认用户的 hash，metadb.py:186-190）。cookie 是 httponly+samesite=lax；Secure 标志通过 DP_AUTH_SECURE_COOKIE 选择开启（对内部 http 安装默认关闭）。除了 samesite=lax 之外没有 CSRF token。 `kernel/kernel/auth.py:66-83 salted scrypt hash + constant-time verify; auth.py:38-60 HMAC-signed time-limited token (7-day TTL); routers/workspace.py:31-43 /auth/login verifies THIS user's own hash; test_per_user_password_is_not_a_skeleton_key (test_kernel.py:673) and test_signed_session_auth (test_kernel.py:1345)`
- ✅ **自助改密/轮换密码** `kernel/kernel/routers/workspace.py:46-56 /auth/password requires old password if one is set, min 6 chars; web/src/views/Shell.tsx:72-119 change-password dialog; rotation asserted in test_kernel.py:688-694`
- ✅ **X-DP-User 开放（免认证）开发模式** —— 按设计它会信任任意 header 值 = 那个用户，不做任何验证；只对单用户/受信任部署安全。当认证启用时，该 header 明确不被信任（security.py:21-25，由 test_kernel.py:1364 断言）。 `kernel/kernel/security.py:21-26 current_user falls back to X-DP-User header when DP_AUTH_SECRET unset; metadb.resolve_user defaults to seeded 'local' user (metadb.py:208-216)`
- ✅ **安全默认的 /api 认证门控（路由级 Depends）** —— 门控在 include 时施加，所以新加的路由默认受保护，除非明确挂到 public_router 上。 `kernel/kernel/main.py:50-54 public_router mounted WITHOUT gate, catalog/runs/workspace routers mounted WITH Depends(current_user); test_api_routes_require_auth_when_enabled (test_kernel.py:697) proves /run, /data, POST /users, /catalog, /canvas, /settings all 401 unauth while /auth/status + GET /users stay public`
- ✅ **登录界面 + 登录名单** —— 用户选择器 + 密码。没有 SSO/OIDC（auth.py:12 注明它 'would slot in later' —— 未实现）。 `web/src/views/Login.tsx:10-21 login form; web/src/App.tsx:38 renders Login when authEnabled && !userId; public GET /users returns id+name only, no emails (workspace.py:81-84, asserted test_kernel.py:719)`
- ✅ **管理员创建用户** —— 按设计不做自助注册；不存在角色/管理员的区分 —— 任何已认证用户都能创建用户（POST /users 只由 current_user 门控，没有管理员检查）。 `kernel/kernel/routers/workspace.py:87-94 POST /users (gated — no anonymous self-registration), sets scrypt hash if password provided`
- ✅ **画布分享 —— 可见性（private/workspace/workspace_view）+ 显式协作者（editor/viewer）** —— 三档可见性：private、workspace（人人可编辑）、workspace_view（人人只读，除非另有显式 editor 分享）。canvas_role 对 workspace_view 返回 viewer，而 put_canvas 本就对非 editor/owner 返回 403，所以只读是自动强制的。ShareModal 提供第三档；add_share 校验可见性值（未知→400）。 `kernel/kernel/metadb.py canvas_role + list_canvases_for; routers/workspace.py add_share 校验; web/src/panels/ShareModal.tsx; test_workspace_view_visibility_is_read_only`
- ✅ **画布访问控制 / 授权（owner/editor/viewer 角色）** —— 前端在保存返回 403 时显示一个 'view-only' 提示 + accessDenied 状态，而不是假装同步（graph.ts:887-891）。 `metadb.canvas_role returns owner\|editor\|viewer\|None (metadb.py:240-251); put_canvas 403s non-editors (workspace.py:129-147); delete is owner-only (workspace.py:180-184); editor cannot re-share (workspace.py:199, asserted test_kernel.py:1385)`
- ✅ **自动保存（防抖 PUT）+ 离线缓存** `web/src/store/graph.ts:859-898 400ms-debounced saveCanvas PUT + unconditional localStorage cache; distinguishes 403 (view-only) from offline (graph.ts:886-895)`
- ✅ **协作写放大防护** —— 防止 N 个协同编辑者在每次按键时各自 PUT 整个文档（N 倍放大）。离线缓存的写入仍然是无条件的，这样重新加载后仍保留已合并的对等端编辑。 `web/src/collab/undo.ts:23 collabApply.remote flag; graph.ts:870-873 skips PUT for a peer's merged edit (only the originating editor PUTs); local edits + undo/redo still PUT`
- ✅ **标签页关闭时的 pagehide 刷新** —— 封堵了这样一个协作边界情况：唯一的发起编辑者在 400ms 防抖窗口内断开连接。 `web/src/store/graph.ts:906-911 pagehide listener PUTs with keepalive only when saved===false`
- ✅ **画布版本历史 + 恢复** —— 恢复本身也可撤销。自动保存约每 400ms 触发一次，但快照器节流到 90s，所以历史按设计是粗粒度的。 `kernel/kernel/metadb.py:422-461 snapshot_canvas (throttled 90s, dedup, prune to newest 30; named snapshots always kept) + list_versions + get_version_doc; workspace.py:150-177 versions/restore endpoints (restore snapshots pre-restore state as 'before restore'); web/src/panels/VersionHistoryModal.tsx; test_canvas_version_history_and_restore (test_kernel.py:737)`
- ✅ **运行历史（按画布，持久化）** —— 对临时/未保存画布的运行是空操作（metadb.py:310-314）。 `kernel/kernel/metadb.py:306-340 record_run + list_runs (RunRecord table, survives restart); workspace.py:216-221 GET /canvas/{id}/runs with same authz; web/src/panels/RunHistoryModal.tsx`
- ✅ **CRDT 感知的协作式撤销/重做** —— 撤销只回退你自己的更改，且不会删除一个对等端并发添加的节点（旧的全文档快照式撤销会）。 `web/src/collab/ydoc.ts:99-104 Y.UndoManager tracks only origin 'store' (local edits); web/src/collab/undo.ts crdtUndo indirection; store falls back to its own snapshot stack when offline (ydoc.ts:134)`
- ✅ **设置 API 里的密文脱敏** —— 严格来说不是协作/认证特性，而是已认证 workspace 面的一部分；密文永远不会以明文离开 kernel。 `kernel/kernel/routers/workspace.py:226-262 GET redacts agentApiKey + objectStore access keys to __redacted__ sentinel; PUT treats the sentinel as 'keep stored' so dots never overwrite a real secret`
- ✅ **CORS 限制为 localhost** —— 假设是本地/同源部署；一个真正的、位于某域名之后的远程 HTTPS 部署需要放宽它。 `kernel/kernel/main.py:41-45 allow_origin_regex localhost/127.0.0.1 only (no wildcard), so a random site the user visits can't read the local API cross-origin`

## 可扩展性与插件 SPI

- ✅ **插件发现 —— 拖入式 workspace 文件夹** —— 扫描 <workspace>/plugins/<pack>/ 里带 register(reg) 的包，把目录加入 sys.path，先读取 dataplay.toml。由 test_plugin_version_negotiation（test_kernel.py:1237）覆盖。 `kernel/kernel/deps.py:150-161`
- ✅ **插件发现 —— pip entry-points（dataplay.plugins 组）** —— 加载 entry_points(group='dataplay.plugins') 并调用 ep.load()(reg)。是真实路径但没有测试覆盖，且 min_core_api 协商没有应用到 entry-points（只应用到拖入式的包）。 `kernel/kernel/deps.py:165-174`
- ✅ **插件发现 —— DP_PLUGINS 环境变量模块列表** —— 逗号分隔的模块名被导入，并通过 _register_module 调用 register(reg)。 `kernel/kernel/settings.py:20, kernel/kernel/deps.py:163-164`
- ✅ **节点编写 SDK —— add_node + ctx 构建器（sql/arrow_map/polars）** —— ctx.sql 使用一个 {input} 占位视图；arrow_map/polars 是惰性的 relation->relation 辅助函数。add_node 拒绝遮蔽一个内置或已注册的插件类型（deps.py:44-49）。 `kernel/kernel/sdk.py:38-70, kernel/kernel/deps.py:39-52`
- ✅ **NodeLowering 协议（插件下降契约）** —— runtime_checkable 的 Protocol；引擎通过 deps.node_lowerings 分派插件类型。单输出返回 Relation，多输出返回 {port_id: Relation}。 `kernel/kernel/backends.py:23-36`
- ✅ **NodeSpec 通用前端渲染（插件节点无需前端代码）** —— 插件节点用来自 /api/nodes 的类型化端口/参数进行通用渲染，包括 code 参数：GenericNode 现在给每个 type==='code' 的参数渲染一个片段预览按钮，打开那唯一的全屏编辑器（与手工卡片一致，按 param 的 lang 选 python/sql）。已用一个声明了 python code 参数的示例插件端到端验证（/api/nodes 携带 lang，registerGenericNodes 接住它）。 `web/src/nodes/generic.tsx GenericNode + NodeParamFields`
- ✅ **前后端节点 spec 一致性守卫** —— 测试会解析每个 web/src/nodes/kinds/*.tsx 的 register({...}) 字面量，并把 ports/wire/accepts 与 BUILTIN_NODE_SPECS 比对，为拥有手工卡片的类型守护漂移。通用渲染的类型会被跳过（按设计）。 `kernel/kernel/tests/test_kernel.py:1263-1334`
- ✅ **数据集适配器 SPI（add_adapter；DuckDB + Lance 内置）** —— DuckDBAdapter（parquet/csv/json/arrow/dir/对象存储）+ LanceAdapter。完整实现了 matches/scan/schema/count/fingerprint/write 契约。插件用 insert(0) 抢在默认之前认领 URI；resolve_adapter 回退到 DuckDBAdapter。 `kernel/kernel/plugins/adapters.py:99-291, kernel/kernel/deps.py:54-55,126-133`
- ✅ **Lance 原生 ANN（可选的 adapter.nearest）** —— LanceAdapter.nearest 把余弦 kNN 下推到 Lance（索引或平铺扫描），暴露 _score=1-distance。当适配器缺少 nearest 时，通用的 vector-search 节点回退到暴力余弦。 `kernel/kernel/plugins/adapters.py:257-264`
- ✅ **ExecutionBackend SPI（add_runner；替代 runner）** —— runtime_checkable 的 Protocol（name/can_run/estimate/run/status/cancel）。内置发布了两个真实 backend：LocalRunner + SubprocessRunner（subprocess_runner.py:28-160）。pick_runner 先按名字尊重 Settings->Execution 的 'backend' 选择，再取第一个 can_run；run_index 把 status/cancel 路由到所属的 runner（上限 1000）。 `kernel/kernel/backends.py:39-57, kernel/kernel/deps.py:57-59,135-147, kernel/kernel/routers/runs.py:165-181`
- 🟡 **Capabilities —— media/vector 列标注 + 查看器标签页** —— media/vector 检测是可用的（在 tag_columns 里用正则匹配 name/type），Media 查看器网格也能渲染。缺口：(1) reg.add_capability 只是向 KernelInfo 声明一个 id+label —— 插件无法添加新的列检测（tag_columns 是硬编码的核心逻辑），也无法添加查看器标签页（那是单独的前端注册），正如模块 docstring 自己承认的（capabilities.py:47-51）。(2) Vector 查看器标签页已被移除（capabilities.tsx:49-50）；vectors 只在单元格里获得一个内联的 chip。 `kernel/kernel/plugins/capabilities.py:35-51, web/src/nodes/capabilities.tsx:42-50`
- ✅ **处理器库 + 提升到库** —— 注册表按设计初始为空。promote() 从一个临时单元格注册一个由代码支撑的处理器，版本号自动递增；插件注册由 fn_factory 支撑的处理器。POST /processors/promote + GET /processors 已接好。build() 用与临时代码相同的沙箱编译代码。 `kernel/kernel/plugins/processors.py:62-97, kernel/kernel/routers/catalog.py:150-157`
- ✅ **目录提供者 SPI（set_catalog；默认 InMemoryCatalog）** —— Registry.set_catalog 可替换提供者。默认的 InMemoryCatalog 是一个每实例缓存，带写穿 + 从共享 metadb（catalog_entries/edges）加载，让无状态 web 实例保持一致；通过 RLock 线程安全；随着 write 节点注册输出而增长血缘。 `kernel/kernel/plugins/catalog.py:21-177, kernel/kernel/deps.py:67-68,105`
- ✅ **插件版本协商（min_core_api vs CORE_API_VERSION）** —— _read_manifest 校验 dataplay.toml（name/version 必填，min_core_api 可选）。一个要求更新核心的包会被记为错误且不加载；缺失清单则以无版本方式加载；格式错误的清单会被阻止。注意：只对拖入式的包强制执行 —— entry-point 和 DP_PLUGINS 模块完全绕过这项检查。 `kernel/kernel/deps.py:30,176-201, kernel/kernel/tests/test_kernel.py:1237-1260`
- 🟡 **流水线导入器 SPI（/pipelines/import）** —— 扩展点现在诚实了：deps.importer 默认是 NullImporter，所以 /pipelines/import 返回 501 未配置（此前是一个错误的 400），插件通过 reg.set_importer 注册一个真正的导入器。按设计不内置任何导入器（没有通用的流水线格式）。 `kernel/kernel/deps.py:98 self.importer=NullImporter(); Registry.set_importer; routers/catalog.py:125 →501`
- ✅ **插件自省端点（/api/plugins, /api/kernel）** —— GET /api/plugins 列出已加载的包及其 source/version/error；KernelInfo 报告 adapters/runners/processors/capabilities。加载失败会被捕获（name+error+traceback 尾部）而不是让启动崩溃（deps.py:210-214）。 `kernel/kernel/routers/catalog.py:46-48, kernel/kernel/deps.py:216-223`

## 平台、设计系统与运维

- ✅ **dataplay 一条命令启动器 / CLI** —— Argparse CLI：--host/--port/--workspace/--data-dir/--no-open/--no-seed；播种样本数据、在构建 deps 前配置 workspace、运行 uvicorn、打开浏览器。 `kernel/kernel/cli.py:15-44; pyproject.toml [project.scripts] dataplay="kernel.cli:main"`
- ✅ **FastAPI 应用工厂 + 路由拆分** —— 路由拆分到 catalog/runs/workspace 各 router；一个单独的 public_router（认证状态/登录/登出/名单）挂载时不设门控，其余一切都带 Depends(current_user)。 `kernel/kernel/main.py:37-54; routers/{catalog,runs,workspace}.py`
- ✅ **所有 /api 路由上的安全默认认证门控** —— 整个 /api router 在 include 时设门控；新路由默认受保护，除非明确加进 public_router。修复了此前 /run,/data,/catalog 门户大开的时代。 `kernel/kernel/main.py:50-54; kernel/kernel/security.py:16-26`
- ✅ **SPA 服务（单进程，打包 + 开发回退）** —— 如果存在则服务 kernel/_web（wheel 包），否则服务 web/dist。在 pyproject 的 wheel target 里强制包含 ../web/dist。没有 SPA 历史回退路由，但 StaticFiles(html=True) 覆盖了 index。 `kernel/kernel/main.py:145-149`
- ✅ **CORS 限制为 localhost 来源** —— allow_origin_regex 只有 localhost/127.0.0.1（有意不用通配符）。同源 SPA + Vite 代理；一个位于负载均衡器之后的跨域部署需要放宽它。 `kernel/kernel/main.py:41-45`
- ✅ **按用户的密码认证（scrypt + 签名会话 cookie）** —— HMAC 签名、周级 TTL 的会话 token；按用户加盐的 scrypt hash；DP_AUTH_PASSWORD 只引导默认用户；登录界面、登出、改密 UI 全部具备。通过 DP_AUTH_SECRET 选择开启；否则是开放的 X-DP-User 开发模式。 `kernel/kernel/auth.py:38-83; routers/workspace.py:31-56; web/src/views/Login.tsx; web/src/views/Shell.tsx:72-119`
- ✅ **设置 API 里的密文脱敏** —— GET 把 agentApiKey + objectStore.{accessKeyId,secretAccessKey} 脱敏为一个哨兵值；PUT 把哨兵值当作 '未改动'，这样点点点永远不会覆盖一个已存的密文。 `kernel/kernel/routers/workspace.py:226-262`
- ✅ **全局设置页面（agent / execution / destinations）** —— 全屏设置，带左侧导航。覆盖 agent 的 model/key/baseURL、execution 的 runner 选择器、destinations + 对象存储凭证。 `web/src/panels/SettingsModal.tsx:23-154; routers/workspace.py:238-262`
- ✅ **用户级设置** —— DB/API 一直支持 scope='user'；Settings UI 现在也接住了：Execution → Runner 是一个 per-user 偏好，pick_runner 先取用户选择、再回退到 workspace 默认（空=继承），run/estimate 会带上当前用户。主题仍走 localStorage（首帧前生效、无闪烁）。 `web/src/panels/SettingsModal.tsx（u 状态 + INHERIT 哨兵）; kernel/kernel/deps.py pick_runner(plan, uid); routers/runs.py; test_user_scoped_settings_are_isolated_per_user + test_user_scoped_backend_preference_wins_over_global`
- ✅ **元数据 DB（SQLAlchemy，开发用 SQLite / 生产用 Postgres）** —— 用户、画布、分享、运行记录/状态、版本、目录条目/边、设置。唯一可配置的是连接字符串（DP_DATABASE_URL）。 `kernel/kernel/metadb.py:39-154; settings.py:17-18; pyproject.toml postgres extra`
- ✅ **Alembic 迁移（含遗留 DB 收编）** —— 7 个 revision，每个都带 upgrade+downgrade；init_db 对 Alembic 之前的 DB 打上 0001_baseline 印记，然后升级到 head。Alembic 是 schema 的唯一真相源（不是 create_all）。 `kernel/kernel/metadb.py:167-191; migrations/versions/0001..0007`
- ✅ **亮/暗主题切换（跟随系统，无闪烁）** —— light/dark/system 三种模式，localStorage 持久化，在 <html> 上打上 data-theme，system 模式下跟随操作系统，在首次绘制前应用。切换在 TopBar 里。 `web/src/theme/mode.ts:9-43; web/src/main.tsx:7; web/src/canvas/TopBar.tsx:205; tailwind.config.js darkMode:['class','[data-theme="dark"]']`
- ✅ **设计 token（shadcn HSL 变量 + TS 镜像）** —— shadcn 原语 + token + 亮/暗主题已经到位；迁移有意尚未完成 —— 仍有一些内联样式/遗留变量的组件（它们读取主题感知的 token 层）。（那处陈旧的 "Preflight disabled" 注释现已修正。） `web/src/index.css:11-107; web/src/theme/tokens.ts:8-99; tailwind.config.js colors map`
- 🟡 **shadcn/ui 迁移** —— Radix+cva 的 shadcn 原语已经存在，较新的面板也在使用它们，但迁移明确尚未完成 —— 仍有许多内联样式/遗留变量的组件。index.css:4 写着 'Preflight is disabled … mid-migration'，而 tailwind.config.js 写着 'Preflight ON' —— 一处陈旧、自相矛盾的注释。 `web/src/components/ui/*.tsx (button,dialog,input,select,…); index.css:4 vs tailwind.config.js Preflight comment`
- ✅ **LLM agent —— 供应商无关、进程内、服务端持有 key** —— 在一个工作中的图上运行 Pydantic AI 的 tool-use 循环（add/connect/set_config/preview 节点）；模型通过 DP_AGENT_MODEL 或 DB 设置选择；key 留在 kernel 内；请求上限约束循环；新节点自动布局。注意 agent.py 的头部与 README 矛盾：它进程内使用 Pydantic AI，而 README/设置仍描述为一个 'LiteLLM tool-use 循环'（LiteLLM 只用于 key 检测）。 `kernel/kernel/agent.py:99-257; routers/runs.py:110-124`
- ⬜ **Agent 离线关键词规划器兜底** —— 这是有意为之，不是缺陷：刻意不做任何基于规则、假装成 LLM 的替身；没有 DP_AGENT_MODEL 时 dock 显示 "unavailable"，其余一切照常工作。README 已修正以保持一致（此前曾错误地声称有离线规划器）。 `web/src/panels/AgentDock.tsx:13,50-51; README.md:151-152`
- ✅ **无状态 web 就绪 —— 共享运行状态** —— RunState 表镜像每一次状态转换（on_status 钩子）；GET /run/{id} + 状态 WS 可从任意实例回答，并在重启后存续。 `kernel/kernel/metadb.py:97-108,343-363; deps.py:78-83,115`
- ✅ **无状态 web 就绪 —— 共享目录写穿** —— catalog_entries/catalog_edges 持久化已注册的数据集、已写入的输出以及血缘；内存态目录是一个每实例缓存，会写穿 + 从 DB 加载。 `kernel/kernel/metadb.py:110-129,366-398; deps.py:104-110; migration 0007`
- ⬜ **无状态 web 就绪 —— 分布式执行** —— 执行始终在接收请求的那个实例上运行。reconcile_orphaned_runs 假设只有单一执行实例 —— 启动时它会把所有非终态的运行标记为 failed，所以在多实例部署里一个实例的重启会取消掉另一个实例上正在进行的运行。需要按运行的实例归属 + 心跳 + 一个 ExecutionBackend 插件。已作为已知缺口记录在案。 `kernel/kernel/metadb.py:401-419 (reconcile_orphaned_runs TODO); README.md:95-99`
- 🟡 **实时协作（按画布分房间）** —— 每个画布一个内存态的广播中继，带在线状态 + 角色门控（viewer 无法中继 yjs 编辑）。最后写入者获胜，并非无冲突（Yjs/CRDT 依赖已经引入，但备注为未来的加固项）。房间是进程本地的 → 部署时必须把 /ws/collab/{canvas_id} sticky-route 到单一实例（没有服务端协调）。 `kernel/kernel/main.py:88-135`
- ✅ **画布版本快照 + 恢复** —— 节流的自动快照（去重，90s，保留 30 份）+ 保存时的命名快照；恢复端点会先快照当前状态，所以恢复本身也可撤销。 `kernel/kernel/metadb.py:84-94,422-461; routers/workspace.py:150-177`
- ✅ **启动时协调孤儿运行** —— 被一个已停止的 kernel 留在 queued/running 的运行会被标记为 failed('interrupted')，这样客户端不会永远轮询。对单实例是正确的；对多实例不安全（见分布式执行缺口）。 `kernel/kernel/metadb.py:191,401-419`
- ✅ **离线 / 零配置运行** —— 无需云账号/外部服务；内置 SQLite；首次运行播种样本数据；引擎依赖（DuckDB/Polars/Arrow，可选 Lance）全部本地。Agent + Postgres + Lance 是可选的 extra。 `README.md:8-9,26; kernel/kernel/settings.py:13-18; pyproject.toml core deps`
- ✅ **用户管理（创建用户）** —— Settings → Members 列出成员并创建成员（姓名 + 可选初始密码，走已有的 POST /users，之后刷新名单，分享选择器也能看到）。密码轮换仍是自助（账户菜单）—— 没有 admin 角色，所以不提供管理员重置端点（否则任何用户都能劫持账号）。 `web/src/panels/SettingsModal.tsx（Members）; api.createUser; store.refreshUsers; e2e 'settings Members creates a user'`
- ✅ **部署文档 + Docker** —— README 覆盖无状态 web 模型、DP_DATABASE_URL→Postgres、对象存储与协作的 nginx 一致性哈希示例；新增 Dockerfile（单镜像：Vite 构建 SPA → uv 同步内核并强制包含 SPA，已端到端冒烟测试 /api/health + 引擎 warm + SPA）与 docker-compose（Postgres + 卷 + 认证/数据集 root 环境变量，并说明 deploy.replicas + sticky routing）。TLS/反向代理留给运维方（README 已注明）。 `Dockerfile; docker-compose.yml; .dockerignore; README.md 'Run with Docker'`
- ✅ **插件发现与 SPI（nodes/adapters/runners/capabilities/catalog）** —— 两条发现路径（拖入式 <workspace>/plugins/<pack>/ + pip entry points）；清单（dataplay.toml）校验带 min_core_api 门控；拒绝遮蔽内置或已注册的类型。错误被记录（经由 GET /plugins 暴露）而不是让启动崩溃。 `kernel/kernel/deps.py:33-215; kernel/README.md`
- 🟡 **可插拔的执行 backend 选择** —— pick_runner 会尊重 'backend' 设置；内置发布了 LocalRunner + 一个真正的 SubprocessRunner。UI 的 runner 选择器可用，但 pod/Ray/队列 runner 只是文档中记载的插件扩展点，并未内置发布 —— 所以 '专用执行层' 目前只是脚手架。 `kernel/kernel/deps.py:116-147; SettingsModal.tsx:95-104`

