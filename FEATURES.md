# Data Playground — 功能清单（架构树）

_验收清单，按架构层次组织：**层 → 组件 → 角色**（接口 / 实现 / 选择判断 / 生命周期 / UI / 安全 / 持久化…）。每个叶子由 `file:line` 佐证。图例：✅ 已实现 · 🟡 部分实现（见备注） · ⬜ 未实现（脚手架 / 规划中 / 有意省略）。跨层复用的组件只写一处，别处用 `↗ 见 §X` 交叉引用（交叉引用行不带状态图标、不计数）。_

_最后更新：2026-07-08。**177 项功能**（含验收批次：chart 节点、按名字解析 source、目录缺失检测/注销、DuckDB 沙箱、admin 角色、弱密钥拒绝、跨站 WS 门、启动护栏；Jupyter 式 per-canvas 执行 kernel——**现为默认执行模型**：run/preview/profile 全走 kernel、扛住 hub 重启 + 重连、预览热缓存；`kernel/` 包已改名 `hub/`；跨机底座可插拔（`KernelSpawner` SPI + 参考 PodSpawner，`DP_KERNEL_SPAWNER=pod`）；以及编辑器体验批次：每画布 pip 依赖、编辑器内联运行结果、面板全屏、Monaco 列名补全喂输入 schema；以及上传数据集：拖到画布/节点/Tables 上传 → 落进共享存储 + 注册进跨实例目录；以及一次完整验收：修 32 个确认缺陷——周期性 reaper、warm cache 死锁/失效、cancel/restart 韧性、PodSpawner 幂等/fencing 等；以及集成就绪度硬化：Adapter/Catalog 提为正式 Protocol、KernelSpawner/Storage 点分路径可插拔、add_capability 检测钩子、PlaceableBackend 放置接缝，并配三个真参考插件——SQL/Postgres 目录、Hugging Face datasets 适配器、Apache Iceberg 适配器，全部走公开接缝而非特权核心路径；以及三个更深的就绪项：谓词/投影下推到 adapter.scan、importer 现返回可运行画布图（+ dp_json_pipeline 参考导入器 + import→canvas→run 往返测试）、引擎中立执行 IR + dp_ray 参考后端（第一个非 DuckDB 引擎从 IR 跑画布的 clean 子集，算子逐字节一致）；以及最后两块接缝：destination 上 Registry（reg.add_destination + dp_datasets_place）、声明式插件配置 dataplay.toml [[config]] + reg.config + Settings → Plugins UI 表单、capability 声明式查看器标签页（viewer.kind → 通用渲染器 + dp_json_view）、IR 统一（引擎共用 resolve_config 单一解析器 + 插件节点 ir 钩子可跑分布式 + dp_upper）；以及 code 算子的 schema 契约：transform/插件可声明或从样本推断输出 schema（`config.outputSchema`），引擎 schema_only 注入 typed 空 relation 替身让其 typing 并**向下游传播**，Inspector 端口显示列结构（可展开）+ 契约编辑器；以及非强制 schema 告警：节点引用了上游已知输入里不存在的列时卡片/连线琥珀提示（只在输入已知+引用可靠时报，避免误报），加契约漂移提示；以及"调度大脑"三件套：per-node 规模估计（保守+诚实+吃实测值，卡片"~N 行"提示）、成本感知放置（阻塞 region 工作集超本地内存预算→路由到更大后端，只注册本地时无操作、手动 mem pin 最高优先级）、分层物化（region handoff 落到生产者+消费者都够得着的最省 tier，本地→本地走本地、涉及远程走 S3）；以及 transform 可选批表示（行字典/pandas/pyarrow，arrow-native 保类型，引擎+IR+dp_ray 一致）+ 运行计划预览（`/graph/plan` → Inspector "Run plan"，放置真切分/路由时才显示）—— ✅ 163 · 🟡 12 · ⬜ 2。_

**层次总览**

| §  | 层 | 说明 |
|----|----|----|
| §1 | 前端 · 画布与交互 | React Flow 画布、节点卡片、端口连线、Section 容器、Agent dock |
| §2 | 内核 · 执行引擎 | 构建引擎、预览、执行器（接口/实现/选择/生命周期）、计算节点 |
| §3 | 数据 · 适配器与目录 | 适配器、写入、目录、目标位置、向量搜索、数据面安全、关系与 join 提示 |
| §4 | 协作 · 多用户与认证 | 实时协作、身份认证、授权分享、WS 门控、持久化 |
| §5 | 扩展点 · 插件 SPI | 发现/版本、节点/适配器/执行器/目录 SPI、能力、导入器 |
| §6 | 平台 · 运维与部署 | 应用服务、设置、元数据、设计系统、Agent、无状态 web/部署 |

## ⚠️ 尚未完全完成（12 🟡 + 2 ⬜，验收重点）

- ⬜ 控制流节点 branch/loop/variable（§1.3，有意省略）· Agent 离线规划器（§6.5，有意省略）
- 🟡 能力查看器标签页（§1.2）· Agent dock 需配模型（§1.6）· 运行取消纯 Python 循环（§2.3）· 成本门控（§2.3）· 独立 loop 节点（§2.4）· Arrow/Feather 扫描（§3.1）· 原子覆盖写-对象存储（§3.2）· 对象存储浏览（§3.4）· 实时协作 sticky-routing（§4.1）· pod/Ray 内置插件（§2.3，参考 pool 后端已内置）· 分布式/解耦执行（§6.6，per-canvas kernel 已解耦 web 层，参考 pod 底座已内置 `DP_KERNEL_SPAWNER=pod`、本地 kind e2e 验证过 `deploy/`，生产需接对象存储/镜像仓库）· shadcn 迁移（§6.4）

---

## §1 前端 · 画布与交互

### §1.1 画布骨架 · 3✅
- ✅ **React Flow 节点画布** — 点状背景、panOnScroll、min/max zoom、fitView；本地 rfNodes 从 store 协调，保留 RF 的 measured/width。 `web/src/canvas/Canvas.tsx:317`
- ✅ **空画布状态** — doc.nodes 为空时提示 'Add a source' + 'Ask the Agent'。 `web/src/canvas/Canvas.tsx:41`
- ✅ **小地图 + 缩放控件** — 控件叠在可平移 MiniMap 上，节点按类型色着色、点击重居中。 `web/src/canvas/Canvas.tsx:350`

### §1.2 节点卡片与渲染 · 9✅ 1🟡
- ✅ **统一的节点卡片** — 强调色条/状态字形/可编辑标题/类型标签/元信息/紧凑主体/悬停操作栏；所有手工类型走它。 `web/src/nodes/NodeCard.tsx:79`
- ✅ **节点状态字形（draft/latest/stale/running/failed）** — 编辑标记下游 stale，完成翻 latest 并快照。 `web/src/theme/tokens.ts:74`
- ✅ **内联节点重命名** — 双击标题 / ⋯ 菜单 Rename，Enter 提交 Esc 还原。 `web/src/nodes/NodeCard.tsx:207`
- ✅ **每节点操作栏（预览/运行/历史/代码/⋯）** — 悬停/唯一选中/运行中出现；操作带理由禁用。 `web/src/nodes/NodeCard.tsx:156`
- ✅ **schema 感知的列选择器字段** — ColumnCombo + useInputColumns 从输出 schema 喂类型化列建议。 `web/src/nodes/fields.tsx:19`
- ✅ **单一全屏代码编辑器 + 内联运行结果** — Monaco 单入口（节点卡片 / Inspector / 画布代码块都开它）；可运行节点右侧内嵌 DataPanel，编辑器里直接 Preview 当前 input 看结果，不用离开去看别处。 `web/src/panels/CodeFullscreen.tsx:36,66`
- ✅ **Monaco 列名补全（喂当前节点的输入 schema）** — sql / transform 单元格补全它实际收到的列（useInputColumns），schema 未解析时回退到预览过的列；补全 provider 对 sql 与 python 都注册。 `web/src/panels/CodeFullscreen.tsx:37; web/src/monaco-setup.ts:45`
- ✅ **面板 maximize / 全屏切换** — 数据 / 运行 / 历史 / 血缘 / section 面板一键放大到全屏覆盖层（同内容），再点还原；与代码编辑器全屏一致。 `web/src/panels/PanelHost.tsx:40,58`
- ✅ **数据表渲染（表头列类型 / struct·map·list·null 单元格 / 行详情 / Stats 标签页）** — RowsTable 列类型表头；struct/list 渲染 JSON、map 也走 JSON（不再是误导的 [N] 徽章）、null 占位、媒体缩略图、向量 chip；RowDetail 行详情；Stats 标签页来自 /run/profile。 `web/src/panels/DataPanel.tsx`
- 🟡 **能力驱动的查看器标签页** — media 能力加图片网格标签页；只有 media 内置，vectors 标签页已有意移除（注册机制真实但内置集小）。 `↗ 详见 §5.6 Capabilities` `web/src/nodes/capabilities.tsx:42`

### §1.3 节点注册与类型 · 3✅ 1⬜
- ✅ **节点 + 能力注册表（插件模型）** — register(spec,component)；glob kinds/*.tsx，加类型=加文件。 `web/src/nodes/registry.ts:32`
- ✅ **schema 驱动的通用节点（backend/插件类型）** — registerGenericNodes 无前端代码渲染任何 /api/nodes 类型。 `↗ 详见 §5.2 通用渲染` `web/src/nodes/generic.tsx:97`
- ✅ **内置节点类型（source/sample/filter/select/transform/sql/join/aggregate/sort/dedup/write/metric/vector-search/section/note/code）** — 16 个手工卡片，nodespecs.py 镜像计算类型。 `web/src/nodes/kinds/source.tsx:94`
- ⬜ **branch / loop / variable 控制流节点** — 有意省略、非缺口：控制流统一由 section 驱动脚本承担。branch 连引擎里残留的 `_route_branch` 路由机器件也已一并移除(不可达死代码);残留的 'control' 工具栏分类无节点注册,空分类被丢弃不显示。 `web/src/theme/tokens.ts:54`

### §1.4 端口与连线 · 6✅
- ✅ **类型化端口 + 连线类型** — 形状+色调编码 dataset/selection/sample/sql-view/metric/value；多输出经 config.outputs。 `web/src/nodes/Port.tsx:13`
- ✅ **连接校验（类型化、单输入、join 双输入）** — isValidConnection 检查 accepts + 拒绝已占用 handle。 `web/src/canvas/Canvas.tsx:187`
- ✅ **从端口连出的添加节点菜单** — 输出端口点击开菜单，过滤能接受该连线的类型并接好。 `web/src/canvas/ConnectMenu.tsx:7`
- ✅ **独立的连线删除 / 重连** — 双击删边（可撤销）；端点拖到空闲端口重连（onReconnect）。 `web/src/canvas/Canvas.tsx:326,209`
- ✅ **Inspector 端口列结构 + schema 契约编辑器** — 输入/输出端口显示 `N cols` 徽章（点开列出每列 name:type）；输入端口 schema 按 targetHandle 路由到上游输出。code 算子（transform/插件/vector-search）多一个"Output schema (contract)"区块：手填列或"Infer from sample"（跑一次有界 preview 自动填充 `config.outputSchema`），契约喂给后端 §2.2 的 typed 端口 + 下游传播；gate 按 kind（代码算子 + 插件），relational/io/annotation 节点不显示。契约还带**漂移提示**：pin 时记录 cell 的 hash，之后改了 cell 就提示契约可能过时（重推或编辑重固定）。 `web/src/panels/Inspector.tsx PortRow/SchemaContract/canDeclareSchemaKind; web/src/nodes/schema.ts codeHash`
- ✅ **非强制 schema 告警（列引用检查 + 连线提示）** — 一个不阻断的类型检查：节点 config 引用了上游**已知**输入里不存在的列时，卡片/Inspector 显示琥珀 `⚠ unknown column: X`、下游连线变琥珀虚线。**只在输入 schema 完全已知且引用可靠提取时才报**（select 纯列表/sort/dedup/groupBy 精确；filter 谓词 best-effort，跳过函数/限定符/lambda/关键字/类型名/日期部件/字符串字面量；join/sql/aggs/transform 不查），disabled/bypassed 节点静默——拿不准就不报，避免误报。 `web/src/nodes/schema.ts schemaWarnings/exprColumns; web/src/nodes/fields.tsx useSchemaWarnings; web/src/wires/WireEdge.tsx`

### §1.5 选择与画布编辑 · 8✅
- ✅ **添加节点工具栏（按分类分组）** — 底部悬浮，从注册表自动填充，portal 弹层，freePosition 不重叠；空分类丢弃。 `web/src/canvas/Toolbar.tsx:19`
- ✅ **选择 + 框选 + shift/meta 多选** — selectionOnDrag + panOnDrag[1,2]；Cmd/Ctrl+A 全选。 `web/src/canvas/Canvas.tsx:288`
- ✅ **带最终位置提交的节点拖拽** — 每帧本地，仅最终位置（dragging:false）作为一个撤销步提交，避免洪泛。 `web/src/canvas/Canvas.tsx:128`
- ✅ **复制/粘贴/剪切/复制副本（单个 + 子图）** — cloneSubgraph 重映射 id、保留内部连线、偏移位置；应用内剪贴板非 OS。 `web/src/store/graph.ts:476`
- ✅ **键盘快捷键** — Delete / B 旁路 / D 禁用 / Cmd A C X V D / Z 重做 / Esc；输入框与全屏编辑器上抑制。 `web/src/canvas/Canvas.tsx:216`
- ✅ **旁路（直通）** — 切 data.bypassed，虚线外框，spec.canBypass 门控。 `web/src/store/graph.ts:535`
- ✅ **禁用 + 下游传播** — isDisabled 向上游遍历，关闭整条下游分支（变暗、DISABLED 徽章、阻止运行）。 `web/src/store/graph.ts:545`
- ✅ **拖文件到画布上传 + 一键上传入口** — 从系统拖 Parquet/CSV/JSON/Arrow 到画布空白处 → 上传并落一个绑好数据的 source 节点（drop 覆盖层提示）；source 节点弹层与 Tables 视图也各有 Upload 按钮；kernel 离线时 toast 拒绝、不静默。 `web/src/canvas/Canvas.tsx onDropFiles; web/src/nodes/kinds/source.tsx onUpload; web/src/views/Shell.tsx; web/src/store/graph.ts uploadDataset`

### §1.6 复合与协作（前端侧）· 1✅ 1🟡
- ✅ **Section 容器（嵌套 / 拖入拖出 / 驱动脚本）** — 有尺寸框架，屏幕空间重叠命中成 parentId 子节点；视觉拖拽嵌套刻意单层（框固定尺寸），更深嵌套用驱动脚本。 `↗ 执行见 §2.4 Section 元编程` `web/src/nodes/kinds/section.tsx`
- 🟡 **Agent dock（一次对话，模型自己决定答还是建）** — 无 Plan/Build 模式；模型每条消息自行决定纯文字答 or 调改图工具（add/connect/set_config），前端仅在真改图时应用+跑终端；需配 DP_AGENT_MODEL，否则 unavailable。 `↗ 详见 §6.5 LLM agent` `web/src/panels/AgentDock.tsx`
- **画布光标 / 在线状态** — 在画布上渲染对等端光标 + 在线状态（PeerCursors 映射到各视口）。 `↗ 见 §4.1 实时协作`

---

## §2 内核 · 执行引擎

### §2.1 构建引擎（BuildEngine）· 2✅
- **接口：NodeBuilder 协议** — 引擎经 node_builders 分派插件类型（单输出 Relation / 多输出 {port:Relation}）。 `↗ 契约叶子见 §5.2 节点 SPI`
- ✅ **图 → DuckDB 关系 plan** — 每关系节点降为 DuckDBPyRelation；多输出 {port->Relation} 按 source_handle 路由。 `kernel/hub/executors/engine.py:150`
- ✅ **核外执行（DuckDB 流式 + 溢出磁盘）** — 关系算子原生流式/溢出；temp_directory 显式设为 DP_SPILL_DIR（运维可控）、DP_MEMORY_LIMIT 可限内存；比内存大的数据集在有限内存上限下排序而非 OOM；Python transform 溢出 Parquet 再读回，runner GC。 `kernel/hub/db.py _apply_session; engine.py:364`

### §2.2 预览与 schema · 5✅
- ✅ **忠实的样本预览（join/sort/vector 对完整输入运行）** — 这些算子预览时以未采样子引擎构建，LIMIT 成诚实 top-N；预览预算 2000 行。 `kernel/hub/executors/engine.py:118`
- ✅ **NotPreviewable 的诚实性** — 干净区分"需完整遍历"与真错误；尊重 spec.previewable + transform 模式。 `kernel/hub/executors/engine.py:30; preview.py:55`
- ✅ **每节点输出 schema（类型化 vs 未类型化端口）** — 元数据 only 解析关系列（源以 limit=0 扫），code 算子返回 null；自己的游标、无超时。 `kernel/hub/executors/schema.py:20`
- ✅ **code 算子的 schema 契约（声明/推断 + 下游传播）** — transform/插件/vector-search 默认 untyped，但可带 `config.outputSchema` 契约：schema_only 模式下引擎注入一个 typed 空 relation 替身（`_stand_in`，标识符安全转义），让该节点及**下游** relational 节点无需运行代码即可 typing；disabled 时 untyped、bypassed 时按透传（契约不适用）。契约由用户手填或"从样本推断"（跑一次有界 preview 取列）填充，二者共存、留空则保持动态。 `kernel/hub/executors/schema.py declared_schema 分支; kernel/hub/executors/engine.py declared_schema/_stand_in`
- ✅ **per-node 输出规模估计 + 尺寸提示** — `hub/estimate.py` 自底向上、放置无关地估每个节点的输出行数/字节：**保守**（减不了的算子如 filter/dedup 保留输入行数=上界，绝不低估）、**诚实**（aggregate/join/sql/code 输出未知→rows=None，不编数字）、**吃实测值**（跑过的/已物化的用真实行数覆盖估计）。`POST /graph/estimate` 暴露给前端，卡片显示"~N 行"提示（仅当能可靠估计；unknown 不显示；有真实 lastRun 就让位）。喂 §2.3 的成本感知放置。 `kernel/hub/estimate.py; kernel/hub/routers/runs.py graph_estimate; web/src/nodes/NodeCard.tsx`

### §2.3 执行器（ExecutionBackend）· 14✅ 3🟡 · `↗ per-user 选择见 §6.2`
**接口** — `↗ ExecutionBackend 协议契约叶子见 §5.4 执行器 SPI`

**实现**
- ✅ **LocalRunner（本地核外）** — 后台守护线程、进程内运行，发每节点状态转换。 `kernel/hub/plugins/runner.py:34`
- ✅ **SubprocessRunner（进程隔离）** — 真 OS 进程隔离，崩溃/OOM 不拖垮 kernel，取消=硬 terminate()；**auth 模式下默认走它**（多用户隔离）。 `kernel/hub/subprocess_runner.py:28`
- ✅ **KernelBackend（每画布常驻 kernel，**默认执行模型**）** — 每 canvas 一个脱离 hub 的常驻 kernel 进程（`hub.kernel`，token 鉴权 loopback 命令通道 /run·/preview·/profile·/cancel·/shutdown），**hub 可重启/重部署而不打断在飞 run**（run 在脱离的 kernel 上跑完，重开画布经 active-runs 重连）；DB 租约（kernels 表，migration 0011）原子单 spawner + `kernel_id` fencing 防脑裂，kernel 自己写 run_states（单写者），心跳门控 reaper 只在 owning kernel 真死才判失败。**run + preview + profile 都走 kernel**；预览有每 kernel 的**热中间关系缓存**（`plan_hash` 键 + preview scope 隔离 + 行上限物化：超上限不缓存→不 OOM；任意编辑即失效——键含 config + bypass/disable/rename），重开画布/重启 kernel(Settings→Execution)清空。pod 底座（P3）参考实现已内置（`DP_KERNEL_SPAWNER=pod`），并在本地 kind 集群 e2e 验证过（spawn→run 完成→restart 拆除 Pod，见 `deploy/verify-pod-substrate.sh`；ready 超时可配 `DP_KERNEL_READY_TIMEOUT_S`，首连重试跨 Service 注册延迟）。 `↗ 部署见 §6.6` `kernel/hub/kernel.py; kernel_backend.py; relation_cache.py; plan_key.py; deploy/`
- ✅ **每画布 pip 依赖（notebook 式）** — canvas 声明 `requirements`（随画布走、存 Graph），kernel `pip install --target` 到哈希路径（幂等缓存，失败也缓存+打日志不静默重试）+ 沙箱进程级放行（`_KERNEL_ALLOWED`，一 kernel = 一 canvas；放行的是 --target 全量 = 声明依赖 + 其传递闭包；每次运行按当前 requirements 重算，移除即收回）。**信任说明**：安装 = 任意代码 + 出网；锁定部署 `DP_CANVAS_PIP_DEPS=0` 关掉（装啥都不允许，改用预烤镜像）。 `kernel/hub/kernel_deps.py; sandbox.py set_allowed; web/src/panels/CanvasSettingsModal.tsx:60`
- ✅ **列剖析统计（/run/profile）** — 对预览样本按列算 non_null/null/distinct/min/max/mean（按类型守卫，非数值降级为空），与 preview 一样走 kernel。 `kernel/hub/executors/profile.py; kernel/hub/routers/runs.py run_profile`
- 🟡 **pod/Ray/队列 runner** — 已内置**参考多 worker pool 后端**（`DP_POOL_WORKERS` 开启，能力化放置 + RunController + placement planner，见 kernel/pool_runner.py · run_controller.py · placement.py）；k8s-pod/Ray 仍是插件扩展点。 `kernel/hub/deps.py:155-164` `↗ 部署见 §6.6`

**选择**
- ✅ **pick_runner / chosen_backend（默认 = kernel）** — per-user 偏好 > workspace 默认 > `DP_EXECUTION` > **默认 kernel**（kernel-only：无显式选择即走 per-canvas kernel，隔离+durability+热复用）；显式选择永远胜出（stale/未装的选择降级到 kernel 默认，不再静默落到 local）；in-process/subprocess 仍注册可选（pool 内部用 subprocess）。run_index 路由 status/cancel，未命中经 run_states DB 回退（跨实例/重启安全，cancel 也回退）。 `kernel/hub/deps.py chosen_backend/pick_runner; test_default_execution_is_the_per_canvas_kernel`

**生命周期**
- ✅ **每运行的 DuckDB 游标隔离（db.run_scope）** — 并发运行/预览不再串行在全局锁；退出只回滚 + 清自己的视图。 `kernel/hub/db.py:84`
- ✅ **运行估算** — 粗略而诚实，不编造每算子 ETA；无源可计数报 'size unknown'。 `kernel/hub/plugins/runner.py:62`
- ✅ **实时状态 + 运行历史持久化（跨实例/重启安全）** — on_status→run_states，_status_or_lost 回退，不返回 404；周期性 reaper（每 KERNEL_STALE_S）判死 owning kernel 已亡的 run，不再只在启动时。 `↗ 部署见 §6.6` `kernel/hub/plugins/runner.py:47 on_status; routers/runs.py:269 _status_or_lost; main.py _reaper_loop`
- ✅ **启动时协调孤儿运行（心跳门控 reaper）** — reap_orphaned_runs 只在运行的 owning kernel 死/失联时判 failed('interrupted')；owning kernel 还活着的 run 保留（可重连），无 kernel 的进程内/子进程 run 随 hub 死→failed。取代旧的"重启即全判失败"；reap_kernels 清死租约。 `kernel/hub/metadb.py reap_orphaned_runs/reap_kernels`
- 🟡 **运行取消 + 查询中断** — 步骤间可取消 + scope.interrupt() 中断游标查询；纯 Python 死循环只有 subprocess kill 能停。 `kernel/hub/plugins/runner.py:268; db.py:75`
- ✅ **内容寻址缓存（未变更 plan 走缓存，持久化 + 跨实例）** — DB 支撑的 `result_cache` 表（迁移 0008），跨运行/重启/实例复用；plan 内容哈希 → 输出 uri，非可缓存 plan(对象存储源/append/library/plugin)不缓存；进程内 dict 仅未接线 fallback。 `kernel/hub/deps.py:146; kernel/hub/metadb.py get_result/put_result; migrations/versions/0008_result_cache.py`
- 🟡 **成本 / 确认门控** — 现共用 `hub.estimate`（`_row_estimate` = 目标 cone 内最大估计行数，含源计数 + 下游 sample 缩减），但仍是固定 5M 行阈值、无字节/成本模型；大小未知→放行（HTTP 409 除非 confirmed）。 `kernel/hub/routers/runs.py _row_estimate; kernel/hub/plugins/runner.py:72`
- ✅ **成本感知放置（估计 → 内存需求 → 路由）** — 一个阻塞 region（sort/join/aggregate/sql/…）的估计工作集（输入字节之和）超过本地内存预算（`DP_MEMORY_LIMIT`/`DP_KERNEL_MEM`，默认 4GB）时，planner 折入一个 **mem 需求下界**（仅抬高、不动 cpu/gpu/labels，`_merge_mem`），现有 `_place`/`placement.satisfies` 据此路由到内存够的后端。**只注册本地后端时严格无操作**（无后端满足→default region→base runner，行为不变）；**手动 `config.requires.mem` 最高优先级**（声明了 mem 的节点估计器完全不插手）；估计失败也不阻断运行。贪心 per-node（全局优化留后）。 `kernel/hub/run_controller.py _cost_requires/_working_set_bytes; planner.py _merge_mem; deps.py local_mem_bytes`
- ✅ **分层物化 + 后端可达性（region handoff tier）** — region 边界物化到"生产者与消费者都够得着的最省 tier"：本地→本地走本地盘，涉及远程后端才走共享对象存储（s3/gs）——"不是每次 handoff 都写 S3"。后端经可选 `reachable_tiers()` 声明可达 tier（默认后端=本地+对象，假定远程后端=仅对象）；内容寻址复用保留（缓存 key 带 `@<tier>`，与 write 节点的 content-skip 不冲突），无共享 tier→退回本地+告警。默认→默认（checkpoint）仍走本地、行为不变。**C3 跨 tier 自动搬运**：上一 run 若在别的 tier 物化过同一 region，就 `_move_tier` 拷过来而非重算（本地结果喂远程步骤时自动传过去）。这正是"物化到 S3 供下一步 job 读"与"浏览中间结果"的同一个机制。 `kernel/hub/tiers.py; kernel/hub/run_controller.py _boundary_tier/_materialize/_move_tier`
- ✅ **运行计划预览（让调度可见）** — `POST /graph/plan`（`RunController.plan_summary`）返回一个 target 会切成哪些 region、每个 region 的后端 + 边界物化 tier + 估计输出行数；Inspector 的 "Run plan" 区块渲染它，**且只在放置真的做了事时才出现**（分成多 region、或路由到非 default 后端——即注册了集群后端 / 标了 `engine=ray` / 打了 checkpoint），平凡的单本地 region 自动隐藏（卡片已显示 ~N 行）。让"成本感知放置 + 分层物化"在跑之前就看得见。 `kernel/hub/run_controller.py plan_summary; kernel/hub/routers/runs.py graph_plan; web/src/panels/Inspector.tsx RunPlan`

### §2.4 计算节点执行 · 5✅ 1🟡
- ✅ **chart 图表节点（可视化）** — build 成 (x,y) 序列：bar/line/area 走 `agg(y) group by x`（预览也跑全量、诚实，上游有 Python transform 则拒；TRY_CAST 让非数值 min/max 降级为空而非报错），scatter 走原始点；数据面板渲染零依赖、明暗自适应 SVG；输出可继续接。 `kernel/hub/executors/engine.py chart 分支; web/src/panels/DataPanel.tsx ChartView; web/src/nodes/kinds/chart.tsx`
- ✅ **Transform 逃生舱（在 Arrow RecordBatches 上跑 Python）+ 可选批表示** — map/filter/flat_map/map_batches；onError='skip' 丢失败行；完整运行溢出 Parquet。**map_batches 可选把整批递给单元格的表示**（`batchFormat`）：行字典（默认）/ **pandas.DataFrame** / **pyarrow.Table**——pandas/arrow 走 arrow-native 路径**保住列类型**（不经 dict 往返丢时间戳/嵌套类型），经共享 `resolve_config` 传播到引擎、IR、**dp_ray** 三处一致（Ray 上用同一个 `_apply_batch`）。pandas 需在 requirements 声明；pyarrow 常驻、已加入沙箱基线白名单。 `kernel/hub/executors/engine.py _transform/_apply_batch; kernel/hub/ir.py resolve_config; examples/plugins/dp_ray/__init__.py _make_mapper`
- ✅ **用户单元格代码的软沙箱** — 软性防护非安全边界；builtins 白名单 + import 白名单 + AST 拒 dunder + 墙钟超时。多用户真正的隔离靠 auth 模式默认的 subprocess runner（崩溃/DoS 隔离，非多租户牢笼；见 §2.3、README 多用户隔离）。 `kernel/hub/sandbox.py:82`
- ✅ **Section 元编程（驱动脚本复合节点）** — 只完整遍历，每次迭代物化 Parquet + GC；run() 携带 parentId 子树以支持嵌套 section。 `↗ 容器 UI 见 §1.6` `kernel/hub/section.py:75`
- ✅ **向量搜索引擎入口** — 查询向量来自配置或选中行，预览也在完整输入（忠实）；裸 Lance source 走原生 ANN 否则暴力余弦。 `↗ 详见 §3.5` `kernel/hub/executors/engine.py:415`
- 🟡 **独立的 loop 节点** — 裸 loop 是直通占位符，真迭代走 section；环路一开始被拒（必须封装）。 `↗ §1.3 控制流` `kernel/hub/executors/engine.py:307`

---

## §3 数据 · 适配器与目录

### §3.1 数据集适配器（DatasetAdapter）· 9✅ 1🟡 · `↗ SPI 契约见 §5.3`
**DuckDB 适配器 — 扫描格式**
- ✅ **Parquet 扫描（惰性/核外）** — 默认读取器；也处理 parquet 分片的对象存储前缀。 `kernel/hub/plugins/adapters.py:151`
- ✅ **CSV/TSV 扫描** `kernel/hub/plugins/adapters.py:144`
- ✅ **JSON/NDJSON 扫描** `kernel/hub/plugins/adapters.py:146`
- 🟡 **Arrow/Feather/IPC 扫描** — 仅本地文件（pyarrow 读入 DuckDB）；无对象存储路径、不支持追加。 `kernel/hub/plugins/adapters.py:148`
- ✅ **文件目录扫描（part-*.<ext> 数据集）** — 扫描追加模式写的目录，递归 glob 覆盖 parquet/pq/csv/tsv/json。 `kernel/hub/plugins/adapters.py:153`
- ✅ **CSV 解析选项（分隔符/表头覆盖，否则自动检测）** — source 节点只在设置时传覆盖；分隔符接受 'tab'。 `kernel/hub/plugins/adapters.py:61`
- ✅ **mem:// 适配器（内存态命名表）** — 把进程内 DuckDB 表暴露为数据集（测试/fixture）。 `kernel/hub/plugins/adapters.py:128`

**Lance 适配器**
- ✅ **Lance 流式扫描（列/limit 下推）** — 需 lance extra；列/limit 下推，谓词扫描后于 DuckDB 应用。 `kernel/hub/plugins/adapters.py:246`

**对象存储**
- ✅ **对象存储扫描/写入（s3/gs/gcs/r2）经 DuckDB httpfs** — httpfs 在 ensure_object_store 显式加载。 `kernel/hub/plugins/adapters.py:130`
- ✅ **对象存储凭证（显式 key / AWS 链 / MinIO·R2 自定义 endpoint）** — CREATE SECRET，回退 credential_chain。 `kernel/hub/db.py:138`

### §3.2 写入 · 4✅ 1🟡
- ✅ **写入格式 parquet/csv/tsv/json/arrow/lance** — 按扩展名；JSON 经 COPY，Lance 经流式 record batch。 `kernel/hub/plugins/adapters.py:207`
- ✅ **写入模式覆盖** `kernel/hub/plugins/adapters.py:199`
- ✅ **写入模式追加（分片文件目录）** — parquet/csv/tsv/json + Lance 原生；每次写 part-*.<ext>，_read_dir glob 读回；Arrow/Feather 有意不支持追加。 `kernel/hub/plugins/adapters.py write(append)+_read_dir`
- ✅ **内容寻址的写入跳过（幂等覆盖重跑）** — 相同覆盖 plan 已产出且仍在→跳过重写；追加从不缓存。 `kernel/hub/plugins/runner.py:218`
- 🟡 **原子覆盖写（临时文件 + os.replace）** — 仅本地写入原子；对象存储覆盖是就地写（依赖单对象 PUT 原子性，多文件/目录非事务）。 `kernel/hub/plugins/adapters.py:203`

### §3.3 目录（Catalog）· 10✅ · `↗ SPI 契约见 §5.5`
- ✅ **list / get / search** — 内存态 RLock 串行；搜索匹配 name/uri 子串。 `kernel/hub/plugins/catalog.py:101`
- ✅ **血缘（围绕某 uri 的连通分量图）** — 去重父/子边 BFS；遍历前合并其他实例的边。 `kernel/hub/plugins/catalog.py:122`
- ✅ **启动时从本地数据目录播种目录** — 发现 parquet/csv/tsv/json/arrow + .lance 目录。 `kernel/hub/plugins/catalog.py:39`
- ✅ **目录注册（跨重启持久化）** — adapter.schema 校验可读，写穿 catalog_entries（DB 单行/条目），不再靠 settings blob 重注册（重启后从 DB 合并回来）。 `kernel/hub/routers/catalog.py:75`
- ✅ **上传数据集（POST /catalog/upload）** — 裸 body 流式落地（`request.stream()` 边收边判 `DP_MAX_UPLOAD_BYTES`，超限中途中止、不预缓冲、chunked 也不靠 Content-Length），写进共享 storage（本地 dir 或对象存储，跟随 `DP_STORAGE_URL`）再 `register_output`（独立 run_scope，count(*) 全扫不占全局锁）写穿 catalog 跨实例可见；本地=字节原样（原子 `os.replace`），对象存储=经 DuckDB httpfs 重写并把扩展名对齐实际格式（tsv→csv、ndjson→json、arrow→parquet）；同名加短后缀不互相覆盖。 `kernel/hub/routers/catalog.py catalog_upload/_land_upload`
- ✅ **register_output（write 节点注册输出 + 血缘边）** — 提交写入后用父 uri + pipeline='canvas' 注册。 `kernel/hub/plugins/runner.py:253`
- ✅ **DB 支撑/跨实例（catalog_entries + catalog_edges）** — 写穿 + 读时 _load_from_db 合并，跨实例可见；尽力而为，每次全量重合（大目录扩展性问题）。 `↗ 部署见 §6.6` `kernel/hub/plugins/catalog.py:82`
- ✅ **缺失检测（missing 标记，不静默隐藏）** — overlay 时对本地路径条目探活，源文件消失标 `missing=true`（跳过 object-store 与 mem://，不误报），前端可如实呈现而非假装还在。 `kernel/hub/plugins/catalog.py _overlay`
- ✅ **注销条目（DELETE /catalog/tables/{id}）** — 锁内删内存态 + DB 行（不删底层数据），清掉陈旧/缺失条目。 `kernel/hub/routers/catalog.py; kernel/hub/plugins/catalog.py unregister`
- ✅ **按名字/id 解析 source（图内引用改写）** — `resolve_ref` 把 source 节点里写的数据集名字/catalog id 解析成真实 uri；compile/preview/schema/estimate/run 各端点在编译前 `resolve_source_refs` 就地改写，让图可移植（不必硬编码绝对路径）。 `kernel/hub/plugins/catalog.py resolve_ref; kernel/hub/graph.py resolve_source_refs; kernel/hub/routers/runs.py`

### §3.4 目标位置（Destinations）· 2✅ 1🟡
- ✅ **本地 backend 浏览 + mkdir（受 root 限定）** — realpath 阻止越界遍历；.lance 目录以文件显示。 `kernel/hub/destinations.py:27`
- ✅ **目标预设（全局设置 + 默认 workspace 输出）** — 始终注入默认 'outputs'；DP_STORAGE_URL 可设为 s3/gs 前缀。 `kernel/hub/destinations.py:110`
- 🟡 **对象存储 backend 浏览 + target_uri** — glob 浏览前缀；对象存储 mkdir 空操作（无真文件夹）；凭证/桶缺失如实报错。 `kernel/hub/destinations.py:61`

### §3.5 向量搜索 · 3✅
- ✅ **Lance 原生 ANN（有向量索引则用）** — 仅裸 .lance source；需 lance extra；错误回退暴力余弦；_score=1-距离。 `↗ adapter 契约见 §5.3` `kernel/hub/executors/engine.py:447`
- ✅ **DuckDB 上暴力余弦** — list_cosine_similarity ORDER BY DESC LIMIT k；固定大小数值 list 列；完整输入忠实 top-K。 `kernel/hub/executors/engine.py:453`
- ✅ **按外部向量或按行索引查询** — queryVector 接 JSON/list，否则 queryRow 用数据集偏移。 `kernel/hub/executors/engine.py:428`

### §3.6 数据面安全 · 3✅
- ✅ **SSRF 防护（禁用 DuckDB 扩展 autoload/autoinstall）** — 阻止任意 https:// uri 静默拉 httpfs 取远程数据，每运行游标重断言；对象存储显式加载 httpfs。 `kernel/hub/db.py:42`
- ✅ **本地路径的数据集 URI 限定** — 认证模式下本地路径（register/sample/source 构建）必须在允许 root 内，越界 403；对象存储 URI 不受影响，开放单用户模式不限定。 `kernel/hub/paths.py:24`
- ✅ **DuckDB 原生文件系统沙箱（统一覆盖含裸 sql）** — 认证 + 无对象存储时，运行连接设 `allowed_directories` + `enable_external_access=false`，让 `read_csv`/`COPY`/裸 `sql` 也逃不出允许 root（不止 source 节点这一层，防绕过）；`enable_external_access` 是进程级一次性单向开关，故在基连接上应用一次，配了对象存储需网络访问则与沙箱互斥。 `kernel/hub/db.py:110 _apply_session`

### §3.7 关系与 join 提示（catalog 驱动）· 9✅
- ✅ **键检测（key capability + 复合键模型）** — id/uuid/*_id/*_key 命名 + 可 join 类型标 `key` 能力（media/vector 列不算键）；每表推断单列 PK 候选，`KeyInfo` 模型支持复合键（join 时形成组合候选）。 `kernel/hub/plugins/capabilities.py:38; kernel/hub/relationships.py:33`
- ✅ **基数实测（DuckDB 单趟 count/distinct）** — `count(*)` 与 `count(DISTINCT key)` 一趟聚合出唯一性（一次扫描，不耗尽 Lance 的一次性 Arrow reader）；唯一侧=父（1），非唯一=子（N）；不可测/空数据返回 None→'unknown'（绝不谎报），在 run_scope 游标上跑不占基连接锁。 `kernel/hub/relationships.py:56`
- ✅ **join 提示（两数据集端点）** — 匹配键列（同名或 id↔*_id + 类型兼容）+ 实测基数→排序建议（有父侧/精确名/窄键优先）；复合候选上限防组合爆炸 + 记忆化。 `POST /api/catalog/join-suggestions · kernel/hub/relationships.py:150`
- ✅ **画布 join-analysis（建议 + fan-out 警告）** — 为 join 节点两输入出建议 + 非 1:1 时的扇出警告；left/right 按 incoming 边序解析（与引擎 a/b 别名一致）。 `POST /api/graph/join-analysis · kernel/hub/relationships.py:186`
- ✅ **grain 传播（键集经 relational ops）** — 源的 PK 经 filter/sample/sort 保留、group-by/dedup 重定 grain（唯一）、code/sql/section/join 变未知（诚实）；重命名/派生的 select 丢弃 grain（bare 直传才保留）；这就是"采样/聚合后仍可 join"背后的事实。 `kernel/hub/grain.py:41`
- ✅ **前端 join hints（inspector）** — 防抖拉取建议、基数徐章（1:1/1:N/N:M，明暗主题）、扇出警告条、点击建议填 `on`（同名 USING）/`condition`（异名 a.x=b.y）。 `web/src/panels/Inspector.tsx JoinHints`
- ✅ **声明式主键（不透明 transform 的出路）** — `PUT /catalog/tables/{id}/key` 设/清声明键，领先推断键、grain 中胜出（declared>verified>inferred）；校验列存在；去重推断孪生；**每 uri 一行**持久化（catalog_declared_keys 表）跨实例、无 blob 丢更新。 `kernel/hub/plugins/catalog.py set_declared_key; kernel/hub/routers/catalog.py declare_key`
- ✅ **声明式关系（ER 边）** — `GET/POST /catalog/relationships` + `.../delete`，复合可表达，**每关系一行**持久化（catalog_relationships 表，orientation-insensitive key upsert）跨实例、无 blob 丢更新；join-analysis 中声明关系领先实测（存反了自动翻转基数）。 `kernel/hub/plugins/catalog.py relationships; kernel/hub/relationships.py _declared_suggestion`
- ✅ **ER / UML 视图（Relationships 视图）** — React Flow 实体图：每数据集一实体（列 + 🔑 主键徽章），声明关系实线带基数、命名候选虚线（仅 FK 式匹配，非裸 id↔id）；点列声明主键（点多列=复合键）、拖两表弹选择器声明 join（挑建议键或手选列+基数）、点边删除；明暗主题、布局 localStorage 持久化。 `web/src/views/ERDiagram.tsx; web/src/views/Shell.tsx`

---

## §4 协作 · 多用户与认证

### §4.1 实时协作 · 6✅ 1🟡 · `↗ 画布光标 UI 见 §1.6`
- ✅ **实时协同编辑（Yjs CRDT）** — 真 CRDT 合并（nodes/edges/meta），非最后写入者赢；笨中继无服务端 Y.Doc；'房间第一个' 800ms 计时器启发式。 `web/src/collab/ydoc.ts:50-113`
- ✅ **在线状态 / 对等端光标** — 光标色按浏览器会话随机、不绑身份；sendCursor 50ms 节流；PeerCursors 映射到各视口。 `web/src/collab/collab.ts:92-96; PeerCursors.tsx`
- ✅ **协作通道 viewer 角色写入门控** — 丢弃 viewer 的入站 yjs 更新（仍中继在线状态），堵住越只读边界洗白。 `kernel/hub/main.py:115-116`
- ✅ **CRDT 感知的协作式撤销/重做** — 撤销只回退自己的更改、不删对等端并发加的节点；离线回退自身快照栈。 `web/src/collab/ydoc.ts:99-104`
- ✅ **协作写放大防护** — 只发起编辑者 PUT（对等端合并编辑不 PUT），离线缓存仍无条件写。 `web/src/collab/undo.ts:23; graph.ts:870`
- ✅ **标签页关闭 pagehide 刷新** — 唯一发起编辑者在 400ms 防抖窗口内断连时兜底 keepalive PUT。 `web/src/store/graph.ts:906-911`
- 🟡 **协作 WebSocket 中继（按画布分房间）** — 每实例内存态房间；多实例需 sticky-route 每画布到单实例，否则对等端互不可见；单进程 OK。 `↗ 部署见 §6.6` `kernel/hub/main.py:88-135`

### §4.2 身份与认证 · 5✅
- ✅ **按用户的身份（scrypt 密码 + 签名会话 cookie）** — scrypt$salt$hash（仅标准库）；HMAC 签名 7 天 TTL token；DP_AUTH_PASSWORD 只引导默认用户；httponly+samesite=lax；DP_AUTH_SECRET 开启否则 X-DP-User 开发模式。 `kernel/hub/auth.py:38-83; routers/workspace.py:31-56`
- ✅ **拒绝弱/已知 DP_AUTH_SECRET（启动即失败）** — 启动时校验密钥不是出厂/示例值，弱密钥会让签名会话可伪造，故 fail fast 而非静默运行。 `kernel/hub/auth.py:39 reject_weak_secret; main.py:55`
- ✅ **自助改密/轮换密码** — /auth/password 需旧密码、min 6；Shell 改密对话框。 `kernel/hub/routers/workspace.py:46-56; web/src/views/Shell.tsx:72-119`
- ✅ **X-DP-User 开放（免认证）开发模式** — 无 DP_AUTH_SECRET 时信任 header=用户；认证开启后明确不信任。 `kernel/hub/security.py:21-26`
- ✅ **登录界面 + 登录名单** — 用户选择器 + 密码；无 SSO/OIDC；public GET /users 只回 id+name 无邮箱。 `web/src/views/Login.tsx:10-21`

### §4.3 授权与分享 · 4✅ · `↗ 门控实现见 §6.1`
- ✅ **安全默认的 /api 认证门控（路由级 Depends）** — include 时施加，新路由默认受保护，除非挂 public_router；修复了 /run,/data,/catalog 门户大开的时代。 `kernel/hub/main.py:50-54`
- ✅ **画布分享（可见性 private/workspace/workspace_view + 显式协作者）** — workspace_view=人人只读；canvas_role 对其回 viewer；add_share 校验值（未知 400）。 `kernel/hub/metadb.py canvas_role; ShareModal.tsx`
- ✅ **画布访问控制/授权（owner/editor/viewer）** — put_canvas 403 非 editor；delete 仅 owner；editor 不能再分享；前端 403→view-only 提示。 `kernel/hub/routers/workspace.py:129-147`
- ✅ **管理员角色（is_admin，门控实例级操作）** — 引导/唯一用户即 admin（升级过的库自动补一个 admin，不至无人可管）；建用户 + 实例级设置（对象存储凭证 / agent key / destinations）需 admin，认证模式下非 admin 403。 `kernel/hub/metadb.py:239 is_admin (migration 0010); routers/workspace.py:87 _require_admin`

### §4.4 认证门控的 WebSocket · 3✅
- ✅ **协作 WS 认证门控** — 开放模式不门控；否则验 dp_session + canvas_role，无角色 close 1008。 `kernel/hub/main.py:97-103`
- ✅ **运行状态 WS 认证门控** — 无有效会话 close 1008。 `kernel/hub/main.py:69-71`
- ✅ **跨站 WebSocket 同源门（CSWSH 防护）** — 两个 WS 端点先查 Origin 同源，跨站直接拒，堵住浏览器带 cookie 的跨站 WS 劫持。 `kernel/hub/main.py:62 _cross_site_ws`

### §4.5 持久化（协作面）· 3✅ · `↗ metadb 见 §6.3`
- ✅ **自动保存（防抖 PUT）+ 离线缓存** — 400ms 防抖 PUT + 无条件 localStorage 缓存；区分 403（view-only）与离线。 `web/src/store/graph.ts:859-898`
- ✅ **画布版本历史 + 恢复** — 节流自动快照（90s 去重、留最新 30、命名快照永留）+ 恢复先快照当前（可撤销）。 `kernel/hub/metadb.py:422-461; workspace.py:150-177`
- ✅ **运行历史（按画布，持久化）** — RunRecord 表存活重启；临时画布空操作；同授权 GET /canvas/{id}/runs。 `kernel/hub/metadb.py:306-340`

---

## §5 扩展点 · 插件 SPI

### §5.1 插件发现与版本 · 5✅
- ✅ **发现：拖入式 workspace 文件夹** — 扫 <workspace>/plugins/<pack>/ 带 register(reg)，加 sys.path，先读 dataplay.toml。 `kernel/hub/deps.py:150-161`
- ✅ **发现：pip entry-points（dataplay.plugins 组）** — 加载并调 ep.load()(reg)；无测试覆盖，min_core_api 不应用到 entry-points。 `kernel/hub/deps.py:165-174`
- ✅ **发现：DP_PLUGINS 环境变量模块列表** — 逗号分隔模块导入并 register。 `kernel/hub/settings.py:20; deps.py:163-164`
- ✅ **版本协商（min_core_api vs CORE_API_VERSION）** — 校验 dataplay.toml；要求更新核心的包记错不加载；仅对拖入式强制。 `kernel/hub/deps.py:176-201`
- ✅ **插件自省端点（/api/plugins, /api/kernel）** — 列已加载包的 source/version/error；加载失败被捕获不崩溃。 `kernel/hub/routers/catalog.py:46-48; deps.py:216-223`

### §5.2 节点 SPI · 4✅ · `↗ 构建协议实现见 §2.1`
- ✅ **节点编写 SDK（add_node + ctx 构建器 sql/arrow_map/polars）** — ctx.sql 用 {input} 占位视图；add_node 拒绝遮蔽内置/已注册类型。 `kernel/hub/sdk.py:38-70`
- ✅ **NodeBuilder 协议（插件构建契约）** — runtime_checkable，单输出 Relation / 多输出 {port:Relation}。 `↗ 引擎侧见 §2.1` `kernel/hub/backends.py:27-41`
- ✅ **NodeSpec 通用前端渲染（插件节点无需前端代码）** — 类型化端口/参数 + code 参数编辑器（片段按钮开全屏编辑器）；示例插件端到端验证。 `web/src/nodes/generic.tsx`
- ✅ **前后端节点 spec 一致性守卫** — 测试解析 kinds/*.tsx register 字面量比对 BUILTIN_NODE_SPECS，守漂移；通用渲染类型跳过。 `kernel/hub/tests/test_kernel.py:1263-1334`

### §5.3 适配器 SPI · 4✅ · `↗ 内置实现见 §3.1/§3.5`
- ✅ **数据集适配器 SPI（add_adapter；DuckDB + Lance 内置）** — 正式 `DatasetAdapter` runtime_checkable 协议（backends.py），完整 matches/scan/schema/count/fingerprint/write（+ 可选 nearest）；内置 DuckDB/Lance 都 conform（内置 = 第一个走接缝的实现，非特权路径）；插件 insert(0) 抢认领，resolve_adapter 回退 DuckDB。 `kernel/hub/backends.py DatasetAdapter; kernel/hub/plugins/adapters.py:99-291`
- ✅ **Lance 原生 ANN（可选的 adapter.nearest）** — 余弦 kNN 下推 Lance；缺 nearest 时通用 vector-search 回退暴力余弦。 `kernel/hub/plugins/adapters.py:271-278`
- ✅ **谓词/投影下推到 adapter.scan（source→filter/select）** — 单消费者的 source→filter / source→select 链在**全量运行**时把谓词 / 投影交给 `adapter.scan(predicate=/columns=)`，仓库/Iceberg/插件适配器可在源头裁剪行/列（内置 DuckDB 适配器也认）；消费节点仍应用自身算子，故结果逐字节一致。护栏：仅全量写/计数路径（`pushdown` 标志，preview 保持原样、无 warm 缓存串键）、永不裁剪运行的目标节点本身、仅单消费者、跳过 bypass/disabled、投影仅限可证的纯列名列表。 `kernel/hub/executors/engine.py _source_pushdown; plugins/runner.py; subrun.py; run_controller.py`
- ✅ **参考适配器插件（HF datasets + Apache Iceberg）** — 两个真插件走 add_adapter 接缝：`hf://<id>[@config][:split]` 读 Hugging Face Hub、`iceberg://<catalog>/<ns>.<table>` 读 Iceberg 表（catalog 从 pyiceberg 配置解析）；都懒加载重依赖（无 extra 也能装、只在用到 scheme 时报错）、只读（write 抛错）；可选 extra `[hf]`/`[iceberg]`；测试对内存 stand-in 跑（importorskip → CI 无 extra 时跳过）。 `examples/plugins/dp_hf_datasets; examples/plugins/dp_iceberg; kernel/hub/tests/test_kernel.py test_hf_datasets_adapter_reference_plugin/test_iceberg_adapter_reference_plugin`

### §5.4 执行器 SPI · 5✅ · `↗ 内置实现/选择/生命周期见 §2.3`
- ✅ **ExecutionBackend SPI（add_runner；替代 runner）** — runtime_checkable 协议；内置 Local + Subprocess；pick_runner 按名字选、run_index 路由。 `kernel/hub/backends.py:43-62; deps.py:286 pick_runner, 256 run_index`
- ✅ **KernelSpawner + Storage 点分路径可插拔** — `DP_KERNEL_SPAWNER` / `DP_STORAGE` 除内置关键字（local/pod、本地/对象）外接受 `pkg.mod:Cls` 点分路径，`import_dotted` 加载并实例化 → 第三种底座/存储后端无需改核心（此前写死、是唯一 fork-forcing 的接缝）。 `kernel/hub/settings.py import_dotted; deps.py _make_spawner; storage.py make_storage`
- ✅ **PlaceableBackend 可选协议（分布式放置接缝）** — 分布式 runner 可另实现 `workers()`/`place(requires)`/`run_unit(graph, output_node, output_uri)`；核心特性探测（非分布式后端省略即可），节点用 `NodeSpec.requires`/`config.requires` 声明需求，仅当注册了 place() 能力的后端时才激活放置。 `kernel/hub/backends.py PlaceableBackend`
- ✅ **引擎中立执行 IR + Ray Data 参考后端（dp_ray）** — `hub.ir.lower_to_ir(graph, target)` 把每个节点一次性读成 `CompiledIR`（归一化 op + 已解析可移植 config + 输入连线），非 DuckDB 引擎据此运行而不重读 config / 重写 lowering；`is_clean()`/`plan_is_clean()` 圈定 map 式引擎能端到端跑的子集（read/write/passthrough + 逐行/逐批 map/filter/flat_map/map_batches），关系/归约/opaque 类回退 DuckDB。参考插件 `dp_ray` 是第一个非 DuckDB 引擎跑画布：复用**同一个** `sandbox.compile_operator` 算子故结果逐字节一致，can_run 门控 + run 回退双保险，`DP_EXECUTION=ray-data` 显式选用。**它现在也是一个 PlaceableBackend（区域派发，D）**：`run_unit(graph, output_node, output_uri)` 把**一个 region** 跑在 Ray 上并把结果物化到 tier uri（parquet 源经 `ray.data.read_parquet` **worker 直读**，不再穿 driver；非 clean region 安全回退到 base 的 subprocess run_unit）；`place()` **仅当节点显式标 `engine=ray`** 才认领（成本放置的 mem 需求不会误路由到这里），`reachable_tiers()=(local,object)`。测试：算子等价 + 门控 + 回退 + 放置/tier 门控（无需活集群）+ 可选 live 差分与 **live run_unit**（`DP_TEST_RAY_LIVE=1`，本机真 Ray 验证过 worker 直读 + region 物化）。生产用的真集群 job 系统照这套已证明的 SPI（run_unit 读输入 tier uri / 写输出 tier uri）去接。 `kernel/hub/ir.py; examples/plugins/dp_ray`
- ✅ **IR 统一：引擎共用同一解析器 + 插件节点可跑分布式** — (A) `hub.ir.resolve_config(node)` 成为内置节点配置的**唯一解析器**，`BuildEngine._lower` 也经它取值 → 引擎与后端配置不再分歧（此前踩过的分歧类 bug 根除）；全 178 套件逐字节一致守护。(B) 插件节点可带**引擎中立 emit 钩子** `reg.add_node(spec, build, ir=…)`：ir(node)→{op,config} 让节点 lower 成真 op（如带内联 code 的 clean `map`）而非 opaque，于是能跑在分布式后端上、不只 DuckDB；`PlanStep.op` 由 compile_plan 经 IR 解析器填充，故 can_run 对插件节点的真实 op 也能门控。参考插件 `dp_upper`（build 与 ir 共用一份生成算子，DuckDB 与 Ray 结果逐字节一致，无需活集群即可验证）。 `kernel/hub/ir.py resolve_config/_op_and_config; deps.py add_node(ir); compiler.py; examples/plugins/dp_upper`

### §5.5 目录 SPI · 2✅ · `↗ 内置目录见 §3.3`
- ✅ **目录提供者 SPI（set_catalog；默认 InMemoryCatalog）** — 正式 `CatalogProvider` runtime_checkable 协议（backends.py，11 方法；`get_table` 未命中必须抛 `KeyError`）；只读的外部目录可子类化 InMemoryCatalog 只覆盖读方法；默认每实例缓存写穿 + 从 metadb 加载，RLock 线程安全。 `kernel/hub/backends.py CatalogProvider; kernel/hub/plugins/catalog.py:21-177`
- ✅ **参考目录插件（SQL/Postgres 支撑）** — 真插件走 set_catalog 接缝：`SqlCatalog(InMemoryCatalog)` 只覆盖 list_tables/get_table，从 SQL `datasets(name, uri)` 表 `_sync`（SQLAlchemy，用公开 register_output），其余继承；`DP_SQL_CATALOG_URL`/`DP_SQL_CATALOG_TABLE` 配置；证明「只读外部目录子类化内置」的路径。 `examples/plugins/dp_sql_catalog; kernel/hub/tests/test_kernel.py:1838`

### §5.6 能力与处理器 · 3✅
- ✅ **处理器库 + 提升到库** — promote() 从临时单元格注册代码处理器（版本自增）；沙箱编译；POST /processors/promote + GET /processors。 `kernel/hub/plugins/processors.py:62-97`
- ✅ **add_capability 检测钩子（插件可加列检测）** — capability 带可选 `detect(col)->bool` 时，deps 把它注册进 `register_detector`，`tag_columns` 在内置 media/vector/key 之外应用插件检测器（try/except 护栏）→ 插件能给列打自定义能力标签，无需改核心。 `kernel/hub/plugins/capabilities.py register_detector; deps.py add_capability`
- ✅ **Capabilities 查看器标签页（插件可声明式加 tab）** — capability 可带 `viewer = {"kind": …}`；`deps.info()` 经 `KernelInfo.capability_views` 暴露，前端按 `kind` 用**通用渲染器**（`grid` = 媒体网格、`json` = 单元格美化 JSON）注册 tab —— 插件加查看器标签页**零前端代码**（和 NodeSpec 渲染节点卡同理）。参考插件 `dp_json_view`（检测 JSON-doc 列 + viewer kind json）。内置 media 仍走同机制；通用渲染器词表可在核心扩展。 `kernel/hub/plugins/capabilities.py; models.py CapabilityView; web/src/nodes/capabilities.tsx; examples/plugins/dp_json_view`

### §5.7 流水线导入器 SPI · 2✅
- ✅ **/pipelines/import → 可运行画布图** — 正式 `Importer` runtime_checkable 协议（`import_pipeline(config, params) -> PipelineImport`）；`PipelineImport.graph` 现携带一张可运行的画布 `Graph`（内置/插件节点的 nodes+edges），前端把它落到新画布并直接运行——这才让「导入外部流水线 → 可运行画布」成真（此前 PipelineImport 只是描述、无 nodes/edges）。路由对未定位的图自动布局；无导入器时仍诚实 501。 `kernel/hub/plugins/importer.py Importer; models.py PipelineImport.graph; graph.py layout; routers/catalog.py import_pipeline`
- ✅ **参考导入器插件（dp_json_pipeline）+ 往返测试** — 真插件走 set_importer 接缝：把一个小 JSON 流水线（`source`/`steps`/`write`）解析成 source→…→write 的节点链；测试 POST /pipelines/import → 断言节点种类/连线/已布局 → 把返回的图喂给 POST /run 跑到 done（真正的 import→canvas→run 往返）。 `examples/plugins/dp_json_pipeline; kernel/hub/tests/test_kernel.py test_json_pipeline_importer_round_trips_to_a_run`

### §5.8 目的地 SPI + 插件配置 · 2✅
- ✅ **目的地 SPI（reg.add_destination；正式化上 Registry）** — 保存/打开对话框的「地点」后端（`DestinationBackend`：kind + browse + target_uri）现走 `register(reg)` 而非模块级 `destinations.register_backend()`（内置 local/s3/gs 同一注册表）；参考插件 `dp_datasets_place`（kind `datasets`，只列数据集文件、藏杂项、路径锁在 root 内）+ 遍历/注册测试。 `kernel/hub/deps.py Registry.add_destination; destinations.py DestinationBackend; examples/plugins/dp_datasets_place`
- ✅ **声明式插件配置 + UI 表单（dataplay.toml [[config]]）** — 插件在 manifest 里声明 `[[config]]` 字段（key/type/label/default/env/secret/options/help，VSCode contributes.configuration 式）；`reg.config(key, default)` 按 **UI 设置(`plugin.<包>.<键>`) > 声明的 env > 声明的 default > 参数 default** 解析（UI 可配 + headless 走 env）；`GET /api/plugins` 暴露 schema+当前值（secret 不回显、只报是否已设），Settings → Plugins 用通用表单渲染；改动下次 kernel 启动生效。`dp_sql_catalog` 为样板。 `kernel/hub/deps.py Registry.config/_normalize_config; routers/catalog.py list_plugins; web/src/panels/SettingsModal.tsx; examples/plugins/dp_sql_catalog`

---

## §6 平台 · 运维与部署

### §6.1 应用与服务 · 5✅
- ✅ **dataplay 一条命令启动器 / CLI** — --host/port/workspace/data-dir/no-open/no-seed；播种、配 workspace、uvicorn、开浏览器；workspace 环境变量在 import settings 前导出，故 --workspace 真隔离元数据库/目录/限定 root。 `kernel/hub/cli.py:15-56`
- ✅ **启动安全护栏（拒非环回免认证绑定 + 日志）** — 免认证下绑非 127.0.0.1 直接拒（除非 DP_AUTH_SECRET 或 DP_ALLOW_INSECURE_BIND=1），否则等于全网任意代码/文件访问；默认输出日志（不再静默），级别走 DP_LOG_LEVEL（归一化非法值不崩 uvicorn）。 `kernel/hub/cli.py:36-67`
- ✅ **FastAPI 应用工厂 + 路由拆分** — catalog/runs/workspace router；public_router 不门控，其余 Depends(current_user)。 `↗ 门控语义见 §4.3` `kernel/hub/main.py:37-54`
- ✅ **SPA 服务（单进程，打包 + 开发回退）** — 存在则服务 kernel/_web（wheel）否则 web/dist；pyproject force-include ../web/dist。 `kernel/hub/main.py:145-149`
- ✅ **CORS 限制为 localhost 来源** — allow_origin_regex 只 localhost/127.0.0.1（无通配符）；跨域部署需放宽。 `kernel/hub/main.py:41-45`

### §6.2 设置 · 4✅
- ✅ **全局设置页面（agent / execution / destinations）** — 全屏带左侧导航；覆盖 agent model/key/baseURL、runner 选择、destinations + 对象存储凭证。 `web/src/panels/SettingsModal.tsx:23-154`
- ✅ **用户级设置** — scope='user' 接进 UI：Execution runner 是 per-user 偏好，pick_runner 用户优先再回退 global；主题走 localStorage（无闪烁）。 `↗ pick_runner 见 §2.3` `web/src/panels/SettingsModal.tsx; deps.py pick_runner(plan,uid)`
- ✅ **设置 API 密文脱敏** — GET 把 agentApiKey + objectStore keys 脱敏为哨兵，PUT 把哨兵当"未改动"，点点点不覆盖密文。 `kernel/hub/routers/workspace.py:226-262`
- ✅ **用户管理（Members UI 创建用户）** — Settings→Members 列出 + 创建（姓名 + 可选初始密码，走 POST /users，刷新名单）；密码轮换自助，无 admin 重置端点（否则可劫持）。 `↗ 后端见 §4.3` `web/src/panels/SettingsModal.tsx(Members); api.createUser`

### §6.3 元数据与迁移 · 2✅
- ✅ **元数据 DB（SQLAlchemy；SQLite 开发 / Postgres 生产）** — 用户/画布/分享/运行记录+状态/版本/目录条目+边/设置；仅连接串可配（DP_DATABASE_URL）。 `kernel/hub/metadb.py:39-154`
- ✅ **Alembic 迁移（含遗留 DB 收编）** — 11 revision 各带 upgrade+downgrade；init_db 给 Alembic 前的 DB 打 baseline 印记；Alembic 是 schema 唯一真相源。 `kernel/hub/metadb.py:213 init_db; migrations/0001..0011`

### §6.4 设计系统 · 2✅ 1🟡
- ✅ **亮/暗主题切换（跟随系统，无闪烁）** — light/dark/system，localStorage，data-theme 首帧前应用；TopBar 切换。 `web/src/theme/mode.ts:9-43`
- ✅ **设计 token（shadcn HSL 变量 + TS 镜像）** — shadcn 原语 + token + 亮暗主题到位；组件读主题感知 token 层。 `web/src/index.css:11-107; theme/tokens.ts:8-99`
- 🟡 **shadcn/ui 迁移** — Radix+cva 原语已存在、新面板在用，但迁移未完；仍有内联样式/遗留变量组件。 `web/src/components/ui/*.tsx`

### §6.5 Agent（LLM）· 2✅ 1⬜ · `↗ dock UI 见 §1.6`
- ✅ **LLM agent（供应商无关、进程内、服务端持 key）** — Pydantic AI tool-use 循环（add/connect/set_config/preview）；模型经 DP_AGENT_MODEL/DB 设置；key 留 kernel；请求上限约束；新节点自动布局。 `kernel/hub/agent.py:99-257`
- ✅ **关系感知工具（catalog+键 / join_hints / validate）** — 声明式选择交给 LLM：`list_catalog` 带主键候选+行数；`join_hints(a,b)` 给实测基数的候选键+已声明关系；`validate` 出类型错误+每个 join 的扇出/基数校验。LLM 翻译模糊意图，工具供事实+校验（非 rule-based 规划器）。 `kernel/hub/agent.py list_catalog/join_hints/validate`
- ⬜ **Agent 离线关键词规划器兜底** — 有意为之、非缺陷：不做假装 LLM 的规则替身；无模型时 dock unavailable，其余照常。 `web/src/panels/AgentDock.tsx; README.md`

### §6.6 无状态 web / 扩展部署 · 4✅ 1🟡 · `↗ 执行器 §2.3 · 目录 §3.3 · 协作 §4.1`
- ✅ **共享运行状态** — RunState 表镜像每次转换；GET /run 与状态 WS 可从任意实例回答，重启存续。 `kernel/hub/metadb.py:99 RunState, 407 save_run_state`
- ✅ **共享目录写穿** — catalog_entries/edges 持久化数据集/输出/血缘；每实例缓存写穿 + 从 DB 加载。 `kernel/hub/metadb.py:110-129,366-398`
- ✅ **离线 / 零配置运行** — 无需云账号；内置 SQLite；首次播种样本；引擎依赖全本地；Agent+Postgres+Lance 是可选 extra。 `README.md:8-9,26`
- ✅ **部署文档 + Docker** — Dockerfile 单镜像（冒烟测试）+ docker-compose（Postgres + 卷 + 认证/数据集 root，deploy.replicas + sticky routing）；TLS/反代留运维。 `Dockerfile; docker-compose.yml; README 'Run with Docker'`
- 🟡 **分布式 / 解耦执行** — **默认**每画布常驻 kernel（见 §2.3 KernelBackend）已让 web/hub 层可重启/重部署而不打断在飞 run，run/preview/profile 全走 kernel + 预览热缓存；心跳门控 reaper 取代了"单实例假设"的旧 reconcile。**跨机底座可插拔**：`KernelSpawner` SPI（spawn/kill）+ 内置本地进程 spawner；`DP_KERNEL_SPAWNER=pod` 换成**参考 PodSpawner**（每画布一个 k8s Pod+Service，kernel 绑 0.0.0.0 + 广播 Service DNS，任意 hub 按名删 pod → 解决单机 SIGKILL 限制）。PodSpawner 是**参考实现**（清单生成有测试；RBAC/镜像/数据挂载是运维事，未在真集群验证）→ 仍标 🟡。 `kernel/hub/pod_spawner.py; kernel_backend.py; backends.py KernelSpawner`
