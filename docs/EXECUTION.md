# 执行与放置(Execution & Placement)设计 — 评审稿 v2

_状态:设计评审中(未实现)。v2 已折入一轮基于真实代码的对抗式评审(含实测证明的缺陷)。目标:把"假 kernel"变成真能按能力把每一步放到具体 worker(pod 或进程,取决于插件)上跑、默认自动计划、可手动覆盖/钉住、结果进共享存储从而跨运行复用、相邻同放置的步骤融合成一个操作(甚至一个 Ray job)的执行层 —— 同时不破坏"每一步都能看到真实数据"这个核心卖点。_

## 0. 现状(为什么说是"假 kernel")

- `Deps.pick_runner` 给**整个 run** 选**一个** `ExecutionBackend`(内置 `LocalRunner` 进程内 / `SubprocessRunner`)。这就是今天全部的"切换";没有 per-node 放置。
- `Placement = Literal["local","distributed"]` 是**run 级标量死枚举**,穿过 `RunEstimate/RunStatus/KernelInfo.mode` 但永远 `"local"`。
- `KernelInfo.warm = True` 硬编码;没有 worker/能力/绑定概念。(注:前端徽章其实看的是 `kernelUp` 连通性、不是 `warm`;真正的耦合是 `test_kernel_info` 对 `runners` 的精确断言 + 前端 `api.ts` 的 KernelInfo 类型 + SettingsModal 的 backend 下拉。)
- 节点无资源需求字段。
- 引擎把整张**关系**图融合成一个惰性 DuckDB 计划、在一个进程里跑;但 **transform 已经是物化边界**(full-run 时 spill 到 parquet 再重扫,engine.py:366),**section 每次 `run()` 也物化**(section.py:43)。所以"整图 = 一个 DuckDB op"不准确,准确说法是**"一个进程 / 一个 Ray job,内部在每个 transform/section 处做 parquet 交接"**。
- 已有可复用件:`section` 的 parquet 物化(交接的 sink 半边);`runner._plan_hash`(内容寻址雏形,但**有正确性缺陷,见 §3**);`destinations`/adapters 的对象存储读写;`catalog`/血缘;`run_states`(跨实例/重启状态)。

## 1. 目标模型:放置计划 + 融合单元 + 内容寻址共享存储

**一个放置计划器给每个"可放置节点"定一个 target(worker/backend);把相邻、同 target、可融合的节点并成一个"执行单元"(ExecUnit);在 target 变化处、sink、以及用户 checkpoint 处物化到共享存储;每个单元按内容寻址,已算过的直接复用。**

特例谱系:

- **整图同 target** → 所有节点一个单元 → 一个进程内计划 / **一个 Ray job**。← "下次跑全图融合成一个操作"。
- **一个独立 GPU transform + 其余 CPU** → CPU 关系核一个单元、GPU transform 一个单元,边界物化交接。← "captioning→A100,其余→CPU"。
- **今天** → 一个默认本地池、所有节点同 target → 就是现在的行为(退化特例)。

## 2. 核心抽象(SPI)

### 2.1 `ResourceSpec` — 需求与能力同形
`{ cpu?, mem?, gpu?, gpu_type?, labels? }`。worker 广告 **capacity**,step 声明 **requires**,匹配 = `capacity ⊇ requires`。

### 2.2 `Worker`(pod 或进程 — 后端决定)
`WorkerInfo = { id, backend, capacity: ResourceSpec, state: idle|busy|down, labels }`。core 不关心 pod 还是进程,只看 capacity。

### 2.3 `ExecutionBackend`(**在 Protocol 之外**扩展 — 关键)
现有 `@runtime_checkable` Protocol(`can_run/estimate/run/status/cancel`)**保持不变** —— 否则契约测试(`test_execution_backend_plugin_contract`、`test_spi_contracts_are_the_real_ones`)和旧插件全挂。新能力**用可选方法**(planner 用 `getattr` 探测,老后端没有就当"整图一个单元"的退化后端跑):
```
# 可选(非 Protocol 成员):
def workers() -> list[WorkerInfo]                 # 真 KernelInfo 的来源;集群后端要带 TTL 缓存
def place(requires, reserve=True) -> str|None      # 能力匹配 + 预留/租约(避免并发双占)
def run_unit(unit, inputs: list[ResultRef], outputs: list[ResultRef],
             worker: str) -> UnitHandle             # 执行一个融合单元;返回可持久化的句柄
def cancel_unit(handle: UnitHandle) -> None
```
`run_unit` 由**后端决定怎么执行**:本地池 = 子进程跑该单元(读 input refs、写 output refs);Ray 后端 = 一个 Ray job;k8s = 匹配的 pod。单元之间**从不点对点传数据**,一律经共享存储的 ref。**`UnitHandle`(后端名 + 外部 job/pod id)必须可持久化**,cancel 才能跨实例定位(见 §8)。

### 2.4 `RunController`(新增 — 逻辑 run 的拥有者)
评审指出:`run_index` 是 `run_id→单后端`,而一个逻辑 run 现在跨多单元/多后端。引入一个 controller:
- 持有 `run_id → [(unit_id, backend, worker, UnitHandle, status)]`,**持久化到 DB**(见 §8),不只内存,故任意 stateless 实例都能查/取消。
- 按单元 DAG 拓扑序派发 → 各 target 后端的 `run_unit`;**聚合**各单元状态成一个 run 级 `RunStatus`(规则:任一 unit failed→failed;全 done→done;有 running→running),每次转移经现有 `on_status`→`save_run_state` 写 DB。
- `routers/runs.py` 入口从 `pick_runner(...).run(...)` 改为 `controller.run(plan)`;`status/cancel/ws/_status_or_lost` 只跟 controller 打交道。
- `estimate()` 保留:planner 对每个单元调其 target 后端的 estimate,汇总成 run 级 `RunEstimate`,并**cache-aware**(命中 store 的单元记为已算、从 `needs_confirm` 扣除,修掉"全缓存命中还弹确认框"的假 gate)。

## 3. 内容寻址的共享结果存储(ResultStore)—— 重定义(评审的最大正确性问题)

⚠️ 直接复用 `runner._plan_hash` 会把**进程内的临时缓存**升级成**持久、共享、跨实例的错误数据**。实测:改一个 section 里 filter 的谓词 `x>0`→`x>999`,hash 不变(`664253bf7b741cdc`)。必须先加固 key。

**注册**:按 adapters 模式做成 list + `resolve_store(ref.store)`(ResultRef 已按 store 寻址);实现 `LocalDirStore`/`S3Store`。`put()` 由 **worker 在 `run_unit` 内部**调用,故 ResultStore 必须能在 worker 侧**从配置(uri + 凭证)重建**(参照 `subrun.py` 重建 Deps;Ray/k8s 不能直接序列化内存对象)。

**key 的精确定义**(每个单元):
- (a) 单元内**每个节点**的 type + config —— 对 section,**递归包含所有 `parent_id` 后代节点**的完整 config(含 transform 代码);
- (b) **代码版本指纹**:内联 transform 代码;library processor / 插件 lowering / adapter 的**代码/版本指纹**(不只是 config 里的 processor id);
- (c) **对象存储源用内容指纹**(etag / HEAD 的 size+mtime / manifest hash),**不是** `hash(uri)`(现在 s3 覆盖写同 uri → 指纹不变 → 永久返回旧结果;`mem://` 更糟,恒为 `"mem"`);
- (d) 上游输入按 **(上游单元, 输出 port)** 的 ref key。
- **`requires` 不进 key**:它是"在哪跑"、不是"内容"。同样的确定性算子从 GPU 挪到 CPU 数据不变,却因 requires 变了被迫重算 → 违背共享存储的初衷。

**绕过缓存**:`append` 不是幂等的(每次要加一个分片)→ 显式**非缓存**;非确定性单元(GPU/随机)标 **`cacheable=false`** —— 注意它会**污染所有下游 key**(下游拿不到稳定的上游 ref),这是预期语义,要说清。

**存在性检查**:`has(key)` 必须查**真实 store**(本地 AND 对象存储)。现有 skip 用 `os.path.exists`(runner.py:218),对 s3/gs **永远 False** → 天真复用在最常见的部署场景(README "Scaling out")静默失效。

**保留/GC**:持久共享 store 需要引用计数 / TTL / 保留策略(进程内 `_MAX_RUNS` 驱逐无等价物)—— 否则要么无限增长,要么驱逐掉别人还在复用的 ref。

## 4. 放置计划器(Planner)

输入:编译图 + 每节点 requires + 可用 workers + 手动覆盖。

1. **定 target**:手动钉住 > `place(requires)` 匹配 worker > 默认池(CPU)。
2. **section 是放置不透明的原子单元**:planner **不下钻** `parent_id` 子节点;一个 section 整体在一个 worker 上跑;子节点的 requires **上卷**为 section 的 requires(对子节点取 max)。**修 `compile_plan(target=None)`**:它现在把 section 子节点当自由节点拓扑排序 → 孤儿 filter 报 NotPreviewable(实测 steps=[src,sec,clean] 会崩);planner 必须把 `parent_id` 节点排除出顶层放置/融合/hash。
3. **融合**:相邻、同 target 的关系节点并成一个单元(与今天相同);同 worker 的连续 transform 并入;后端若声明"整单元一个 Ray job"则整块下发。诚实说法:一个单元 = 一个进程/一个 Job,**内部**仍在 transform 处 parquet 交接。
4. **物化点(精确规则)**:一个节点是物化点 ⟺ 它是 sink(write) **或** 任一出边**跨 target 边界** **或** 它 feed **>1** 个否则会重算它的单元 **或** 用户打了 checkpoint。所有消费者(本地/远端)都读它的 ref。(这消解了"融进 D1 → D2 没 ref 可读"的 fan-out 歧义。)
5. **port 感知**:节点可多输出 `{port→Relation}`(engine.py:73),section 多命名端口,write 按 source_handle 路由。**ResultRef 按 (单元, 输出 port)**;多输出单元写 N 个 ref;下游按 source_handle 解析。
6. **保住快路径**:
   - **branch**:谓词在**消费者**侧应用(engine.py:112),branch 本身是直通。若在 branch 处物化,会物化**未过滤**的关系;要么在 branch 处物化**两个已过滤的 ref**,要么把 branch 谓词 + handle 带进下游单元让它读 ref 后再过滤。**二选一(待定)**。
   - **Lance 原生 ANN**:vector-search 只在输入是**裸 .lance 源**(中间无算子)时走原生 nearest();一旦在 Lance 源与 vector-search 间插入 parquet 边界 → 退化为 O(N) 暴力扫描,且 parquet ref 带不了 Lance 索引。→ 让 vector-search 与其 Lance 源**留在同一单元**(不插边界),或让 ResultRef 能引用 **Lance URI**(不只 parquet)。
7. **执行**:单元 DAG 拓扑序(无依赖可并行),各单元交 target 后端 `run_unit`;controller 聚合状态。MVP 调度:首个满足的空闲 worker(带 §2.3 的 reserve/lease,避免并发双占)。

## 5. 物化与可观测 —— "还能不能看中间节点的数据?"(评审 + 用户重点)

融合会让中间节点不落地。**看一个节点的数据有两条路,融合只影响第二条:**

1. **预览(采样)** —— 任何**可预览**节点永远可看。它在本地引擎里把"到该节点为止的子图"在有限样本上重下降一遍,与放置/融合无关 → 头号卖点"每一步看到真实行"始终成立。
   - 注意成本不是恒定小:join/sort/vector-search/metric 的忠实预览会对**未采样**源起第二个 full 引擎(engine.py:130);且 transform 上游时预览会 NotPreviewable。
   - **硬 requires(GPU 等)的节点默认非可预览**(否则会在 web 进程上跑 GPU 活)。
2. **运行产出(完整结果)** —— 只有**被物化**的节点有(§4.4 的物化点)。融合掉的关系中间节点这次运行没有落地的完整输出。注:**transform 输出本来就物化**(spill),所以它天然可看;真正 transient 的是**融合的关系链内部**。

**图上表示 + 运行时选择**(用户要点):
- 节点新增**物化状态**(与 draft/latest/stale 并列):● 已物化(有持久 ResultRef,可点开看完整结果 + 可跨运行复用)/ ○ 融合掉(只可预览采样)。
- **运行前**按 planner 结果预示"这几个会物化、其余融合";**运行后**转为实际状态。
- **手动**:节点上一个 **checkpoint(在此物化)**开关(强制物化=可看+可复用,代价是在此断开融合);运行选项 **"materialize all"**(调试:全物化=不融合,像到处 `.materialize()`,慢但全可看)。
- **统一**:物化 = 可看 + 可复用。给昂贵中间节点打 checkpoint,既能看它、下次改下游也能直接复用它。

## 6. 需求声明(节点侧)
- `NodeSpec.requires`(插件默认,如 captioning 节点声明 `gpu>=8, gpu_type="a100"`);`config.requires`(UI"Resources"区实例覆盖);`config.placement`(auto | 钉住 worker id);`config.checkpoint`(强制物化);`config.cacheable`(默认 true;非确定性置 false)。
- 主要用于 transform/section/插件;关系节点默认空 → 默认 CPU 池。硬 requires ⇒ 非可预览。

## 7. 真 `KernelInfo` + UI(**加字段,不改形状**)
- 保留 `runners`/`warm`/`mode` 不动(`test_kernel_info` 精确断言 + 前端 `api.ts` 类型 + SettingsModal 下拉都依赖),**新增** `backends: [{name, workers:[{id,capacity,state}]}]` 字段。徽章/下拉迁移到新字段是**单独的前端步骤**。
- 集群后端的 `workers()` 是有延迟/会失败的集群查询 → `/api/kernel` 要带 **TTL 缓存**,`state` 更新方式要定。
- `RunStatus.per_node` 增 `worker/backend` 字段(承载"哪个 pod 在跑",§5/UI 需要);run 级 `placement` 标量重命名或降级为摘要,per-node 放置用**新类型**(别复用 `Placement` 这个名)。

## 8. 控制面(分布式的状态/取消/一致性)
- **run_states 按单元拆**:父 run 行 + 每单元行(或父 doc 内嵌单元状态);controller 是聚合写入者。
- **cancel 走持久句柄**:派发时把每单元 `(backend, UnitHandle)` 落 DB;`cancel(run_id)` 由 controller 读 DB → 对每个未完成单元调 `backend.cancel_unit(handle)`。任意实例可取消,不依赖内存 `run_index`。
- **reconcile 加 ownership**:`run_states` 增 `owner_instance_id` + `heartbeat_at`;启动时只把"本实例拥有且已死"或"心跳超时"的标 interrupted,不碰别的活实例的 run(正好落实 metadb.py:410 的 TODO 与"一个实例启动取消另一个实例活 run"的隐患)。

## 9. 增量路径(**已按评审重排** —— 每步独立可发布、可测)

原"P0 = 纯元数据"零执行价值却仍扰动 runners 契约与 KernelInfo 形状,不划算。重排为:

- **Phase A — 持久化本地 ResultStore(真价值、单进程、零分布式)**:给现有 `LocalRunner` 加一个本地(+ 可选 s3)持久 ResultStore,做**跨运行复用 + resume**。前置必做:§3 的 key 加固(section 后代 + transform 代码 + 对象存储内容指纹)、`append`/`cacheable=false` 绕过、**store-aware `has()`**(不是 `os.path.exists`)、cache-aware `estimate()`(修假 confirm)。**再加 §5 的物化状态**(节点 ●/○ + checkpoint)——此时"物化"就是"写进本地 store"。这是最小的、能独立交付价值的一步。
- **Phase B — 控制面重构(无行为变化)**:引入 `RunController`(即使只有一个本地后端);KernelInfo **加** `backends[]` 字段;节点 `requires`(纯元数据 + UI)。为 `ExecutionBackend` 的可选方法定契约、bump `CORE_API_VERSION`;旧后端走退化路径。
- **Phase C — 本地多进程 pool 参考后端 + 真放置**:pod=子进程、能力可配;`workers()/place()/run_unit`;planner 的 per-node 放置 + 边界物化 + fan-out/port/branch/Lance 处理。**无需 k8s 即可演示/测试**"GPU 之外全真"。前置:图切分 + 把被切的边重写成"ref-source"节点(让子图从物化 parquet 起,而不是原始源)+ 新的 child 入口模式(现有 `subrun.py` 跑整图,不够)。注意**worker 复用跨租户会重新引入进程内状态泄漏**(削弱刚加的 subprocess 隔离)—— pool 的租户策略要定。
- **Phase D — 插件后端**:Ray(`run_unit`=一个 Ray job)、k8s-pod;跨实例 ownership+heartbeat(§8)。

## 10. 开源边界
- **OSS 内**:全部 SPI + 模型 + planner + 本地多进程 pool 参考后端 + local/s3 ResultStore + 物化状态 UI + Compute UI。→ 完全可用、可演示、可测。
- **插件(可开源可专有)**:Ray backend、k8s-pod backend、专有调度器/排队。

## 11. 待你拍板的关键决定
1. **`requires` 不进内容 key**(内容 = 输入+config+代码,不含"在哪跑")—— 认可?
2. **section 是放置不透明的原子单元**(不下钻;captioning 要么独立 transform 节点、要么整个 section 放 GPU worker)—— 认可?
3. **branch 跨边界**:在 branch 处物化两个已过滤 ref,还是把谓词带进下游单元?
4. **Lance 源 + vector-search 不被边界拆开**(留同一单元 / 或 ResultRef 支持 Lance URI)—— 倾向?
5. **路线从 Phase A(持久本地 ResultStore + 物化可观测)起步**,而不是原来的纯元数据 P0 —— 认可?
6. **pool worker 是否跨 run/租户复用进程**(复用=快但削弱隔离;每 run 新进程=隔离但慢)—— 倾向?
7. 缓存/store 的**保留/GC 策略**(引用计数 / TTL / 手动)—— 倾向?
