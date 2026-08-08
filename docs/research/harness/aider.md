# Aider 驾驭工程深度研究

> 源码版本：Aider-AI/aider (latest, 2026-08 snapshot)
> 定位：CLI AI 结对编程工具，Stars 35K+，纯 Python 实现

---

## 一、System Prompt 设计

### 1.1 整体架构

Aider 的 prompt 体系采用**三层注入**模式：

```
┌──────────────────────────────────────────────┐
│  main_system (一次性)                         │
│  - 角色设定：expert software developer        │
│  - {final_reminders} 占位符                    │
│  - {shell_cmd_prompt} 动态注入                 │
├──────────────────────────────────────────────┤
│  example_messages (few-shot)                  │
│  - 精确演示 SEARCH/REPLACE 格式                │
│  - 可按 examples_as_sys_msg 配置折叠进 system  │
├──────────────────────────────────────────────┤
│  system_reminder (每轮注入)                    │
│  - 硬约束规则                                  │
│  - 格式检查清单                                │
│  - "ONLY EVER RETURN CODE IN SEARCH/REPLACE"  │
└──────────────────────────────────────────────┘
```

### 1.2 EditBlock 的 main_system 解析

来自 `aider/coders/editblock_prompts.py`:

```python
main_system = """Act as an expert software developer.
Always use best practices when coding.
Respect and use existing conventions, libraries, etc that are already present in the code base.
{final_reminders}
Take requests for changes to the supplied code.
If the request is ambiguous, ask questions.

Once you understand the request you MUST:
1. Decide if you need to propose *SEARCH/REPLACE* edits to any files that haven't been added to the chat...
2. Think step-by-step and explain the needed changes in a few short sentences.
3. Describe each change with a *SEARCH/REPLACE block* per the examples below.

All changes to files must use this *SEARCH/REPLACE block* format.
ONLY EVER RETURN CODE IN A *SEARCH/REPLACE BLOCK*!
{shell_cmd_prompt}
"""
```

**关键设计要点**：

1. **"先申请文件，再编辑"** — 第1步强制模型先告知用户需要编辑哪些文件，让用户 `/add` 后再进行编辑。这避免了模型盲目修改未加载的文件。

2. **"Think step-by-step"** — 第2步要求模型先解释，这强制模型进行思维链推理，提高编辑质量。

3. **Shell 命令可选注入** — `{shell_cmd_prompt}` 根据 `suggest_shell_commands` 开关动态注入 shell 相关提示。

### 1.3 system_reminder 的硬约束规则（每轮重复注入）

```python
system_reminder = """# *SEARCH/REPLACE block* Rules:

Every *SEARCH/REPLACE block* must use this format:
1. The *FULL* file path alone on a line, verbatim. No bold asterisks, no quotes...
2. The opening fence and code language, eg: {fence[0]}python
3. The start of search block: <<<<<<< SEARCH
4. A contiguous chunk of lines to search for in the existing source code
5. The dividing line: =======
6. The lines to replace into the source code
7. The end of the replace block: >>>>>>> REPLACE
8. The closing fence: {fence[1]}

Every *SEARCH* section must *EXACTLY MATCH* the existing file content,
character for character, including all comments, docstrings, etc.

*SEARCH/REPLACE* blocks will *only* replace the first match occurrence.
Keep *SEARCH/REPLACE* blocks concise.
Break large blocks into a series of smaller blocks.

To move code within a file, use 2 *SEARCH/REPLACE* blocks:
1 to delete it from its current location, 1 to insert it in the new location.

ONLY EVER RETURN CODE IN A *SEARCH/REPLACE BLOCK*!
"""
```

**设计哲学**：

- **精确匹配（Exact Match）是核心原则**：`SEARCH` 段必须与文件内容逐字符相同。这避免了 fuzzy matching 的歧义和不确定性——模型自己必须精确引用文件内容，如果匹配失败，错误信息会反馈给模型让它在下一轮修正。

- **每个 block 只替换第一个匹配**：如果需要替换多处，需要多个唯一化的 SEARCH block。这不是限制，而是防止意外替换的策略。

- **代码移动需要 2 个 block**：删除 + 插入，清晰无歧义。

- **system_reminder 每轮都在最终 user message 后拼接**（`reminder = "user"` 模式），或者作为独立 system message（`reminder = "sys"` 模式），确保模型不会忘记格式规则。

### 1.4 {fence} 变量的自适应

`base_coder.py` 中的 fence 选择逻辑：

```python
all_fences = [
    ("`" * 3, "`" * 3),      # 三反引号（默认）
    ("`" * 4, "`" * 4),      # 四反引号（当文件内容含三反引号时）
    ("<source>", "</source>"),
    ("<code>", "</code>"),
    ("<pre>", "</pre>"),
    ("<codeblock>", "</codeblock>"),
    ("<sourcecode>", "</sourcecode>"),
]
```

启动时会扫描所有已加载文件的全部内容，**选择第一个不在文件内容中出现的 fence**。这种设计避免了模型生成的文件内容与 fence 冲突。

### 1.5 不同 Coder 的 Prompt 差异

| Coder 类型 | edit_format | Prompt 策略 |
|-----------|-------------|-------------|
| EditBlockCoder | `diff` | SEARCH/REPLACE 块，要求精确匹配 |
| WholeFileCoder | `whole` | 输出完整文件内容，最安全但 token 消耗大 |
| UnifiedDiffCoder | `udiff` | 类似 `diff -U0` 的 unified diff 格式 |
| ArchitectCoder | `architect` | 只输出指令描述，不写代码 |
| EditorDiffFencedCoder | `editor-diff` | 接收 architect 指令，生成 SEARCH/REPLACE |

### 1.6 WholeFile 的 system_reminder

```python
# WholeFile 要求模型返回完整文件
"To suggest changes to a file you MUST return the entire content of the updated file.
*NEVER* skip, omit or elide content from a *file listing* using '...'"
```

**设计意图**：WholeFile 用在模型能力较弱或格式解析复杂时（如 gpt-3.5-turbo），它不依赖解析，直接覆盖文件，容错性最高。

### 1.7 对 Jeeves 的启示

- **三层注入模式**值得学习：main_system（角色+少样本）+ system_reminder（每轮硬规则）的结构确保了格式遵守
- **fence 自适应**机制可以避免代码块冲突
- **"先申请再编辑"** 的文件模型权限控制比直接允许编辑所有文件更安全
- system_reminder 的**"每轮提醒"**是成本最低的格式保证手段

---

## 二、编辑格式体系

### 2.1 四种核心 Coder 类

Aider 有 **13 种 Coder 子类**，核心是这 4 种：

```
Coder (抽象基类)
├── EditBlockCoder     (edit_format="diff")     ← 主力，SEARCH/REPLACE
├── WholeFileCoder     (edit_format="whole")    ← 最安全，完整文件
├── UnifiedDiffCoder   (edit_format="udiff")    ← diff -U0 风格
├── ArchitectCoder     (edit_format="architect") ← 双模型协作
├── EditorEditBlockCoder (edit_format="editor-diff")  ← 接收架构指令
├── EditorDiffFencedCoder (edit_format="editor-diff-fenced")
└── EditorWholeFileCoder  (edit_format="editor-whole")
```

### 2.2 SEARCH/REPLACE 块的格式要求

来自 `editblock_coder.py` 的解析正则：

```python
HEAD = r"^<{5,9} SEARCH>?\s*$"     # 至少5个<, 最多9个
DIVIDER = r"^={5,9}\s*$"           # 至少5个=, 最多9个
UPDATED = r"^>{5,9} REPLACE\s*$"   # 至少5个>, 最多9个
```

**解析器设计**：使用正则分块，而非严格的单一格式。`5-9` 个符号的柔性匹配可以容忍模型的小偏差（如 6 个 `<` 或 7 个 `>`）。

### 2.3 精确匹配的实现

`editblock_coder.py` 中的 `do_replace()` 尝试多种策略，但有优先级顺序：

```python
def do_replace(fname, content, before_text, after_text, fence=None):
    before_text = strip_quoted_wrapping(before_text, fname, fence)  # 去包装
    after_text = strip_quoted_wrapping(after_text, fname, fence)
    
    if not fname.exists() and not before_text.strip():
        # 创建新文件：SEARCH 为空
        fname.touch()
        content = ""
    
    if not before_text.strip():
        new_content = content + after_text  # 追加
    else:
        new_content = replace_most_similar_chunk(content, before_text, after_text)
```

`replace_most_similar_chunk` 的尝试顺序：

1. **Perfect match**（精确逐行匹配）— 第一优先级
2. **Leading whitespace 容错** — 如果精确匹配失败，尝试忽略前导空白
3. **去除首行空行** — GPT 有时会多余加空行
4. **`...` 省略号支持** — `try_dotdotdots()` 用 split-by-`...` 做分段匹配
5. **Fuzzy matching**（已在当前版本中 disabled，代码中有 `return` 提前退出）

**关键设计决策**：Fuzzy matching 代码存在但已禁用（`replace_closest_edit_distance` 被跳过）。注释中 `return` 在模糊匹配前说明当前版本**放弃了模糊匹配**。设计哲学从 "尽力匹配" 转向 "精确匹配或报错反馈"——让模型自己修正而非系统默默猜测。

### 2.4 解析容错：`...` 省略号

```python
def try_dotdotdots(whole, part, replace):
    dots_re = re.compile(r"(^\s*\.\.\.\n)", re.MULTILINE | re.DOTALL)
    part_pieces = re.split(dots_re, part)
    replace_pieces = re.split(dots_re, replace)
    # 用 ... 分割，逐段精确匹配和替换
    # 如果任一段匹配失败或出现多次，抛出 ValueError
```

当模型用 `...` 表示 "此处省略 N 行" 时，Aider 能智能处理：将 SEARCH 按 `...` 分割，每段独立精确匹配后替换。

### 2.5 SEARCH 失败时的错误反馈

```python
res = f"# {len(failed)} SEARCH/REPLACE blocks failed to match!\n"
for edit in failed:
    res += f"""
## SearchReplaceNoExactMatch: This SEARCH block failed to exactly match lines in {path}
<<<<<<< SEARCH
{original}=======
{updated}>>>>>>> REPLACE

"""
    # 提供 "Did you mean..." 建议
    did_you_mean = find_similar_lines(original, content)
    if did_you_mean:
        res += f"Did you mean to match some of these actual lines from {path}?\n..."
```

**设计亮点**：失败时不仅告诉模型 "失败了"，还返回相似行作为 "Did you mean" 提示。这是一种优雅的反馈循环——模型在下轮可以直接用这些行修正 SEARCH 块。

### 2.6 model-settings.yml 的模型-格式匹配

来自 `aider/resources/model-settings.yml`（3128行，覆盖几乎所有模型）：

```yaml
- name: gpt-4o
  edit_format: diff        # ← SEARCH/REPLACE
  weak_model_name: gpt-4o-mini
  use_repo_map: true
  lazy: true
  reminder: sys
  examples_as_sys_msg: true
  editor_edit_format: editor-diff

- name: gpt-4-turbo
  edit_format: udiff       # ← 旧模型用 udiff

- name: o1-preview
  edit_format: architect   # ← o1 推理模型用 architect 模式
  editor_model_name: gpt-4o
  editor_edit_format: editor-diff

- name: claude-3.5-sonnet
  edit_format: diff
  use_repo_map: true
  examples_as_sys_msg: true
```

`models.py` 中还有 `apply_generic_model_settings()` 做运行时匹配，根据模型名模式自动设置参数（如 deepseek v3 用 `diff`，deepseek r1 用 `diff` + `reasoning_tag = "think"`）。

### 2.7 对 Jeeves 的启示

- **多编辑格式**+模型自动匹配是一种优秀的策略：根据模型能力自动选择最佳输出格式
- **严禁模糊匹配**的哲学值得学习：宁可让模型修正也不默默猜错
- **错误反馈循环**（Did you mean + 精确行）是保证编辑质量的核心机制
- **省略号支持**是一种实用的容错设计

---

## 三、RepoMap — Tree-sitter + PageRank

### 3.1 整体架构

```
Python 源文件
     │
     ▼
Tree-sitter AST 解析 (Query .scm 文件)
     │
     ▼
提取符号：def(定义) + ref(引用)
     │  Tag = namedtuple("Tag", "rel_fname fname line name kind")
     ▼
构建 networkx MultiDiGraph
     │  节点 = 文件
     │  边   = 符号引用关系（权重 = √引用次数 × 乘数）
     ▼
PageRank 排序 → ranked_tags
     │
     ▼
Token budget 二分查找 → 最优输出树
     ▼
TreeContext 渲染 → repo map 文本
```

### 3.2 Tree-sitter 符号提取

来自 `repomap.py` 的 `get_tags_raw()`：

```python
def get_tags_raw(self, fname, rel_fname):
    lang = filename_to_lang(fname)
    language = get_language(lang)
    parser = get_parser(lang)
    
    query_scm = get_scm_fname(lang)  # 加载 {lang}-tags.scm
    query_scm = query_scm.read_text()
    
    code = self.io.read_text(fname)
    tree = parser.parse(bytes(code, "utf-8"))
    
    captures = self._run_captures(Query(language, query_scm), tree.root_node)
    
    for node, tag in all_nodes:
        if tag.startswith("name.definition."):
            kind = "def"
        elif tag.startswith("name.reference."):
            kind = "ref"
        
        yield Tag(rel_fname=rel_fname, fname=fname, name=node.text, kind=kind, line=...)
```

`.scm` 查询文件定义了每种语言的符号提取规则。Aider 内置了对 ~60 种语言的支持。

**Pygments fallback**：如果某语言只有 def 没有 ref（如某些 C++ 配置），用 Pygments 做词法分析回填引用信息。

### 3.3 PageRank 排序

来自 `get_ranked_tags()`：

```python
def get_ranked_tags(self, chat_fnames, other_fnames, mentioned_fnames, mentioned_idents):
    import networkx as nx
    
    defines = defaultdict(set)    # ident → {定义该符号的文件}
    references = defaultdict(list) # ident → [引用该符号的文件]
    personalization = dict()       # 个性化 PageRank 种子
    
    # 1. 遍历所有文件，收集 def/ref
    for fname in fnames:
        tags = list(self.get_tags(fname, rel_fname))
        for tag in tags:
            if tag.kind == "def":
                defines[tag.name].add(rel_fname)
            elif tag.kind == "ref":
                references[tag.name].append(rel_fname)
    
    # 2. 构建有向图
    G = nx.MultiDiGraph()
    
    for ident in idents:
        definers = defines[ident]
        mul = 1.0
        
        # 权重调整策略
        if ident in mentioned_idents:
            mul *= 10       # 用户提到的符号 ×10
        if (is_snake or is_kebab or is_camel) and len(ident) >= 8:
            mul *= 10       # 长标识符 ×10（更可能是重要符号）
        if ident.startswith("_"):
            mul *= 0.1      # 私有符号 ×0.1
        if len(defines[ident]) > 5:
            mul *= 0.1      # 定义太多次 = 太通用 ×0.1
        
        for referencer, num_refs in Counter(references[ident]).items():
            for definer in definers:
                use_mul = mul
                if referencer in chat_rel_fnames:
                    use_mul *= 50  # chat 中的文件引用 ×50！
                
                num_refs = math.sqrt(num_refs)  # sqrt 压缩高频引用
                G.add_edge(referencer, definer, weight=use_mul * num_refs, ident=ident)
    
    # 3. 运行 PageRank
    ranked = nx.pagerank(G, weight="weight", personalization=personalization)
    
    # 4. 按 rank 分布到每个定义
    for src in G.nodes:
        src_rank = ranked[src]
        total_weight = sum(data["weight"] for _, _, data in G.out_edges(src, data=True))
        for _, dst, data in G.out_edges(src, data=True):
            data["rank"] = src_rank * data["weight"] / total_weight
            ranked_definitions[(dst, ident)] += data["rank"]
```

**PageRank 个性化种子（Personalization）**：

- **Chat 中的文件**获得更高的 personalization 值，这意味着它们作为 "teleport" 的目标，将 PageRank 质量导向与当前任务相关的文件
- 用户消息中提到的文件名/标识符也会提升对应节点的初始权重

### 3.4 Token Budget 动态调整

这是 RepoMap 最精巧的部分——**二分查找最优输出大小**：

```python
def get_ranked_tags_map_uncached(self, ...):
    ranked_tags = self.get_ranked_tags(...)  # PageRank 排序后的 tag 列表
    
    num_tags = len(ranked_tags)
    lower_bound = 0
    upper_bound = num_tags
    best_tree = None
    best_tree_tokens = 0
    
    middle = min(int(max_map_tokens // 25), num_tags)  # 初始猜测
    while lower_bound <= upper_bound:
        tree = self.to_tree(ranked_tags[:middle], chat_rel_fnames)
        num_tokens = self.token_count(tree)
        
        pct_err = abs(num_tokens - max_map_tokens) / max_map_tokens
        ok_err = 0.15  # 15% 容差
        
        if (num_tokens <= max_map_tokens and num_tokens > best_tree_tokens) or pct_err < ok_err:
            best_tree = tree
            best_tree_tokens = num_tokens
            if pct_err < ok_err:
                break  # 在 15% 误差范围内，停止搜索
        
        if num_tokens < max_map_tokens:
            lower_bound = middle + 1
        else:
            upper_bound = middle - 1
        middle = int((lower_bound + upper_bound) // 2)
    
    return best_tree
```

**关键设计**：
- 初始 `middle = max_map_tokens // 25`，假设每个 tag 约 25 tokens（一个经验估计）
- 二分查找找到不超过 token budget 的最大 tag 集合
- 15% 容差避免过度搜索

### 3.5 缓存与刷新策略

```python
class RepoMap:
    TAGS_CACHE_DIR = ".aider.tags.cache.v4"  # 基于 tree-sitter 版本的缓存
    
    def __init__(self, ..., refresh="auto"):
        self.refresh = refresh  # "auto" | "manual" | "always" | "files"
        self.map_processing_time = 0
    
    def get_ranked_tags_map(self, ..., force_refresh=False):
        if self.refresh == "auto":
            use_cache = self.map_processing_time > 1.0  # 处理时间 >1秒才缓存
        elif self.refresh == "manual":
            return self.last_map  # 始终用上次的 map
        elif self.refresh == "always":
            use_cache = False
        elif self.refresh == "files":
            use_cache = True  # 文件不变则复用
```

### 3.6 Tree 输出格式

`to_tree()` 将排序后的 tags 渲染成树状文本：

```
some_file.py:
⋮...
class Foo:
⋮...
    def bar(self):
    def baz(self):
⋮...

another_file.py:
⋮...
import some_file
⋮...
```

使用 `grep_ast.TreeContext` 做代码骨架展示，只显示关键符号周围的代码行（lines of interest），其余用 `⋮...` 省略。

### 3.7 相比简单文件列表的优势

| 维度 | 简单文件列表 | Aider RepoMap |
|------|-------------|---------------|
| 信息密度 | 只有文件名 | 文件名 + 关键符号 + 骨架代码 |
| 关联性 | 无结构 | 符号引用图 + PageRank 排序 |
| Token 利用率 | 低（无关文件占 token） | 高（按重要性排序，budget 二分） |
| 上下文感知 | 无 | 用户提到的标识符 ×10，chat 文件 ×50 |
| 缓存 | 无 | 基于 mtime 的 SQLite 缓存 |

### 3.8 对 Jeeves 的启示

- **符号级代码图谱 + PageRank** 是提供 "代码上下文" 的高级方案，远优于简单的文件列表
- **权重调整策略**（用户提及 ×10，chat 文件 ×50，私有 ×0.1，高频 ×0.1）是手工调优的结果，可借鉴
- **二分查找 token budget** 是一种通用的 "在 token 限制下最大化信息量" 的策略
- **Tree-sitter .scm 查询** 是语言感知代码分析的标准方案

---

## 四、Architect/Editor 双模型

### 4.1 架构设计

Architect/Editor 模式在 Aider 中被实现为一种**串行双阶段流程**：

```
用户请求
    │
    ▼
ArchitectCoder (强推理模型，如 o1-preview / Claude Opus)
    │  输出：自然语言代码修改指令
    │  "Modify foo() in app.py to add error handling..."
    ▼
用户确认？ ───否──→ 结束
    │ 是
    ▼
Editor Coder (编辑模型，如 gpt-4o / Claude Sonnet)
    │  编辑格式：editor-diff (或 editor-diff-fenced / editor-whole)
    │  输出：SEARCH/REPLACE 块
    ▼
应用编辑 + Git 提交
```

### 4.2 Architect 的 Prompt

来自 `aider/coders/architect_prompts.py`：

```python
main_system = """Act as an expert architect engineer and provide direction to your editor engineer.
Study the change request and the current code.
Describe how to modify the code to complete the request.
The editor engineer will rely solely on your instructions, so make them unambiguous and complete.
Explain all needed code changes clearly and completely, but concisely.
Just show the changes needed.

DO NOT show the entire updated function/file/etc!

Always reply to the user in {language}.
"""
```

**关键设计**：
- "DO NOT show the entire updated function" — Architect 不输出代码，只输出修改指令
- "make them unambiguous and complete" — 指令是 Editor 的唯一依据
- system_reminder 为空 — Architect 没有格式约束，自由输出自然语言

### 4.3 ArchitectCoder 实现

来自 `aider/coders/architect_coder.py`（仅 48 行）：

```python
class ArchitectCoder(AskCoder):
    edit_format = "architect"
    gpt_prompts = ArchitectPrompts()
    
    def reply_completed(self):
        content = self.partial_response_content
        
        # 用户确认
        if not self.auto_accept_architect and not self.io.confirm_ask("Edit the files?"):
            return
        
        # 选择 Editor 模型
        editor_model = self.main_model.editor_model or self.main_model
        
        kwargs["main_model"] = editor_model
        kwargs["edit_format"] = self.main_model.editor_edit_format
        kwargs["map_tokens"] = 0          # Editor 不需要 repo map
        kwargs["summarize_from_coder"] = False
        
        # 创建新的 Editor Coder
        editor_coder = Coder.create(io=self.io, from_coder=self, **kwargs)
        editor_coder.cur_messages = []     # 清空消息，只传指令
        editor_coder.done_messages = []
        
        # 运行 Editor
        editor_coder.run(with_message=content, preproc=False)
        
        # 将结果回传到 Architect
        self.move_back_cur_messages("I made those changes to the files.")
```

**关键设计点**：

1. **map_tokens=0** — Editor 不需要 repo map，因为 Architect 已经告诉它做什么，节省 token
2. **cur_messages=[]** — Editor 不继承 Architect 的对话历史，只接收修改指令
3. **继承 total_cost** — 用户看到的成本是两者的总和

### 4.4 模型配置

来自 `models.py` 和 `model-settings.yml`：

```python
@dataclass
class ModelSettings:
    editor_model_name: Optional[str] = None   # Editor 模型
    editor_edit_format: Optional[str] = None  # Editor 使用的编辑格式

# 典型配置
# o1-preview: architect 模式，Editor 用 gpt-4o
# gpt-4o:     同时作为 editor_model（自己编辑自己）
```

`get_editor_model()` 方法：
```python
def get_editor_model(self, provided_editor_model_name, editor_edit_format):
    # 如果未指定 editor_model，使用自身
    if not self.editor_model_name or self.editor_model_name == self.name:
        self.editor_model = self
    else:
        self.editor_model = Model(self.editor_model_name, editor_model=False)
    
    # 自动推导 editor_edit_format
    if not self.editor_edit_format:
        if self.editor_model.edit_format in ("diff", "whole", "diff-fenced"):
            self.editor_edit_format = "editor-" + self.editor_model.edit_format
```

### 4.5 成本优化策略

```
Architect 用最强的推理模型（$15/M tokens）
    ↓ 只输出几段自然语言（~500 tokens）
Editor 用中等模型（$2.5/M tokens）
    ↓ 输出大量代码编辑（~2000 tokens）

总成本 ≈ 15×0.5K + 2.5×2K ≈ $12.5
vs 全量最强模型 ≈ 15×2.5K ≈ $37.5
```

**节省约 67% 成本**，同时因为 Architect 推理质量更高，编辑质量也更好。

### 4.6 对 Jeeves 的启示

- **思考/执行分离**是 AI 编程工具的成熟模式，Jeeves 可以借鉴
- Editor 模型不需要上下文（map_tokens=0），因为指令已经包含所有需要的信息
- **成本优化**的量化方法值得学习：推理用强模型，执行用中模型
- **用户确认环节**（"Edit the files?"）保持了人在回路的控制

---

## 五、Git 原生集成

### 5.1 自动 Commit 机制

来自 `base_coder.py`：

```python
def send_message(self, inp):
    # ... LLM 调用 ...
    
    edited = self.apply_updates()   # 应用文件编辑
    
    if edited:
        self.aider_edited_files.update(edited)
        saved_message = self.auto_commit(edited)  # 自动提交！
        
        self.move_back_cur_messages(saved_message)  # 将 commit 信息告知模型
    
    if edited and self.auto_lint:
        lint_errors = self.lint_edited(edited)
        self.auto_commit(edited, context="Ran the linter")  # Lint 后再提交
```

`auto_commit()` 实现：
```python
def auto_commit(self, edited, context=None):
    if not self.repo or not self.auto_commits or self.dry_run:
        return
    
    if not context:
        context = self.get_context_from_history(self.cur_messages)
    
    res = self.repo.commit(fnames=edited, context=context, aider_edits=True, coder=self)
    if res:
        self.show_auto_commit_outcome(res)
        commit_hash, commit_message = res
        return self.gpt_prompts.files_content_gpt_edits.format(
            hash=commit_hash, message=commit_message,
        )
```

**`dirty_commit()`** — 在已修改的文件上自动保存：
```python
def dirty_commit(self):
    """在 user 修改过的文件上做自动提交，防止 aider 覆盖用户工作"""
    if not self.dirty_commits:
        return
    self.repo.commit(fnames=self.need_commit_before_edits, coder=self)
```

### 5.2 Commit Message 生成

来自 `repo.py` 和 `prompts.py`：

```python
commit_system = """You are an expert software engineer that generates concise,
one-line Git commit messages based on the provided diffs.
The commit message should be structured as follows: <type>: <description>
Use these for <type>: fix, feat, build, chore, ci, docs, style, refactor, perf, test

Ensure the commit message:
- Starts with the appropriate prefix.
- Is in the imperative mood ('add feature' not 'added feature').
- Does not exceed 72 characters.

Reply only with the one-line commit message, without any additional text.
"""
```

模型回退策略：
```python
def get_commit_message(self, diffs, context, user_language=None):
    for model in self.models:  # [weak_model, main_model]
        commit_message = model.simple_send_with_retries(messages)
        if commit_message:
            break  # 先用 weak/cheap 模型，失败再用主模型
```

**设计要点**：Commit message 生成使用**模型级联回退**——先尝试 weak_model（便宜），失败再用 main_model。这种策略在 90%+ 的情况下用 cheap 模型就够了。

### 5.3 Undo 命令

来自 `commands.py`：

```python
def raw_cmd_undo(self, args):
    # 1. 安全检查：最后 commit 必须是 aider 做的
    if last_commit_hash not in self.coder.aider_commit_hashes:
        self.io.tool_error("The last commit was not made by aider...")
        return
    
    # 2. 安全检查：不能是 merge commit（>1 parent）
    
    # 3. 安全检查：文件不能有未提交的修改
    
    # 4. 安全检查：文件在上一个 commit 中必须存在
    
    # 5. 安全检查：不能已推送到 origin
    
    # 6. 逐个文件 checkout HEAD~1
    for file_path in changed_files_last_commit:
        self.coder.repo.repo.git.checkout("HEAD~1", file_path)
    
    # 7. Soft reset HEAD
    self.coder.repo.repo.git.reset("--soft", "HEAD~1")
```

**安全设计**：Undo 有 5 层安全检查，确保只在安全条件下执行。使用 `checkout` 逐个文件恢复 + `reset --soft`（保留 stage 区）的策略。

### 5.4 Commit 归因（Attribution）

```python
# 三种归因策略
# 1. --attribute-author:  Author = "UserName (aider)"
# 2. --attribute-committer: Committer = "UserName (aider)"  
# 3. --attribute-co-authored-by: commit message 尾部加
#    Co-authored-by: aider (model-name) <aider@aider.chat>
```

复杂的决策矩阵（见 `repo.py` 的 commit 方法，有 60+ 行注释说明逻辑）确保归因在 aider-edits 和 user-edits 之间正确区分。

### 5.5 对 Jeeves 的启示

- **每轮自动 commit** 是最安全和最实用的设计：undo 成为可能，用户随时可以回退
- **Undo 的多层安全检查**确保不会破坏用户数据
- **模型级联回退**（weak → main）在 commit message 生成这种非关键路径上很实惠
- **归因标记**让用户清楚哪些更改是 AI 做的

---

## 六、Aider 的设计哲学

### 6.1 "人在回路" vs "全自主 Agent"

Aider 的核心理念是 **AI-assisted**，不是 **AI-autonomous**：

```
Aider 的 "人在回路" 表现：

1. 文件权限控制
   └── 模型不能编辑未加入 chat 的文件
   └── 模型需要"请求"用户添加文件

2. 编辑确认
   └── Architect 模式：用户确认后才执行编辑
   └── Shell 命令：始终需用户手动确认

3. 每轮独立
   └── 每轮编辑后自动 commit
   └── 用户可以随时 /undo
   └── 用户可以随时中断

4. 透明性
   └── 显示 diff
   └── 显示 commit hash
   └── 显示 token 成本和用量
```

### 6.2 为什么 Aider 不追求自主性

从代码设计可见几条哲学：

1. **精确性 > 自主性**：SEARCH 必须 EXACTLY MATCH，宁报错也不猜测
2. **安全性 > 效率**：放弃 Fuzzy matching、多层 undo 检查、shell 命令需确认
3. **可逆性**：Git commit 是关键——任何操作可回退
4. **用户理解的成本**：少样本示例精确演示格式，而不是依赖模型自己推断
5. **模型协议简化**：为每种模型预设最佳编辑格式，避免模型在格式选择上犯错

### 6.3 设计优缺点分析

**优点**：

| 优点 | 实现手段 |
|------|---------|
| 安全可逆 | 每轮 Git commit + /undo |
| 高成功率 | 精确匹配 + 错误反馈循环 |
| 多模型适配 | model-settings.yml + 运行时匹配 |
| 成本可控 | Architect/Editor 分离 + weak model 回退 |
| 用户可控 | 文件权限 + Shell 确认 + 随时中断 |

**缺点**：

| 缺点 | 原因 |
|------|------|
| 多文件编辑需手动 | 必须通过 `/add` 显式加载每个文件 |
| 不擅长大规模重构 | 缺少跨文件编辑协调能力 |
| 依赖 Git | 非 Git 项目功能受限 |
| O1 等模型受限 | 不支持 system prompt 的模型兼容性差 |

### 6.4 对 Jeeves 的总体启示

1. **Git 集成是 AI 编程工具的基石** — commit 提供了可逆性和可审计性
2. **精确匹配优于模糊匹配** — 在代码编辑场景中，宁可报错让 AI 修正也不猜测
3. **模型能力决定工具策略** — model-settings.yml 的中心化配置让每个模型获得最佳体验
4. **Token economy 是核心竞争力** — RepoMap 的二分查找 token budget 是信息密度的极致优化
5. **"人在回路"不是限制而是优势** — 文件权限、shell 确认、编辑确认让用户保持控制的同时获得 AI 辅助
6. **三层 prompt 注入**（main_system + examples + system_reminder）是保证输出格式的最低成本方案
7. **双模型模式**的价值在于思考质量和执行成本的分离

---

## 附录：关键文件索引

| 文件 | 功能 | 行数 |
|------|------|------|
| `aider/coders/editblock_prompts.py` | SEARCH/REPLACE 格式的 prompt | 172 |
| `aider/coders/editblock_coder.py` | SEARCH/REPLACE 解析和应用 | 657 |
| `aider/coders/wholefile_prompts.py` | WholeFile 格式的 prompt | 64 |
| `aider/coders/udiff_coder.py` | Unified diff 格式解析和应用 | 429 |
| `aider/coders/base_coder.py` | Coder 基类，agent loop 核心 | 2485 |
| `aider/coders/base_prompts.py` | 通用 prompt 模板 | 60 |
| `aider/coders/architect_coder.py` | Architect+Editor 双模型 | 48 |
| `aider/coders/architect_prompts.py` | Architect 的 prompt | 40 |
| `aider/coders/shell.py` | Shell 命令提示 | 37 |
| `aider/repomap.py` | Tree-sitter + PageRank 实现 | 867 |
| `aider/models.py` | LiteLLM 模型路由和配置 | 1338 |
| `aider/prompts.py` | 通用 prompt（commit 等） | 61 |
| `aider/repo.py` | Git 操作封装 | 622 |
| `aider/commands.py` | 命令系统（/undo 等） | 1712 |
| `aider/resources/model-settings.yml` | 模型预设配置 | 3128 |
