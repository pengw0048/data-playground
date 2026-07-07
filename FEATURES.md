# Data Playground — 功能清单（架构树）

_验收清单，按架构层次组织：**层 → 组件 → 角色**（接口 / 实现 / 选择判断 / 生命周期 / UI / 安全 / 持久化…）。每个叶子由 `file:line` 佐证。图例：✅ 已实现 · 🟡 部分实现（见备注） · ⬜ 未实现（脚手架 / 规划中 / 有意省略）。跨层复用的组件只写一处，别处用 `↗ 见 §X` 交叉引用（交叉引用行不带状态图标、不计数）。_

_最后更新：2026-07-06。**142 项功能**（旧扁平清单 142 条里的跨层重复已合并为交叉引用；移除了死掉的 branch 路由）—— ✅ 126 · 🟡 13 · ⬜ 3。_

**层次总览**

| §  | 层 | 说明 |
|----|----|----|
| §1 | 前端 · 画布与交互 | React Flow 画布、节点卡片、端口连线、Section 容器、Agent dock |
| §2 | 内核 · 执行引擎 | 下降引擎、预览、执行器（接口/实现/选择/生命周期）、计算节点 |
| §3 | 数据 · 适配器与目录 | 适配器、写入、目录、目标位置、向量搜索、数据面安全、关系与 join 提示 |
| §4 | 协作 · 多用户与认证 | 实时协作、身份认证、授权分享、WS 门控、持久化 |
| §5 | 扩展点 · 插件 SPI | 发现/版本、节点/适配器/执行器/目录 SPI、能力、导入器 |
| §6 | 平台 · 运维与部署 | 应用服务、设置、元数据、设计系统、Agent、无状态 web/部署 |

## ⚠️ 尚未完全完成（13 🟡 + 3 ⬜，验收重点）

- ⬜ 控制流节点 branch/loop/variable（§1.3，有意省略）· Agent 离线规划器（§6.5，有意省略）· 分布式执行（§6.6，基建）
- 🟡 能力查看器标签页（§1.2）· Agent dock 需配模型（§1.6）· 运行取消纯 Python 循环（§2.3）· 成本门控（§2.3）· 独立 loop 节点（§2.4）· Arrow/Feather 扫描（§3.1）· 原子覆盖写-对象存储（§3.2）· 对象存储浏览（§3.4）· 实时协作 sticky-routing（§4.1）· Capabilities 插件扩展（§5.6）· 导入器 501（§5.7）· pod/Ray 内置插件（§2.3，参考 pool 后端已内置）· shadcn 迁移（§6.4）

---

## §1 前端 · 画布与交互

### §1.1 画布骨架 · 3✅
- ✅ **React Flow 节点画布** — 点状背景、panOnScroll、min/max zoom、fitView；本地 rfNodes 从 store 协调，保留 RF 的 measured/width。 `web/src/canvas/Canvas.tsx:268`
- ✅ **空画布状态** — doc.nodes 为空时提示 'Add a source' + 'Ask the Agent'。 `web/src/canvas/Canvas.tsx:41`
- ✅ **小地图 + 缩放控件** — 控件叠在可平移 MiniMap 上，节点按类型色着色、点击重居中。 `web/src/canvas/Canvas.tsx:296`

### §1.2 节点卡片与渲染 · 5✅ 1🟡
- ✅ **统一的节点卡片** — 强调色条/状态字形/可编辑标题/类型标签/元信息/紧凑主体/悬停操作栏；所有手工类型走它。 `web/src/nodes/NodeCard.tsx:79`
- ✅ **节点状态字形（draft/latest/stale/running/failed）** — 编辑标记下游 stale，完成翻 latest 并快照。 `web/src/theme/tokens.ts:74`
- ✅ **内联节点重命名** — 双击标题 / ⋯ 菜单 Rename，Enter 提交 Esc 还原。 `web/src/nodes/NodeCard.tsx:207`
- ✅ **每节点操作栏（预览/运行/历史/代码/⋯）** — 悬停/唯一选中/运行中出现；操作带理由禁用。 `web/src/nodes/NodeCard.tsx:156`
- ✅ **schema 感知的列选择器字段** — ColumnCombo + useInputColumns 从输出 schema 喂类型化列建议。 `web/src/nodes/fields.tsx:1`
- 🟡 **能力驱动的查看器标签页** — media 能力加图片网格标签页；只有 media 内置，vectors 标签页已有意移除（注册机制真实但内置集小）。 `↗ 详见 §5.6 Capabilities` `web/src/nodes/capabilities.tsx:42`

### §1.3 节点注册与类型 · 3✅ 1⬜
- ✅ **节点 + 能力注册表（插件模型）** — register(spec,component)；glob kinds/*.tsx，加类型=加文件。 `web/src/nodes/registry.ts:32`
- ✅ **schema 驱动的通用节点（backend/插件类型）** — registerGenericNodes 无前端代码渲染任何 /api/nodes 类型。 `↗ 详见 §5.2 通用渲染` `web/src/nodes/generic.tsx:97`
- ✅ **内置节点类型（source/sample/filter/select/transform/sql/join/aggregate/sort/dedup/write/metric/vector-search/section/note/code）** — 16 个手工卡片，nodespecs.py 镜像计算类型。 `web/src/nodes/kinds/source.tsx:94`
- ⬜ **branch / loop / variable 控制流节点** — 有意省略、非缺口：控制流统一由 section 驱动脚本承担。branch 连引擎里残留的 `_route_branch` 路由机器件也已一并移除(不可达死代码);残留的 'control' 工具栏分类无节点注册,空分类被丢弃不显示。 `web/src/theme/tokens.ts:54`

### §1.4 端口与连线 · 4✅
- ✅ **类型化端口 + 连线类型** — 形状+色调编码 dataset/selection/sample/sql-view/metric/value；多输出经 config.outputs。 `web/src/nodes/Port.tsx:13`
- ✅ **连接校验（类型化、单输入、join 双输入）** — isValidConnection 检查 accepts + 拒绝已占用 handle。 `web/src/canvas/Canvas.tsx:158`
- ✅ **从端口连出的添加节点菜单** — 输出端口点击开菜单，过滤能接受该连线的类型并接好。 `web/src/canvas/ConnectMenu.tsx:7`
- ✅ **独立的连线删除 / 重连** — 双击删边（可撤销）；端点拖到空闲端口重连（onReconnect）。 `web/src/canvas/Canvas.tsx:293,182`

### §1.5 选择与画布编辑 · 7✅
- ✅ **添加节点工具栏（按分类分组）** — 底部悬浮，从注册表自动填充，portal 弹层，freePosition 不重叠；空分类丢弃。 `web/src/canvas/Toolbar.tsx:19`
- ✅ **选择 + 框选 + shift/meta 多选** — selectionOnDrag + panOnDrag[1,2]；Cmd/Ctrl+A 全选。 `web/src/canvas/Canvas.tsx:288`
- ✅ **带最终位置提交的节点拖拽** — 每帧本地，仅最终位置（dragging:false）作为一个撤销步提交，避免洪泛。 `web/src/canvas/Canvas.tsx:128`
- ✅ **复制/粘贴/剪切/复制副本（单个 + 子图）** — cloneSubgraph 重映射 id、保留内部连线、偏移位置；应用内剪贴板非 OS。 `web/src/store/graph.ts:476`
- ✅ **键盘快捷键** — Delete / B 旁路 / D 禁用 / Cmd A C X V D / Z 重做 / Esc；输入框与全屏编辑器上抑制。 `web/src/canvas/Canvas.tsx:216`
- ✅ **旁路（直通）** — 切 data.bypassed，虚线外框，spec.canBypass 门控。 `web/src/store/graph.ts:535`
- ✅ **禁用 + 下游传播** — isDisabled 向上游遍历，关闭整条下游分支（变暗、DISABLED 徽章、阻止运行）。 `web/src/store/graph.ts:545`

### §1.6 复合与协作（前端侧）· 1✅ 1🟡
- ✅ **Section 容器（嵌套 / 拖入拖出 / 驱动脚本）** — 有尺寸框架，屏幕空间重叠命中成 parentId 子节点；视觉拖拽嵌套刻意单层（框固定尺寸），更深嵌套用驱动脚本。 `↗ 执行见 §2.4 Section 元编程` `web/src/nodes/kinds/section.tsx`
- 🟡 **Agent dock（一次对话，模型自己决定答还是建）** — 无 Plan/Build 模式；模型每条消息自行决定纯文字答 or 调改图工具（add/connect/set_config），前端仅在真改图时应用+跑终端；需配 DP_AGENT_MODEL，否则 unavailable。 `↗ 详见 §6.5 LLM agent` `web/src/panels/AgentDock.tsx`
- **画布光标 / 在线状态** — 在画布上渲染对等端光标 + 在线状态（PeerCursors 映射到各视口）。 `↗ 见 §4.1 实时协作`

---

## §2 内核 · 执行引擎

### §2.1 下降引擎（LoweringEngine）· 2✅
- **接口：NodeLowering 协议** — 引擎经 node_lowerings 分派插件类型（单输出 Relation / 多输出 {port:Relation}）。 `↗ 契约叶子见 §5.2 节点 SPI`
- ✅ **图 → DuckDB 关系 plan** — 每关系节点降为 DuckDBPyRelation；多输出 {port->Relation} 按 source_handle 路由。 `kernel/kernel/executors/engine.py:150`
- ✅ **核外执行（DuckDB 流式 + 溢出磁盘）** — 关系算子原生流式/溢出；temp_directory 显式设为 DP_SPILL_DIR（运维可控）、DP_MEMORY_LIMIT 可限内存；Python transform 溢出 Parquet 再读回，runner GC。**benchmark 背书**：240M 行/4.7GB 在 1.3GiB 上限下排序、溢出 4.9GB、峰值 RSS 仅 2.3GB。 `kernel/kernel/db.py _apply_session; engine.py:364; docs/BENCHMARK.md`

### §2.2 预览与 schema · 3✅
- ✅ **忠实的样本预览（join/sort/vector 对完整输入运行）** — 这些算子预览时以未采样子引擎下降，LIMIT 成诚实 top-N；预览预算 2000 行。 `kernel/kernel/executors/engine.py:118`
- ✅ **NotPreviewable 的诚实性** — 干净区分"需完整遍历"与真错误；尊重 spec.previewable + transform 模式。 `kernel/kernel/executors/engine.py:30; preview.py:55`
- ✅ **每节点输出 schema（类型化 vs 未类型化端口）** — 元数据 only 解析关系列，code 算子返回 null；自己的游标、无超时。 `kernel/kernel/executors/schema.py:20`

### §2.3 执行器（ExecutionBackend）· 8✅ 3🟡 · `↗ per-user 选择见 §6.2`
**接口** — `↗ ExecutionBackend 协议契约叶子见 §5.4 执行器 SPI`

**实现**
- ✅ **LocalRunner（本地核外）** — 后台守护线程、进程内运行，发每节点状态转换。 `kernel/kernel/plugins/runner.py:34`
- ✅ **SubprocessRunner（进程隔离）** — 真 OS 进程隔离，崩溃/OOM 不拖垮 kernel，取消=硬 terminate()；**auth 模式下默认走它**（多用户隔离）。 `kernel/kernel/subprocess_runner.py:28`
- 🟡 **pod/Ray/队列 runner** — 已内置**参考多 worker pool 后端**（`DP_POOL_WORKERS` 开启，能力化放置 + RunController + placement planner，见 kernel/pool_runner.py · run_controller.py · placement.py）；k8s-pod/Ray 仍是插件扩展点。 `kernel/kernel/deps.py:155-164` `↗ 部署见 §6.6`

**选择**
- ✅ **pick_runner（含多用户隔离默认）** — 尊重 Settings 的 backend + per-user 偏好（空=继承）；**无显式选择且 auth 开启时默认 subprocess**（开放模式保持进程内），再取首个 can_run；run_index 路由 status/cancel。 `kernel/kernel/deps.py:135-147; test_auth_mode_defaults_to_subprocess_runner_for_isolation`

**生命周期**
- ✅ **每运行的 DuckDB 游标隔离（db.run_scope）** — 并发运行/预览不再串行在全局锁；退出只回滚 + 清自己的视图。 `kernel/kernel/db.py:84`
- ✅ **运行估算** — 粗略而诚实，不编造每算子 ETA；无源可计数报 'size unknown'。 `kernel/kernel/plugins/runner.py:62`
- ✅ **实时状态 + 运行历史持久化（跨实例/重启安全）** — on_status→run_states，_status_or_lost 回退，不返回 404。 `↗ 部署见 §6.6` `kernel/kernel/plugins/runner.py:110; routers/runs.py:184`
- ✅ **启动时协调孤儿运行** — 非终态→failed('interrupted')；单实例正确，多实例见分布式执行缺口（§6.6）。 `kernel/kernel/metadb.py:401-419`
- 🟡 **运行取消 + 查询中断** — 步骤间可取消 + scope.interrupt() 中断游标查询；纯 Python 死循环只有 subprocess kill 能停。 `kernel/kernel/plugins/runner.py:268; db.py:75`
- ✅ **内容寻址缓存（未变更 plan 走缓存，持久化 + 跨实例）** — DB 支撑的 `result_cache` 表（迁移 0008），跨运行/重启/实例复用；plan 内容哈希 → 输出 uri，非可缓存 plan(对象存储源/append/library/plugin)不缓存；进程内 dict 仅未接线 fallback。 `kernel/kernel/deps.py:146; kernel/kernel/metadb.py get_result/put_result; migrations/versions/0008_result_cache.py`
- 🟡 **成本 / 确认门控** — 仅固定源行数阈值（5M），无字节/成本模型；大小未知→放行（HTTP 409 除非 confirmed）。 `kernel/kernel/plugins/runner.py:72; routers/runs.py:168`

### §2.4 计算节点执行 · 4✅ 1🟡
- ✅ **Transform 逃生舱（在 Arrow RecordBatches 上跑 Python）** — map/filter/flat_map/map_batches；onError='skip' 丢失败行；完整运行溢出 Parquet。 `kernel/kernel/executors/engine.py:331`
- ✅ **用户单元格代码的软沙箱** — 软性防护非安全边界；builtins 白名单 + import 白名单 + AST 拒 dunder + 墙钟超时。多用户真正的隔离靠 auth 模式默认的 subprocess runner（崩溃/DoS 隔离，非多租户牢笼；见 §2.3、README 多用户隔离）。 `kernel/kernel/sandbox.py:82`
- ✅ **Section 元编程（驱动脚本复合节点）** — 只完整遍历，每次迭代物化 Parquet + GC；run() 携带 parentId 子树以支持嵌套 section。 `↗ 容器 UI 见 §1.6` `kernel/kernel/section.py:75`
- ✅ **向量搜索引擎入口** — 查询向量来自配置或选中行，预览也在完整输入（忠实）；裸 Lance source 走原生 ANN 否则暴力余弦。 `↗ 详见 §3.5` `kernel/kernel/executors/engine.py:415`
- 🟡 **独立的 loop 节点** — 裸 loop 是直通占位符，真迭代走 section；环路一开始被拒（必须封装）。 `↗ §1.3 控制流` `kernel/kernel/executors/engine.py:307`

---

## §3 数据 · 适配器与目录

### §3.1 数据集适配器（DatasetAdapter）· 9✅ 1🟡 · `↗ SPI 契约见 §5.3`
**DuckDB 适配器 — 扫描格式**
- ✅ **Parquet 扫描（惰性/核外）** — 默认读取器；也处理 parquet 分片的对象存储前缀。 `kernel/kernel/plugins/adapters.py:151`
- ✅ **CSV/TSV 扫描** `kernel/kernel/plugins/adapters.py:144`
- ✅ **JSON/NDJSON 扫描** `kernel/kernel/plugins/adapters.py:146`
- 🟡 **Arrow/Feather/IPC 扫描** — 仅本地文件（pyarrow 读入 DuckDB）；无对象存储路径、不支持追加。 `kernel/kernel/plugins/adapters.py:148`
- ✅ **文件目录扫描（part-*.<ext> 数据集）** — 扫描追加模式写的目录，递归 glob 覆盖 parquet/pq/csv/tsv/json。 `kernel/kernel/plugins/adapters.py:153`
- ✅ **CSV 解析选项（分隔符/表头覆盖，否则自动检测）** — source 节点只在设置时传覆盖；分隔符接受 'tab'。 `kernel/kernel/plugins/adapters.py:61`
- ✅ **mem:// 适配器（内存态命名表）** — 把进程内 DuckDB 表暴露为数据集（测试/fixture）。 `kernel/kernel/plugins/adapters.py:128`

**Lance 适配器**
- ✅ **Lance 流式扫描（列/limit 下推）** — 需 lance extra；列/limit 下推，谓词扫描后于 DuckDB 应用。 `kernel/kernel/plugins/adapters.py:246`

**对象存储**
- ✅ **对象存储扫描/写入（s3/gs/gcs/r2）经 DuckDB httpfs** — httpfs 在 ensure_object_store 显式加载。 `kernel/kernel/plugins/adapters.py:130`
- ✅ **对象存储凭证（显式 key / AWS 链 / MinIO·R2 自定义 endpoint）** — CREATE SECRET，回退 credential_chain。 `kernel/kernel/db.py:138`

### §3.2 写入 · 4✅ 1🟡
- ✅ **写入格式 parquet/csv/tsv/json/arrow/lance** — 按扩展名；JSON 经 COPY，Lance 经流式 record batch。 `kernel/kernel/plugins/adapters.py:207`
- ✅ **写入模式覆盖** `kernel/kernel/plugins/adapters.py:199`
- ✅ **写入模式追加（分片文件目录）** — parquet/csv/tsv/json + Lance 原生；每次写 part-*.<ext>，_read_dir glob 读回；Arrow/Feather 有意不支持追加。 `kernel/kernel/plugins/adapters.py write(append)+_read_dir`
- ✅ **内容寻址的写入跳过（幂等覆盖重跑）** — 相同覆盖 plan 已产出且仍在→跳过重写；追加从不缓存。 `kernel/kernel/plugins/runner.py:218`
- 🟡 **原子覆盖写（临时文件 + os.replace）** — 仅本地写入原子；对象存储覆盖是就地写（依赖单对象 PUT 原子性，多文件/目录非事务）。 `kernel/kernel/plugins/adapters.py:203`

### §3.3 目录（Catalog）· 6✅ · `↗ SPI 契约见 §5.5`
- ✅ **list / get / search** — 内存态 RLock 串行；搜索匹配 name/uri 子串。 `kernel/kernel/plugins/catalog.py:101`
- ✅ **血缘（围绕某 uri 的连通分量图）** — 去重父/子边 BFS；遍历前合并其他实例的边。 `kernel/kernel/plugins/catalog.py:122`
- ✅ **启动时从本地数据目录播种目录** — 发现 parquet/csv/tsv/json/arrow + .lance 目录。 `kernel/kernel/plugins/catalog.py:39`
- ✅ **目录注册（跨重启持久化）** — adapter.schema 校验可读，存 datasets 设置以便重启重注册。 `kernel/kernel/routers/catalog.py:75`
- ✅ **register_output（write 节点注册输出 + 血缘边）** — 提交写入后用父 uri + pipeline='canvas' 注册。 `kernel/kernel/plugins/runner.py:253`
- ✅ **DB 支撑/跨实例（catalog_entries + catalog_edges）** — 写穿 + 读时 _load_from_db 合并，跨实例可见；尽力而为，每次全量重合（大目录扩展性问题）。 `↗ 部署见 §6.6` `kernel/kernel/plugins/catalog.py:82`

### §3.4 目标位置（Destinations）· 2✅ 1🟡
- ✅ **本地 backend 浏览 + mkdir（受 root 限定）** — realpath 阻止越界遍历；.lance 目录以文件显示。 `kernel/kernel/destinations.py:27`
- ✅ **目标预设（全局设置 + 默认 workspace 输出）** — 始终注入默认 'outputs'；DP_STORAGE_URL 可设为 s3/gs 前缀。 `kernel/kernel/destinations.py:110`
- 🟡 **对象存储 backend 浏览 + target_uri** — glob 浏览前缀；对象存储 mkdir 空操作（无真文件夹）；凭证/桶缺失如实报错。 `kernel/kernel/destinations.py:61`

### §3.5 向量搜索 · 3✅
- ✅ **Lance 原生 ANN（有向量索引则用）** — 仅裸 .lance source；需 lance extra；错误回退暴力余弦；_score=1-距离。 `↗ adapter 契约见 §5.3` `kernel/kernel/executors/engine.py:447`
- ✅ **DuckDB 上暴力余弦** — list_cosine_similarity ORDER BY DESC LIMIT k；固定大小数值 list 列；完整输入忠实 top-K。 `kernel/kernel/executors/engine.py:453`
- ✅ **按外部向量或按行索引查询** — queryVector 接 JSON/list，否则 queryRow 用数据集偏移。 `kernel/kernel/executors/engine.py:428`

### §3.6 数据面安全 · 2✅
- ✅ **SSRF 防护（禁用 DuckDB 扩展 autoload/autoinstall）** — 阻止任意 https:// uri 静默拉 httpfs 取远程数据，每运行游标重断言；对象存储显式加载 httpfs。 `kernel/kernel/db.py:42`
- ✅ **本地路径的数据集 URI 限定** — 认证模式下本地路径（register/sample/source 下降）必须在允许 root 内，越界 403；对象存储 URI 不受影响，开放单用户模式不限定。 `kernel/kernel/paths.py:24`

### §3.7 关系与 join 提示（catalog 驱动）· 9✅
- ✅ **键检测（key capability + 复合键模型）** — id/uuid/*_id/*_key 命名 + 可 join 类型标 `key` 能力（media/vector 列不算键）；每表推断单列 PK 候选，`KeyInfo` 模型支持复合键（join 时形成组合候选）。 `kernel/kernel/plugins/capabilities.py:36; kernel/kernel/relationships.py:33`
- ✅ **基数实测（DuckDB 单趟 count/distinct）** — `count(*)` 与 `count(DISTINCT key)` 一趟聚合出唯一性（一次扫描，不耗尽 Lance 的一次性 Arrow reader）；唯一侧=父（1），非唯一=子（N）；不可测/空数据返回 None→'unknown'（绝不谎报），在 run_scope 游标上跑不占基连接锁。 `kernel/kernel/relationships.py:56`
- ✅ **join 提示（两数据集端点）** — 匹配键列（同名或 id↔*_id + 类型兼容）+ 实测基数→排序建议（有父侧/精确名/窄键优先）；复合候选上限防组合爆炸 + 记忆化。 `POST /api/catalog/join-suggestions · kernel/kernel/relationships.py:150`
- ✅ **画布 join-analysis（建议 + fan-out 警告）** — 为 join 节点两输入出建议 + 非 1:1 时的扇出警告；left/right 按 incoming 边序解析（与引擎 a/b 别名一致）。 `POST /api/graph/join-analysis · kernel/kernel/relationships.py:186`
- ✅ **grain 传播（键集经 relational ops）** — 源的 PK 经 filter/sample/sort 保留、group-by/dedup 重定 grain（唯一）、code/sql/section/join 变未知（诚实）；重命名/派生的 select 丢弃 grain（bare 直传才保留）；这就是"采样/聚合后仍可 join"背后的事实。 `kernel/kernel/grain.py:41`
- ✅ **前端 join hints（inspector）** — 防抖拉取建议、基数徐章（1:1/1:N/N:M，明暗主题）、扇出警告条、点击建议填 `on`（同名 USING）/`condition`（异名 a.x=b.y）。 `web/src/panels/Inspector.tsx JoinHints`
- ✅ **声明式主键（不透明 transform 的出路）** — `PUT /catalog/tables/{id}/key` 设/清声明键，领先推断键、grain 中胜出（declared>verified>inferred）；校验列存在；去重推断孪生；**每 uri 一行**持久化（catalog_declared_keys 表）跨实例、无 blob 丢更新。 `kernel/kernel/plugins/catalog.py set_declared_key; kernel/kernel/routers/catalog.py declare_key`
- ✅ **声明式关系（ER 边）** — `GET/POST /catalog/relationships` + `.../delete`，复合可表达，**每关系一行**持久化（catalog_relationships 表，orientation-insensitive key upsert）跨实例、无 blob 丢更新；join-analysis 中声明关系领先实测（存反了自动翻转基数）。 `kernel/kernel/plugins/catalog.py relationships; kernel/kernel/relationships.py _declared_suggestion`
- ✅ **ER / UML 视图（Relationships 视图）** — React Flow 实体图：每数据集一实体（列 + 🔑 主键徽章），声明关系实线带基数、命名候选虚线（仅 FK 式匹配，非裸 id↔id）；点列声明主键（点多列=复合键）、拖两表弹选择器声明 join（挑建议键或手选列+基数）、点边删除；明暗主题、布局 localStorage 持久化。 `web/src/views/ERDiagram.tsx; web/src/views/Shell.tsx`

---

## §4 协作 · 多用户与认证

### §4.1 实时协作 · 6✅ 1🟡 · `↗ 画布光标 UI 见 §1.6`
- ✅ **实时协同编辑（Yjs CRDT）** — 真 CRDT 合并（nodes/edges/meta），非最后写入者赢；笨中继无服务端 Y.Doc；'房间第一个' 800ms 计时器启发式。 `web/src/collab/ydoc.ts:50-113`
- ✅ **在线状态 / 对等端光标** — 光标色按浏览器会话随机、不绑身份；sendCursor 50ms 节流；PeerCursors 映射到各视口。 `web/src/collab/collab.ts:92-96; PeerCursors.tsx`
- ✅ **协作通道 viewer 角色写入门控** — 丢弃 viewer 的入站 yjs 更新（仍中继在线状态），堵住越只读边界洗白。 `kernel/kernel/main.py:115-116`
- ✅ **CRDT 感知的协作式撤销/重做** — 撤销只回退自己的更改、不删对等端并发加的节点；离线回退自身快照栈。 `web/src/collab/ydoc.ts:99-104`
- ✅ **协作写放大防护** — 只发起编辑者 PUT（对等端合并编辑不 PUT），离线缓存仍无条件写。 `web/src/collab/undo.ts:23; graph.ts:870`
- ✅ **标签页关闭 pagehide 刷新** — 唯一发起编辑者在 400ms 防抖窗口内断连时兜底 keepalive PUT。 `web/src/store/graph.ts:906-911`
- 🟡 **协作 WebSocket 中继（按画布分房间）** — 每实例内存态房间；多实例需 sticky-route 每画布到单实例，否则对等端互不可见；单进程 OK。 `↗ 部署见 §6.6` `kernel/kernel/main.py:88-135`

### §4.2 身份与认证 · 4✅
- ✅ **按用户的身份（scrypt 密码 + 签名会话 cookie）** — scrypt$salt$hash（仅标准库）；HMAC 签名 7 天 TTL token；DP_AUTH_PASSWORD 只引导默认用户；httponly+samesite=lax；DP_AUTH_SECRET 开启否则 X-DP-User 开发模式。 `kernel/kernel/auth.py:38-83; routers/workspace.py:31-56`
- ✅ **自助改密/轮换密码** — /auth/password 需旧密码、min 6；Shell 改密对话框。 `kernel/kernel/routers/workspace.py:46-56; web/src/views/Shell.tsx:72-119`
- ✅ **X-DP-User 开放（免认证）开发模式** — 无 DP_AUTH_SECRET 时信任 header=用户；认证开启后明确不信任。 `kernel/kernel/security.py:21-26`
- ✅ **登录界面 + 登录名单** — 用户选择器 + 密码；无 SSO/OIDC；public GET /users 只回 id+name 无邮箱。 `web/src/views/Login.tsx:10-21`

### §4.3 授权与分享 · 4✅ · `↗ 门控实现见 §6.1`
- ✅ **安全默认的 /api 认证门控（路由级 Depends）** — include 时施加，新路由默认受保护，除非挂 public_router；修复了 /run,/data,/catalog 门户大开的时代。 `kernel/kernel/main.py:50-54`
- ✅ **画布分享（可见性 private/workspace/workspace_view + 显式协作者）** — workspace_view=人人只读；canvas_role 对其回 viewer；add_share 校验值（未知 400）。 `kernel/kernel/metadb.py canvas_role; ShareModal.tsx`
- ✅ **画布访问控制/授权（owner/editor/viewer）** — put_canvas 403 非 editor；delete 仅 owner；editor 不能再分享；前端 403→view-only 提示。 `kernel/kernel/routers/workspace.py:129-147`
- ✅ **管理员创建用户（后端）** — 无自助注册；无角色区分，任何认证用户可建（POST /users 仅 current_user 门控）。 `↗ UI 见 §6.2 Members` `kernel/kernel/routers/workspace.py:87-94`

### §4.4 认证门控的 WebSocket · 2✅
- ✅ **协作 WS 认证门控** — 开放模式不门控；否则验 dp_session + canvas_role，无角色 close 1008。 `kernel/kernel/main.py:97-103`
- ✅ **运行状态 WS 认证门控** — 无有效会话 close 1008。 `kernel/kernel/main.py:69-71`

### §4.5 持久化（协作面）· 3✅ · `↗ metadb 见 §6.3`
- ✅ **自动保存（防抖 PUT）+ 离线缓存** — 400ms 防抖 PUT + 无条件 localStorage 缓存；区分 403（view-only）与离线。 `web/src/store/graph.ts:859-898`
- ✅ **画布版本历史 + 恢复** — 节流自动快照（90s 去重、留最新 30、命名快照永留）+ 恢复先快照当前（可撤销）。 `kernel/kernel/metadb.py:422-461; workspace.py:150-177`
- ✅ **运行历史（按画布，持久化）** — RunRecord 表存活重启；临时画布空操作；同授权 GET /canvas/{id}/runs。 `kernel/kernel/metadb.py:306-340`

---

## §5 扩展点 · 插件 SPI

### §5.1 插件发现与版本 · 5✅
- ✅ **发现：拖入式 workspace 文件夹** — 扫 <workspace>/plugins/<pack>/ 带 register(reg)，加 sys.path，先读 dataplay.toml。 `kernel/kernel/deps.py:150-161`
- ✅ **发现：pip entry-points（dataplay.plugins 组）** — 加载并调 ep.load()(reg)；无测试覆盖，min_core_api 不应用到 entry-points。 `kernel/kernel/deps.py:165-174`
- ✅ **发现：DP_PLUGINS 环境变量模块列表** — 逗号分隔模块导入并 register。 `kernel/kernel/settings.py:20; deps.py:163-164`
- ✅ **版本协商（min_core_api vs CORE_API_VERSION）** — 校验 dataplay.toml；要求更新核心的包记错不加载；仅对拖入式强制。 `kernel/kernel/deps.py:176-201`
- ✅ **插件自省端点（/api/plugins, /api/kernel）** — 列已加载包的 source/version/error；加载失败被捕获不崩溃。 `kernel/kernel/routers/catalog.py:46-48; deps.py:216-223`

### §5.2 节点 SPI · 4✅ · `↗ 下降协议实现见 §2.1`
- ✅ **节点编写 SDK（add_node + ctx 构建器 sql/arrow_map/polars）** — ctx.sql 用 {input} 占位视图；add_node 拒绝遮蔽内置/已注册类型。 `kernel/kernel/sdk.py:38-70`
- ✅ **NodeLowering 协议（插件下降契约）** — runtime_checkable，单输出 Relation / 多输出 {port:Relation}。 `↗ 引擎侧见 §2.1` `kernel/kernel/backends.py:23-36`
- ✅ **NodeSpec 通用前端渲染（插件节点无需前端代码）** — 类型化端口/参数 + code 参数编辑器（片段按钮开全屏编辑器）；示例插件端到端验证。 `web/src/nodes/generic.tsx`
- ✅ **前后端节点 spec 一致性守卫** — 测试解析 kinds/*.tsx register 字面量比对 BUILTIN_NODE_SPECS，守漂移；通用渲染类型跳过。 `kernel/kernel/tests/test_kernel.py:1263-1334`

### §5.3 适配器 SPI · 2✅ · `↗ 内置实现见 §3.1/§3.5`
- ✅ **数据集适配器 SPI（add_adapter；DuckDB + Lance 内置）** — 完整 matches/scan/schema/count/fingerprint/write；插件 insert(0) 抢认领，resolve_adapter 回退 DuckDB。 `kernel/kernel/plugins/adapters.py:99-291`
- ✅ **Lance 原生 ANN（可选的 adapter.nearest）** — 余弦 kNN 下推 Lance；缺 nearest 时通用 vector-search 回退暴力余弦。 `kernel/kernel/plugins/adapters.py:257-264`

### §5.4 执行器 SPI · 1✅ · `↗ 内置实现/选择/生命周期见 §2.3`
- ✅ **ExecutionBackend SPI（add_runner；替代 runner）** — runtime_checkable 协议；内置 Local + Subprocess；pick_runner 按名字选、run_index 路由。 `kernel/kernel/backends.py:39-57; deps.py:135-147`

### §5.5 目录 SPI · 1✅ · `↗ 内置目录见 §3.3`
- ✅ **目录提供者 SPI（set_catalog；默认 InMemoryCatalog）** — 可替换提供者；默认每实例缓存写穿 + 从 metadb 加载，RLock 线程安全。 `kernel/kernel/plugins/catalog.py:21-177`

### §5.6 能力与处理器 · 1✅ 1🟡
- ✅ **处理器库 + 提升到库** — promote() 从临时单元格注册代码处理器（版本自增）；沙箱编译；POST /processors/promote + GET /processors。 `kernel/kernel/plugins/processors.py:62-97`
- 🟡 **Capabilities（media/vector 列标注 + 查看器标签页）** — 检测可用 + Media 网格；缺口：add_capability 只声明 id+label（tag_columns 硬编码、不能加检测或标签页），vector 标签页已移除只剩内联 chip。 `↗ 前端标签页见 §1.2` `kernel/kernel/plugins/capabilities.py:35-51; capabilities.tsx:42-50`

### §5.7 流水线导入器 SPI · 1🟡
- 🟡 **/pipelines/import** — deps.importer 默认 NullImporter→501 未配置（诚实，此前误为 400），插件 set_importer 注册真导入器；按设计不内置（无通用流水线格式）。 `kernel/kernel/deps.py:98; routers/catalog.py:125`

---

## §6 平台 · 运维与部署

### §6.1 应用与服务 · 4✅
- ✅ **dataplay 一条命令启动器 / CLI** — --host/port/workspace/data-dir/no-open/no-seed；播种、配 workspace、uvicorn、开浏览器。 `kernel/kernel/cli.py:15-44`
- ✅ **FastAPI 应用工厂 + 路由拆分** — catalog/runs/workspace router；public_router 不门控，其余 Depends(current_user)。 `↗ 门控语义见 §4.3` `kernel/kernel/main.py:37-54`
- ✅ **SPA 服务（单进程，打包 + 开发回退）** — 存在则服务 kernel/_web（wheel）否则 web/dist；pyproject force-include ../web/dist。 `kernel/kernel/main.py:145-149`
- ✅ **CORS 限制为 localhost 来源** — allow_origin_regex 只 localhost/127.0.0.1（无通配符）；跨域部署需放宽。 `kernel/kernel/main.py:41-45`

### §6.2 设置 · 4✅
- ✅ **全局设置页面（agent / execution / destinations）** — 全屏带左侧导航；覆盖 agent model/key/baseURL、runner 选择、destinations + 对象存储凭证。 `web/src/panels/SettingsModal.tsx:23-154`
- ✅ **用户级设置** — scope='user' 接进 UI：Execution runner 是 per-user 偏好，pick_runner 用户优先再回退 global；主题走 localStorage（无闪烁）。 `↗ pick_runner 见 §2.3` `web/src/panels/SettingsModal.tsx; deps.py pick_runner(plan,uid)`
- ✅ **设置 API 密文脱敏** — GET 把 agentApiKey + objectStore keys 脱敏为哨兵，PUT 把哨兵当"未改动"，点点点不覆盖密文。 `kernel/kernel/routers/workspace.py:226-262`
- ✅ **用户管理（Members UI 创建用户）** — Settings→Members 列出 + 创建（姓名 + 可选初始密码，走 POST /users，刷新名单）；密码轮换自助，无 admin 重置端点（否则可劫持）。 `↗ 后端见 §4.3` `web/src/panels/SettingsModal.tsx(Members); api.createUser`

### §6.3 元数据与迁移 · 2✅
- ✅ **元数据 DB（SQLAlchemy；SQLite 开发 / Postgres 生产）** — 用户/画布/分享/运行记录+状态/版本/目录条目+边/设置；仅连接串可配（DP_DATABASE_URL）。 `kernel/kernel/metadb.py:39-154`
- ✅ **Alembic 迁移（含遗留 DB 收编）** — 7 revision 各带 upgrade+downgrade；init_db 给 Alembic 前的 DB 打 baseline 印记；Alembic 是 schema 唯一真相源。 `kernel/kernel/metadb.py:167-191; migrations/0001..0007`

### §6.4 设计系统 · 2✅ 1🟡
- ✅ **亮/暗主题切换（跟随系统，无闪烁）** — light/dark/system，localStorage，data-theme 首帧前应用；TopBar 切换。 `web/src/theme/mode.ts:9-43`
- ✅ **设计 token（shadcn HSL 变量 + TS 镜像）** — shadcn 原语 + token + 亮暗主题到位；组件读主题感知 token 层。 `web/src/index.css:11-107; theme/tokens.ts:8-99`
- 🟡 **shadcn/ui 迁移** — Radix+cva 原语已存在、新面板在用，但迁移未完；仍有内联样式/遗留变量组件。 `web/src/components/ui/*.tsx`

### §6.5 Agent（LLM）· 2✅ 1⬜ · `↗ dock UI 见 §1.6`
- ✅ **LLM agent（供应商无关、进程内、服务端持 key）** — Pydantic AI tool-use 循环（add/connect/set_config/preview）；模型经 DP_AGENT_MODEL/DB 设置；key 留 kernel；请求上限约束；新节点自动布局。 `kernel/kernel/agent.py:99-257`
- ✅ **关系感知工具（catalog+键 / join_hints / validate）** — 声明式选择交给 LLM：`list_catalog` 带主键候选+行数；`join_hints(a,b)` 给实测基数的候选键+已声明关系；`validate` 出类型错误+每个 join 的扇出/基数校验。LLM 翻译模糊意图，工具供事实+校验（非 rule-based 规划器）。 `kernel/kernel/agent.py list_catalog/join_hints/validate`
- ⬜ **Agent 离线关键词规划器兜底** — 有意为之、非缺陷：不做假装 LLM 的规则替身；无模型时 dock unavailable，其余照常。 `web/src/panels/AgentDock.tsx; README.md`

### §6.6 无状态 web / 扩展部署 · 4✅ 1⬜ · `↗ 执行器 §2.3 · 目录 §3.3 · 协作 §4.1`
- ✅ **共享运行状态** — RunState 表镜像每次转换；GET /run 与状态 WS 可从任意实例回答，重启存续。 `kernel/kernel/metadb.py:97-108,343-363`
- ✅ **共享目录写穿** — catalog_entries/edges 持久化数据集/输出/血缘；每实例缓存写穿 + 从 DB 加载。 `kernel/kernel/metadb.py:110-129,366-398`
- ✅ **离线 / 零配置运行** — 无需云账号；内置 SQLite；首次播种样本；引擎依赖全本地；Agent+Postgres+Lance 是可选 extra。 `README.md:8-9,26`
- ✅ **部署文档 + Docker** — Dockerfile 单镜像（冒烟测试）+ docker-compose（Postgres + 卷 + 认证/数据集 root，deploy.replicas + sticky routing）；TLS/反代留运维。 `Dockerfile; docker-compose.yml; README 'Run with Docker'`
- ⬜ **分布式执行** — 执行始终在接收实例；reconcile_orphaned_runs 假设单执行实例（多实例重启会取消别处运行）；需按运行的实例归属 + 心跳 + ExecutionBackend 插件。 `kernel/kernel/metadb.py:401-419; README.md:95-99`
