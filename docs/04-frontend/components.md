# 组件清单

单文件上限 300 行。超了拆。

## chat/

| 组件 | 职责 | 关键点 |
| --- | --- | --- |
| `MessageList` | 消息流容器 | `react-virtuoso` 虚拟滚动；自动滚到底但用户上滑后不强拉 |
| `MessageItem` | 单条消息分发 | 按 `role` 分发到具体组件，自身不含渲染逻辑 |
| `UserBubble` | 用户消息 | 显示引用条；右键菜单（引用此消息、从此处重发） |
| `AssistantBubble` | 助手消息 | 组合 ThinkingBlock + Markdown + ToolCallCard |
| `ThinkingBlock` | 思维链 | 默认折叠；流式时标题显示"思考中…" |
| `MarkdownRenderer` | Markdown | `rehype-sanitize` 必须开；代码块流式期不高亮 |
| `CodeBlock` | 代码块 | 复制按钮、语言标签、行号 |
| `ToolCallCard` | 通用工具卡片 | MCP 工具和未知工具都用这个 |
| `ToolCardFile` | 文件工具专属 | 显示路径 + 行数 + diff（edit_file） |
| `ToolCardExec` | 执行工具专属 | 命令 + 退出码 + 输出（可展开全部） |
| `ToolCardTodo` | Todo 工具专属 | 直接渲染成清单 |
| `ArtifactCard` | 产物 | 折叠展示 + 下载按钮 + "这是最新版"标记 |
| `SubAgentCard` | 子智能体 | 可展开看它的完整执行过程（拉它的记忆线） |
| `CompactDivider` | 压缩分隔条 | "已压缩 N 条消息"，可点开看摘要 |
| `ErrorBlock` | 错误 | 红色块 + hint + 重试按钮 |
| `ChatInput` | 输入框 | 见下 |
| `RefBar` | 引用条 | 输入框上方，显示已添加的引用，可删除 |
| `Mentioner` | 提词器 | `@` `#` `!` 三种触发 |
| `TopBar` | 顶栏 | 标题 + Todo进度 + 上下文占用 + 四个开关 |
| `SessionList` | 会话列表 | 右键菜单（固定、重命名、删除） |
| `ApprovalDialog` | 审批弹框 | 见下 |
| `InteractDialog` | 交互弹框 | text / single / multi 三种形态 |
| `WelcomeScreen` | 空状态 | 无 chat 模型时引导去设置页 |

### MessageItem 只做分发

```typescript
// 不在这里写任何渲染逻辑。加一种消息类型时只在这里加一个 case，
// 具体渲染在独立组件里 —— 否则这个文件会变成 2000 行的 if/else 山。
switch (msg.role) {
  case "user":      return <UserBubble message={msg} />;
  case "assistant": return <AssistantBubble message={msg} />;
  case "tool":      return null;   // tool 消息由 AssistantBubble 内联渲染
  case "artifact":  return <ArtifactCard message={msg} />;
  case "summary":   return <CompactDivider message={msg} />;
  case "system":    return null;   // 不显示
}
```

`role=tool` 返回 `null` 是刻意的：工具结果应该显示在触发它的 assistant 气泡**内部**，不是独立一条。`AssistantBubble` 按 `tool_calls` 的 `call_id` 去找对应的 tool 消息。

### 自动滚动的正确行为

```
用户在底部     → 新内容自动滚到底
用户上滑了     → 不自动滚，显示"回到底部"浮标
用户点浮标     → 滚到底，恢复自动
```

判定"在底部"要留容差（距底部 < 100px 算在底部）。严格判等会因为亚像素误差而失效。

这是聊天界面最容易做错的交互——强制滚动会让用户没法回看历史。

### ChatInput

要素：

- 多行 textarea，`Enter` 发送 / `Shift+Enter` 换行
- **上方可拖拽调高**（设计，看长文本时很有用）
- 左侧：附件按钮、文件引用按钮、文件夹引用按钮
- 右侧：私密开关、视觉开关、麦克风（M7）、发送/停止按钮
- 粘贴 URL 自动转为 url 引用
- 粘贴图片自动上传为附件
- 流式期间 disabled，但**停止按钮可用**

快捷键：

| 键 | 动作 |
| --- | --- |
| `Enter` | 发送 |
| `Shift+Enter` | 换行 |
| `Ctrl+K` | 切换私密模式 |
| `Ctrl+Shift+K` | 切换失忆模式 |
| `Esc` | 流式期间 = 停止；否则关闭提词器 |
| `@` `#` `!` | 触发提词器 |

### Mentioner 提词器

三种触发字符对应三类数据：

| 触发 | 数据源 | 插入结果 |
| --- | --- | --- |
| `@` | `configStore.skills` | `refs` 加一条 `{type:"skill"}` |
| `#` | `configStore.tools` | `refs` 加一条 `{type:"tool"}` |
| `!` / `！` | `configStore.macros` | `refs` 加一条 `{type:"macro"}` |

交互：上下键选择、Tab 或 Enter 确认、Esc 关闭。输入继续过滤。

**全角 `！` 也要支持**——中文输入法下打感叹号默认是全角，这是必然会遇到的。

插入后触发字符和已输入的名字从文本里**移除**，改为 `RefBar` 上的一个标签。这样输入框里留下的是干净的自然语言。

### ApprovalDialog

```
┌────────────────────────────────────┐
│ 需要确认：run_shell          [45s] │
├────────────────────────────────────┤
│ ⚠ 匹配到风险：删除根目录相关路径     │
│                                    │
│ rm -rf ./build                     │
│                                    │
│ 工作目录: D:/proj/workspace        │
├────────────────────────────────────┤
│              [拒绝]  [允许执行]     │
└────────────────────────────────────┘
```

要点：

- **风险标注放最上面**，用醒目颜色。这是用户唯一的判断依据
- 命令用等宽字体、完整显示不截断
- 倒计时可见（超时视为拒绝）
- **默认焦点在"拒绝"上**——防止用户习惯性按 Enter 就放行
- 不提供"本次会话内全部允许"的快捷选项。想要就去顶栏切 auto 模式，那是个显式的、可见的状态

最后一点：一次性的"全部允许"会让用户忘记自己放开了限制。顶栏的 auto 标记一直在那里提醒。

## todo/

| 组件 | 职责 |
| --- | --- |
| `TodoProgress` | 顶栏进度条 `[■■■□□] 3/5 · 工作中` |
| `TodoBoard` | 展开的看板，可拖拽排序 |
| `TodoItem` | 单条，可勾选/删除 |
| `TodoArchiveButton` | 全部完成后出现的"验收关闭" |

进度条**全部完成后不自动消失**，等用户点关闭。见 [../01-architecture/todo.md](../01-architecture/todo.md#验收关闭)。

## settings/

| 组件 | 对应接口 |
| --- | --- |
| `ProviderList` / `ProviderForm` | `/api/providers` |
| `ProbeDialog` | `/api/providers/probe` —— **核心交互，见下** |
| `ModelTable` | `/api/models` |
| `BindingPanel` | `/api/bindings` |
| `SkillList` / `SkillUpload` | `/api/skills` |
| `MacroList` | `/api/macros` |
| `McpServerList` | `/api/mcp/servers` |
| `PersonaEditor` | `/api/personas/{kind}` |
| `WhitelistPanel` | `/api/settings/whitelist` |
| `BlockerPanel` | `/api/settings/blocker` |
| `WorkspacePanel` | `/api/workspaces` |

### ProbeDialog 是用户体验的关键点

用户点名要"不用手动输入模型名"，这个弹框就是那个功能的落地：

```
步骤 1：填 Base URL + API Key  →  [测试连接]
步骤 2：显示拉取到的模型列表，多选勾选
        ┌──────────────────────────────────┐
        │ ☑ deepseek-chat        64K      │
        │ ☑ deepseek-reasoner    64K      │
        │ ☐ deepseek-coder       ?  ⚠     │
        └──────────────────────────────────┘
        ⚠ = 窗口大小未知，将按 32K 处理
步骤 3：为 chat 位选一个  →  [保存]
```

要点：

- `normalized_base_url` 要回显，让用户看到实际会用的地址
- 窗口未知的模型要标注（`window_source=default`）
- 失败时错误信息必须具体（见 [../03-api/endpoints-config.md](../03-api/endpoints-config.md#post-apiprovidersprobe)）
- **失败时提供"手动输入模型名"的入口**，不能是死路
- 保存后如果还没有 chat 绑定，直接在这里让用户选，不要让他再跳一次

### BindingPanel 的 compact 位提示

在 compact 位旁边放一句说明：

```
压缩模型不宜过弱。压缩出错会影响整个会话往后的全部推理，
且不会有任何报错 —— 表现为"模型突然忘了之前的约定"。
建议与对话模型同档或最多低一档。
```

这是反直觉的点，必须在 UI 上说清，否则用户一定会在这里配最便宜的模型。

## common/

| 组件 | 职责 |
| --- | --- |
| `Layout` | 整体布局 + 侧栏折叠 |
| `WarningBanner` | 常驻警示条（沙箱降级、非 localhost 绑定） |
| `EmptyState` | 空状态占位 |
| `ConfirmDialog` | 通用确认框 |
| `CopyButton` | 复制到剪贴板 |
| `TokenBadge` | token 数显示，带千分位 |
| `RelativeTime` | 相对时间（"3 分钟前"），60s 刷新一次 |

### WarningBanner 是常驻的

两种情况显示，且**不可关闭**：

1. `sandbox_fallback` 事件后：当前不是隔离环境
2. `meta.host_is_localhost === false`：服务绑到了非本机地址且无鉴权

不可关闭是刻意的。这两件事的风险是持续存在的，用户点掉之后就会忘记。

## 无障碍

不做完整 WCAG 验证（那需要辅助技术实测和专业评审），但做到基本几点：

- 所有图标按钮有 `aria-label`
- 弹框有 `role="dialog"` + `aria-modal` + 焦点陷阱（shadcn/Radix 自带）
- 键盘可完整操作主流程（发送、取消、审批、提词器选择）
- 流式内容区加 `aria-live="polite"`，但**只在 `done` 后播报一次**——每个 chunk 都播报会让屏幕阅读器疯掉
- 颜色不作为唯一信息载体（错误状态除了红色还有图标和文字）
