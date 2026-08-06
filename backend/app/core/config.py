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
    def skills_dir(self) -> Path:
        return PROJECT_ROOT / "skills"

    @property
    def macros_dir(self) -> Path:
        return PROJECT_ROOT / "macros"

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
