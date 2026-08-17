"""
全局配置。

配置项集中在这一个文件里，不拆散 —— 找一个配置项应该只有一个地方可看。
每个非显然的取值都带注释说明依据（.env.example 里有更详细的版本）。
"""

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录。从 backend/app/core/config.py 上溯 3 层。
# 改动目录层级时必须同步改这个数字 —— 所以启动日志里会打出实际路径，一眼能看出对不对。
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class AppConfig(BaseModel):
    host: str = "127.0.0.1"
    # 9000 而非 8000：Windows 的 Hyper-V/WSL 预留了 7905-8928 整段，
    # 8000 在里面。绑定失败的报错出现在 "Application startup complete" 之后,
    # 很容易误判成应用自身的问题。
    # 查看本机保留段：netsh interface ipv4 show excludedportrange protocol=tcp
    port: int = 9000
    log_level: str = "INFO"
    env: str = "dev"

    @property
    def is_dev(self) -> bool:
        return self.env == "dev"


class SecurityConfig(BaseModel):
    # 缺失时拒绝启动。见 main.py 的启动期校验。
    encryption_key: str = ""


class LLMConfig(BaseModel):
    # 300 秒。60s 会让代码生成必然失败：实测一次演示请求耗时 220s，
    # 22921 个 completion token 里 20994 是推理 token（91%），推理阶段流里没有任何数据。
    request_timeout: int = 300
    # 探测是交互式操作，用户在等着看结果，不能给 300 秒。
    probe_timeout: int = 15
    max_retries: int = 3
    # httpx 默认读环境变量与 Windows 注册表里的系统代理。
    # 实测：开着 Clash 时同一个长生成请求走代理 32s 被掐断，绕过代理 252s 正常完成。
    # 代理的空闲连接超时远短于一次长推理，而推理阶段不吐字节，连接看起来就是空闲的。
    trust_env: bool = False
    # 思维链是否随【带 tool_calls 的 assistant 消息】回传给上游。
    #
    # DeepSeek 文档要求带 tools 时必须回传（说不传会 400），收益是让模型
    # 延续上一步的推理 —— 对多步工具链的 agent 是实质收益，因为 tool_call
    # 轮次的 content 常常是空的，思维链就是它全部的思考。
    #
    # 关掉的场景：某些端点对未知字段严格，会因为 reasoning_content 报 400。
    # 那时把这个设成 false，其它一切照常工作。
    send_reasoning_back: bool = True


class AgentConfig(BaseModel):
    # 40。一个真实的代码修改任务轻易用掉 15~25 轮，设小了会在任务快完成时被截断。
    max_turns: int = 40
    # 达到 80% 时注入一次催促（不硬停）。模型收到催促通常会收敛给结论，
    # 而硬停会留下一个半成品。用 == 判定所以只注入一次，不会反复刷。
    warn_turn_ratio: float = 0.8
    # 用比例而非固定 token 数：模型窗口 8K~200K 不等，
    # 固定值在小窗口上永不触发（直接 400），在大窗口上过早触发（白丢信息）。
    compact_trigger_ratio: float = 0.75
    keep_tail_turns: int = 4
    # 候选集少于这个数就不压。压缩本身要花一次 LLM 调用，
    # 把两条消息换成一条摘要毫无意义，还会让"压缩"本身变成上下文噪音。
    compact_min_victims: int = 4
    # 摘要的目标长度：窗口的百分之多少。
    #
    # 【只算对话部分】—— 系统提示词和工具定义不占这个额度。那两项是固定
    # 开销（本项目 18 个工具就 4300 token），把它们算进来的话摘要额度会
    # 被挤到几乎没有。
    #
    # 20% 的取法：压完之后对话占 20%、固定开销占 5% 左右，还剩 75% 给
    # 后续对话。留得更少（比如 10%）会让摘要丢太多信息；留得更多
    # （比如 40%）则压完没多久又要再压一次，而每次压缩都是一次有损转写。
    compact_target_ratio: float = 0.20
    # 结构类事件入队的最长等待。
    #
    # 【不能无限等】—— 那会让整个 run 永久死锁、会话被锁死、DB 连接泄漏，
    # 只有重启进程能恢复。而超时丢一个事件的代价只是前端某个卡片
    # 停在"执行中"，刷新即好。
    #
    # 30 秒：正常消费端的 get() 是毫秒级；卡这么久说明客户端已经
    # 实质失联，再等下去没有意义。
    event_put_timeout: float = 30.0
    # tail 最多占窗口的多少。keep_tail_turns 按轮次数，但每轮大小差异巨大 ——
    # 实测 keep=4 遇到每轮 3000 token 的长文时 tail 自己就 12000 token，
    # 而窗口只有 4200，于是压缩每轮触发每轮放弃，上下文涨到窗口的 221%。
    # 取 0.4：剩下的留给 system、工具定义（实测 1400+）、摘要和本轮生成。
    tail_budget_ratio: float = 0.4
    # artifact 常驻上下文（排除在压缩之外），所以要限大小。
    # 一个几万行的文件会把窗口占满，反而挤掉真正需要的历史 ——
    # 那就本末倒置了。超过这个大小的产出不当 artifact，
    # 模型需要时用 read_file 重新读。
    artifact_max_chars: int = 20_000
    # 渲染给摘要模型时，单个工具结果最多取多少字符。
    # 工具输出可能几万字符（比如 grep 一个大仓库），全塞进去会让
    # 摘要请求本身就超限 —— 压缩动作自己撞墙，这是最尴尬的失败方式。
    compact_tool_excerpt: int = 1_500
    # 首轮没有真实 usage 时用估算，阈值打八折做保守判断。
    # 估算偏差在有工具定义和长 system 时可达 20%，不打折会估低后直接 400。
    estimate_safety_ratio: float = 0.8
    max_subagent_depth: int = 3
    # 超时视为拒绝而非允许 —— 用户离开电脑时不应该有命令自己执行。
    approval_timeout: int = 300
    # 单个工具的执行上限。没有它，一个忘了设 timeout 的工具能把整个 run 挂死,
    # 而 SSE 有心跳所以前端不会断，用户就一直等。
    # 很多实现没有这层统一超时（timeout 分散在各个工具内部），
    # 但我们有 registry.execute 这个唯一入口，加在这里能全局兜住。
    tool_timeout: int = 300
    # LLM 调用失败的重试次数。8 次是经验值：够扛过限流窗口，又不会把一次失败拖成几分钟。
    max_llm_retries: int = 8
    # 连续多少轮以完全相同的 (工具名, 参数) 调用后判定为"打转"。
    # 到达时注入一次提示让模型换路，不硬停。
    max_repeat_calls: int = 3
    # 事件队列上限。满时丢增量类事件（thinking/message），结构类阻塞等待。
    event_queue_size: int = 512
    # SSE 心跳间隔。推理阶段可能 200s+ 不吐字节，没有心跳会被中间层判为超时。
    heartbeat_interval: int = 15


class SandboxConfig(BaseModel):
    backend: str = "local"
    timeout_default: int = 120
    timeout_max: int = 600
    # 输出上限用【行数 + 字节数】双限制，先到先算。
    #
    # 只用单一字符数阈值（32000 字符），在多字节内容下与真实 token
    # 消耗脱节 —— 32000 个中文字符是 32000 个 token 上下，而同样字符数的
    # 英文只有 8000。完全不截断，长输出直接冲爆上下文。
    # 这里用行数和字节数双限。
    max_output_lines: int = 2_000
    max_output_bytes: int = 50 * 1024
    # 进程退出后等管道空闲的宽限期（秒）。
    #
    # 一个已知问题的修复思路：短命的主进程可能已 exit，但它 fork 出的
    # 后台进程还持着 stdout。立即关流会静默丢掉还在写的输出 ——
    # 丢的通常正是错误信息。
    drain_grace: float = 0.5
    # 空表示自动探测（Windows 优先 PowerShell，POSIX 优先 bash）
    shell_path: str = ""

    # ── Docker 后端（backend=docker 时生效）──
    #
    # 【字段名必须带 docker_ 前缀】—— .env.example 里文档的就是
    # JEEVES_SANDBOX__DOCKER_IMAGE 这套名字。
    #
    # 我最初新增了一套不带前缀的同义字段（image / network / memory_limit），
    # 于是配置里同时存在两套：代码读 settings.sandbox.image，而用户按
    # .env.example 配的是 DOCKER_IMAGE —— 完全不生效，
    # 且 Settings 的 extra="ignore" 让它静默失败，没有任何报错。
    #
    # 用 python:3.12-slim 而不是自建镜像：官方镜像 docker pull 就有，
    # 不需要构建步骤（构建要几分钟且需要网络）。约 130MB，
    # 而自建 ubuntu + node + build-essential 的镜像要 800MB+。
    docker_image: str = "python:3.12-slim"
    # none | bridge
    #
    # 【默认 none】。用的是 --network host
    # —— 那意味着容器直接用宿主的网络
    # 命名空间：能 curl 宿主的 localhost 服务（含 agent 自己的 API）、
    # 能访问内网、能打 169.254.169.254 拿云凭证。
    #
    # 文件系统隔离做了但网络没做，等于沙箱只有一半。
    # 需要装包时用户临时改成 bridge。
    docker_network: str = "none"
    docker_memory: str = "2g"
    docker_cpus: str = "2"

    # 环境变量名里含这些片段的一律不传给子进程。
    #
    # 防的场景很具体：用户为了方便在系统里设了 OPENAI_API_KEY，
    # 然后模型执行 `env` 把它打印出来 —— 那就直接进了上下文、日志、摘要。
    # 命中即删，宁可多删无关变量也不漏一个 Key。
    env_deny_markers: tuple[str, ...] = (
        "KEY",
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "PASSWD",
        "CREDENTIAL",
        "PRIVATE",
    )
    # 防 fork 炸弹。
    #
    # docker_memory 挡不住它 —— fork 炸弹的每个进程都很小，
    # 靠数量而不是内存把系统压垮。
    docker_pids_limit: int = 512

    @property
    def default_timeout(self) -> int:
        return self.timeout_default


class MemoryConfig(BaseModel):
    # 总开关。关掉时不提取、不召回，但已有的记忆文件保留 ——
    # 删记忆要显式操作，不能因为关个开关就丢数据。
    enabled: bool = True

    # 提取时保留最近几轮不归档。
    #
    # 正在进行的对话还没结束，不该被总结。按 user 消息分轮
    # （一轮 = 一个 user query + 后续 assistant/tool）。
    #
    # 3 而非 OpenViking 的 10：它按【条】数，我们按【轮】数。
    # 一轮在本项目里可能有十几条消息（一次代码修改任务的工具调用很多），
    # 3 轮已经比它的 10 条多。
    keep_recent_turns: int = 3

    # 单条记忆的正文上限（字符）。
    #
    # 超过就截断。理由是记忆会被整段注入提示词，一条失控的记忆
    # （模型偶尔会把整个文件内容写进 content）能把窗口占满。
    max_item_chars: int = 8_000

    # 一次提取最多写多少条记忆。
    #
    # 防的是模型把一段对话拆成 50 个"事件"。真实的一轮对话产出
    # 3~8 条记忆是正常的，超过 20 条说明它在拆碎片。
    max_items_per_extraction: int = 20

    # 提取的 ReAct 循环上限。与 OpenViking 的 max_iterations=3 一致。
    #
    # 这是【基础预算】。工具调用、格式重试、patch 修复、refetch
    # 每次各 +1，所以实际轮数可能更多 —— 那三种情况不是"模型不听话"，
    # 是信息不足，不该占用正常预算。
    extract_max_iterations: int = 3

    # 预取策略。对齐 OpenViking 的 eager_prefetch（memory_config.py:58）。
    #
    # true  = 一次性预取全部可改的记忆，不给模型工具
    # false = 只预取轻量索引，给 list/read/search 让模型按需拉取
    #
    # 默认 true：记忆少时全预取省一轮 LLM 调用，且不会出现
    # "模型没搜到于是新建重复记忆"。记忆多到装不下窗口时应该改成 false ——
    # 那时全预取会挤掉对话内容，而对话才是提取的原料。
    eager_prefetch: bool = True

    # eager_prefetch=false 时，预取阶段仍然读取的记忆条数上限。
    # 超出的部分靠模型自己 search/read。
    #
    # 5 与 OpenViking 的 prefetch_search_topn 一致（memory_config.py:64）。
    prefetch_topn: int = 5

    # 向量搜索查询文本的截断上限（字符）。
    #
    # 用于构建搜索查询的摘要：从对话消息中提取关键内容，截断到该长度。
    # 查询太长会影响搜索性能，且嵌入模型通常有输入长度限制。
    #
    # 5000 与 OpenViking 的 _PREFETCH_SEARCH_QUERY_MAX_CHARS 一致。
    prefetch_search_query_max_chars: int = 5_000

    # 向量搜索返回的候选记忆数量上限。
    #
    # 搜索会返回按相关性排序的 URI 列表，取前 N 个。
    # 实际加载多少由预算决定（prefetch_max_chars），但搜索结果数量
    # 不应该太大，避免后续处理开销。
    #
    # 20 是合理值：相关性排第 20 之后的记忆基本不会被用到。
    prefetch_search_limit: int = 20

    # 构建搜索查询时，单条用户消息的截断上限（字符）。
    # 1000 与 OpenViking 的 _PREFETCH_SEARCH_TEXT_PART_MAX_CHARS 一致。
    prefetch_search_user_msg_max_chars: int = 1_000

    # 构建搜索查询时，单条助手消息的截断上限（字符）。
    # 500 与 OpenViking 的 _PREFETCH_SEARCH_ASSISTANT_TEXT_PART_MAX_CHARS 一致。
    # 助手回复通常比用户输入长，但对召回的信号价值较低，截更短。
    prefetch_search_assistant_msg_max_chars: int = 500

    # ── 截断相关（都可以在前端调） ──
    #
    # 这几个原来硬编码在代码里。抽出来的理由：不同模型的窗口差异很大
    # （8K 到 200K），而窗口小的模型需要更狠的截断。写死值意味着
    # 换个小窗口模型就只能改代码。

    # 提取时单条消息的正文上限（字符）。超过则截头尾，保留结论。
    max_msg_chars: int = 1_200

    # 一次提取的对话总量上限（字符）。超了从最早的轮次开始丢。
    max_conversation_chars: int = 60_000

    # 预取时单条记忆正文的展示上限（字符）。
    #
    # 比 max_msg_chars 宽松：预取内容是 patch 的 SEARCH 依据，
    # 截太狠会让模型拿不到可匹配的原文，只能新建。
    prefetch_preview_chars: int = 1_500

    # 向量化时 L2 详细层级的截断上限（字符）。
    #
    # L2 记忆可能包含完整的对话记录、代码片段等长文本，
    # 直接向量化会导致：
    # 1. 成本高 - 嵌入 API 按 token 计费
    # 2. 效果差 - 过长的文本会稀释关键信息
    # 3. 超限风险 - 可能超过嵌入模型的最大 token 限制
    #
    # 截断策略：取开头 + 结尾，保留关键信息和结论。
    # 2000 字符约 500-600 tokens，适合大多数嵌入模型。
    embedding_l2_max_chars: int = 2_000

    # 预取的【总字符预算】。超了从尾部类型开始丢条目。
    #
    # 必须有这个上限：原来 eager 模式不限量，实测一个有 120 条偏好的
    # 智能体（用半年就会有）让预取吃掉 13572 token。而记忆预取和对话内容
    # 抢同一个窗口 —— 预取吃太多就没地方放对话，而对话才是提取的原料。
    #
    # 24000 字符 ≈ 8000 token，给 128K 窗口的模型留足余量。
    # 窗口小的模型要调低。
    prefetch_max_chars: int = 24_000

    # read_memory 工具单次返回的正文上限（字符）。
    #
    # 比预取更宽松：模型主动 read 说明它确实要看全文来构造 SEARCH。
    tool_read_max_chars: int = 4_000

    # search_memories 返回的条数上限。
    # 与 OpenViking 的 search_files(limit=5) 一致。
    tool_search_limit: int = 5

    # ── 向量化 ──

    # 单条文本的字节上限。按字节而非字符 —— 中文一个字符 3 字节。
    embedding_max_bytes: int = 24_000

    # 一次嵌入请求塞多少条文本。
    #
    # 32 是保守值：供应商限制差异很大（OpenAI 2048、部分自建 16），
    # 超限的表现是 400 而不是自动截断。
    embedding_batch_size: int = 32

    # 语义搜索的最低相似度。低于它的结果不返回。
    #
    # 0 表示不过滤 —— 默认不过滤是因为合适的阈值【强依赖嵌入模型】：
    # bge-m3 的 0.4 和另一个模型的 0.4 不是一回事。让用户按自己的
    # 模型调，比我猜一个值更可靠。
    search_min_score: float = 0.0

    # 召回时注入提示词的记忆总字符上限。
    #
    # OpenViking 用 6500。本项目的窗口预算已经被系统提示词 + 工具定义
    # 占了不少（18 个工具就 4300 token），所以取更保守的值。
    recall_max_chars: int = 5_000

    # 分类型召回配额。混在一起搜会让某一类占满名额 ——
    # 三种类型语义完全不同，事件的相似度普遍比偏好高，
    # 不分桶时偏好永远排不进前 10。
    recall_limit_events: int = 8
    recall_limit_entities: int = 8
    recall_limit_preferences: int = 3
    recall_limit_experiences: int = 5

    # ── 递归搜索配置（OpenViking hierarchical_retriever）──

    # 是否启用递归搜索。
    #
    # true  = 从向量搜索结果出发，递归查找相关记忆（标签、实体、时间）
    # false = 只用向量搜索结果，不递归扩展
    #
    # 递归搜索是 OpenViking 的核心优势，能显著提升召回质量。
    # 默认启用，但会增加一些延迟（通常 <500ms）。
    recall_enable_recursive_search: bool = True

    # 递归搜索最大深度。
    #
    # depth=0: 只看初始向量搜索结果
    # depth=1: 从初始结果出发，查找一层相关记忆
    # depth=2: 从一层结果继续查找二层相关记忆
    # depth=3: 三层递归
    #
    # OpenViking 默认 3，我们沿用。深度越大召回越全面，但也越慢。
    recall_recursive_max_depth: int = 3

    # 分数传播系数（alpha）。
    #
    # 子节点最终分数 = alpha * 子节点原始分数 + (1 - alpha) * 父节点分数
    #
    # - 1.0: 完全不传播，子节点分数独立
    # - 0.7: 子节点主要靠自己分数，父节点贡献 30%（OpenViking 默认）
    # - 0.5: 父子平均
    # - 0.0: 完全继承父节点分数
    #
    # 0.7 是经验最佳值：既保留了子节点的语义相关性，又受益于父节点的高分。
    recall_recursive_alpha: float = 0.7

    # 每个节点扩展多少个相关记忆。
    #
    # 5 表示从每个高分记忆出发，最多找 5 个相关记忆（标签、实体、时间）。
    # 太大会导致搜索空间爆炸，太小会漏掉相关内容。
    recall_recursive_expansion: int = 5

    # 递归搜索的最低分数阈值。
    #
    # 传播后的分数低于此值，不再继续递归。
    # 0.3 表示只保留中等以上相关的记忆，过滤噪音。
    recall_recursive_min_score: float = 0.3

    # ── Rerank 重排序配置（OpenViking RerankClient）──

    # 是否启用 rerank 重排序。
    #
    # true  = 向量搜索后，用专门的 rerank 模型重新打分
    # false = 只用向量搜索分数
    #
    # Rerank 是精筛：能捕捉更细粒度的相关性（词序、逻辑关系）。
    # OpenViking 默认启用，但需要额外的 API 调用和成本。
    #
    # 注意：rerank 模型需要在设置页配置（purpose="memory_rerank"），
    # 不再通过环境变量配置。
    recall_enable_rerank: bool = False

    # 向量分数和 rerank 分数的混合权重。
    #
    # 最终分数 = vector_weight * vector_score + rerank_weight * rerank_score
    #
    # - (0.5, 0.5): 平均权重
    # - (0.3, 0.7): 更信任 rerank（推荐）
    # - (0.2, 0.8): 几乎完全依赖 rerank
    #
    # OpenViking 倾向于信任 rerank，因为它是精筛。
    rerank_vector_weight: float = 0.3
    rerank_rerank_weight: float = 0.7

    # ── 混合搜索配置（密集向量 + BM25 稀疏向量）──

    # 是否启用混合搜索。
    #
    # true  = 向量搜索 + BM25 关键词搜索混合
    # false = 只用向量搜索
    #
    # 混合搜索结合语义理解和关键词匹配，对精确查询（版本号、代码片段）效果更好。
    # OpenViking 通过 sparse_query_vector 实现，我们用 BM25 自己实现。
    recall_enable_hybrid_search: bool = False

    # BM25 参数 k1。
    #
    # 控制词频饱和度：
    # - k1 = 0: 忽略词频（只看是否出现）
    # - k1 = 1.5: 平衡词频影响（推荐，OpenSearch 默认）
    # - k1 = ∞: 词频线性增长
    #
    # 1.5 是经验最佳值。
    bm25_k1: float = 1.5

    # BM25 参数 b。
    #
    # 文档长度归一化：
    # - b = 0: 完全忽略长度
    # - b = 0.75: 平衡归一化（标准值）
    # - b = 1: 完全按长度归一化
    #
    # 防止长文档仅因包含更多词就得高分。
    bm25_b: float = 0.75

    # 混合搜索权重策略。
    #
    # 支持的策略：
    # - "adaptive": 自适应权重（根据结果重叠率调整）
    # - "query_based": 基于查询类型（版本号/代码 → 关键词优先）
    # - "balanced": 固定平衡权重（0.7 密集 / 0.3 稀疏）
    #
    # 推荐 "query_based"，对齐 OpenViking 的智能查询理解。
    hybrid_search_strategy: str = "query_based"

    # 默认密集向量权重（平衡查询）。
    #
    # 混合分数 = dense_weight * dense_score + sparse_weight * sparse_score
    #
    # 0.7 表示更信任语义搜索（密集向量），但保留关键词搜索的贡献。
    hybrid_default_dense_weight: float = 0.7
    hybrid_default_sparse_weight: float = 0.3

    # 关键词查询的权重（检测到版本号/代码片段时）。
    #
    # 平衡权重，因为关键词匹配很重要。
    hybrid_keyword_dense_weight: float = 0.5
    hybrid_keyword_sparse_weight: float = 0.5

    # 语义查询的权重（检测到问句/抽象概念时）。
    #
    # 更信任密集向量，因为 BM25 不理解语义。
    hybrid_semantic_dense_weight: float = 0.8
    hybrid_semantic_sparse_weight: float = 0.2


    # 热度在最终分里的权重。0 = 纯语义相似度。
    #
    # 0.15 而非更高：热度是辅助信号。给太高会让"经常被召回的记忆"
    # 持续霸榜，形成正反馈 —— 越被召回越容易被召回，新记忆永远出不来。
    hotness_weight: float = 0.15
    # 热度的时间衰减半衰期（天）。与 OpenViking 的 DEFAULT_HALF_LIFE_DAYS 一致。
    hotness_half_life_days: float = 7.0

    # ── 自动提交策略 ──────────────────────────────────────
    #
    # 自动触发记忆提取的条件。满足任一条件即触发。
    # 参考 OpenViking 的 auto_commit_policy.py。

    # 待处理 token 数阈值。默认 10,000 (OpenViking 的 DEFAULT_PENDING_TOKEN_THRESHOLD)。
    auto_commit_pending_token_threshold: int = 10_000

    # 是否启用上下文窗口百分比策略。
    auto_commit_use_context_percentage: bool = True

    # 上下文窗口使用率阈值（0.0 - 1.0）。默认 0.80 (80%)。
    # 多智能体时取最小窗口。
    auto_commit_context_usage_percentage: float = 0.80

    # 待处理消息数量阈值。默认 50 (OpenViking 的 DEFAULT_MESSAGE_COUNT_THRESHOLD)。
    auto_commit_message_count_threshold: int = 50

    # 提取时保留最近消息数（不提取）。默认 2 (OpenViking 的 DEFAULT_KEEP_RECENT_COUNT)。
    auto_commit_keep_recent_count: int = 2

    # 最小提取间隔（秒）。防止频繁提交。默认 300 (5 分钟)。
    auto_commit_min_interval_seconds: int = 300


class WebSearchConfig(BaseModel):
    backend: str = "none"
    tavily_api_key: str = ""
    searxng_url: str = ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # 绝对路径。相对路径 ".env" 按【进程 cwd】解析：
        # 从项目根启动能读到，从 backend/ 启动读不到，从其它任何目录启动也读不到。
        # 读不到时全部回落默认值，表现为"ENCRYPTION_KEY 缺失拒绝启动"或"连不上模型",
        # 而真正的原因是"配置根本没加载" —— 极难排查。
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        # 必须加前缀。顶层分组名是 app/agent/llm/security 这类通用词，
        # 不加前缀时【系统里任意同名环境变量都会覆盖整个分组】。
        # 实测：本机存在 AGENT=1，启动直接崩在
        # "Input should be a valid dictionary or instance of AgentConfig, input_value=1"。
        # 报错指向 AgentConfig，但真正原因是一个与本项目无关的环境变量。
        env_prefix="JEEVES_",
        extra="ignore",
    )

    app: AppConfig = Field(default_factory=AppConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    websearch: WebSearchConfig = Field(default_factory=WebSearchConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)

    api_prefix: str = "/api"

    # ── 派生路径。全部基于 PROJECT_ROOT，不受进程 cwd 影响 ──
    @property
    def env_file_path(self) -> Path:
        return PROJECT_ROOT / ".env"

    @property
    def data_dir(self) -> Path:
        return PROJECT_ROOT / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jeeves.db"

    @property
    def db_dsn(self) -> str:
        # 绝对路径。用相对路径的话，从别的目录启动服务会在那里新建一个空库,
        # 表现为"我的会话全没了"。
        return f"sqlite+aiosqlite:///{self.db_path.as_posix()}"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def temp_dir(self) -> Path:
        """
        被截断的完整命令输出落在这里。

        放 data/ 下而不是系统临时目录：这些文件要能被 read_file 读到，
        而 read_file 走路径白名单。系统临时目录不在白名单里，
        模型拿到路径也读不了 —— 那落盘就白做了。
        """
        return self.data_dir / "tmp"

    @property
    def workspace_dir(self) -> Path:
        return PROJECT_ROOT / "workspace"

    @property
    def memory_dir(self) -> Path:
        return self.data_dir / "memory"

    @property
    def skills_dir(self) -> Path:
        return PROJECT_ROOT / "skills"

    @property
    def agents_dir(self) -> Path:
        """子智能体定义（md + frontmatter）。用户在这里覆盖内置 spec。"""
        return PROJECT_ROOT / "agents"

    @property
    def personas_dir(self) -> Path:
        return PROJECT_ROOT / "personas"

    @property
    def config_dir(self) -> Path:
        return PROJECT_ROOT / "config"

    @property
    def prompts_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent / "prompts"

    @property
    def frontend_dist(self) -> Path:
        return PROJECT_ROOT / "frontend" / "dist"

    @property
    def is_localhost(self) -> bool:
        return self.app.host in ("127.0.0.1", "localhost", "::1")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
