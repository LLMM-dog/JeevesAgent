# Jeeves API 快速上手指南

> **5 分钟掌握 Jeeves API 核心用法**

---

## 前置条件

1. Jeeves 后端已启动（默认 `http://localhost:9000`）
2. 已配置至少一个 LLM 端点和模型

---

## 快速示例

### 1. 创建会话

**请求**:
```bash
curl -X POST http://localhost:9000/api/sessions \
  -H "Content-Type: application/json" \
  -d '{
    "title": "我的第一个会话"
  }'
```

**响应**:
```json
{
  "id": "ses_abc123",
  "title": "我的第一个会话",
  "workspace_id": "",
  "pinned": false,
  "message_count": 0,
  "last_message_at": 0,
  "created_at": 1723708800,
  "approval_mode": "auto",
  "work_dir": "",
  "model_pk": "",
  "context_window": 128000,
  "private_mode": false,
  "amnesia_mode": false,
  "vision_mode": false,
  "agent_id": ""
}
```

---

### 2. 发送消息（流式）

**请求**:
```bash
curl -X POST http://localhost:9000/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "ses_abc123",
    "content": "你好，请介绍一下你自己"
  }'
```

**响应** (Server-Sent Events):
```
event: delta
data: {"content": "你"}

event: delta
data: {"content": "好"}

event: delta
data: {"content": "！我是"}

event: delta
data: {"content": " Jeeves"}

event: delta
data: {"content": "，一个"}

event: delta
data: {"content": "智能"}

event: delta
data: {"content": "助手"}

event: delta
data: {"content": "..."}

event: done
data: {"message_id": "msg_123", "tokens": {"prompt": 15, "completion": 50}}
```

---

### 3. 获取消息历史

**请求**:
```bash
curl http://localhost:9000/api/sessions/ses_abc123/messages
```

**响应**:
```json
{
  "messages": [
    {
      "id": "msg_122",
      "seq": 1,
      "role": "user",
      "agent_name": "",
      "content": "你好，请介绍一下你自己",
      "reasoning": null,
      "tool_calls": null,
      "tool_call_id": null,
      "tool_name": null,
      "tool_display": null,
      "is_error": false,
      "refs": null,
      "attachments": null,
      "artifact_kind": null,
      "artifact_path": null,
      "run_id": "run_xyz",
      "span_id": "span_001",
      "prompt_tokens": null,
      "completion_tokens": null,
      "created_at": 1723708801
    },
    {
      "id": "msg_123",
      "seq": 2,
      "role": "assistant",
      "agent_name": "Jeeves",
      "content": "你好！我是 Jeeves，一个智能助手...",
      "reasoning": null,
      "tool_calls": null,
      "tool_call_id": null,
      "tool_name": null,
      "tool_display": null,
      "is_error": false,
      "refs": null,
      "attachments": null,
      "artifact_kind": null,
      "artifact_path": null,
      "run_id": "run_xyz",
      "span_id": "span_002",
      "prompt_tokens": 15,
      "completion_tokens": 50,
      "created_at": 1723708802
    }
  ],
  "total": 2
}
```

---

### 4. 修改会话配置

**请求**:
```bash
curl -X PATCH http://localhost:9000/api/sessions/ses_abc123 \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Python 学习助手",
    "work_dir": "/workspace/python-project",
    "approval_mode": "manual"
  }'
```

**响应**:
```json
{
  "id": "ses_abc123",
  "title": "Python 学习助手",
  "work_dir": "/workspace/python-project",
  "approval_mode": "manual",
  // ... 其他字段
}
```

---

### 5. 列出会话

**请求**:
```bash
curl http://localhost:9000/api/sessions
```

**响应**:
```json
{
  "sessions": [
    {
      "id": "ses_abc123",
      "title": "Python 学习助手",
      "workspace_id": "",
      "pinned": false,
      "message_count": 2,
      "last_message_at": 1723708802,
      "created_at": 1723708800
    }
  ],
  "total": 1
}
```

---

## 前端集成示例

### TypeScript + Fetch

```typescript
// 配置
const API_BASE = 'http://localhost:9000/api';

// 类型定义
interface SessionDetail {
  id: string;
  title: string;
  workspace_id: string;
  pinned: boolean;
  message_count: number;
  last_message_at: number;
  created_at: number;
  approval_mode: 'auto' | 'manual';
  work_dir: string;
  model_pk: string;
  context_window: number;
  private_mode: boolean;
  amnesia_mode: boolean;
  vision_mode: boolean;
  agent_id: string;
}

interface MessageOut {
  id: string;
  seq: number;
  role: 'user' | 'assistant' | 'tool';
  content: string;
  tool_calls?: any[];
  refs?: any[];
  attachments?: string[];
  created_at: number;
}

// API 客户端
class JeevesAPI {
  private base: string;

  constructor(baseURL: string = API_BASE) {
    this.base = baseURL;
  }

  // 创建会话
  async createSession(title: string): Promise<SessionDetail> {
    const response = await fetch(`${this.base}/sessions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title }),
    });

    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.detail?.message || 'Request failed');
    }

    return response.json();
  }

  // 获取会话列表
  async getSessions(): Promise<SessionDetail[]> {
    const response = await fetch(`${this.base}/sessions`);
    const data = await response.json();
    return data.sessions;
  }

  // 获取消息历史
  async getMessages(sessionId: string): Promise<MessageOut[]> {
    const response = await fetch(`${this.base}/sessions/${sessionId}/messages`);
    const data = await response.json();
    return data.messages;
  }

  // 流式对话
  async chat(
    sessionId: string,
    content: string,
    onDelta: (text: string) => void,
    onDone: (data: any) => void,
    onError: (error: Error) => void
  ): Promise<void> {
    const response = await fetch(`${this.base}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, content }),
    });

    if (!response.ok) {
      const error = await response.json();
      onError(new Error(error.detail?.message || 'Chat failed'));
      return;
    }

    const reader = response.body!.getReader();
    const decoder = new TextDecoder();

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const chunk = decoder.decode(value, { stream: true });
        const lines = chunk.split('\n');

        for (const line of lines) {
          if (line.startsWith('event: ')) {
            const event = line.slice(7).trim();
            const nextLine = lines[lines.indexOf(line) + 1];

            if (nextLine && nextLine.startsWith('data: ')) {
              const data = JSON.parse(nextLine.slice(6));

              if (event === 'delta' && data.content) {
                onDelta(data.content);
              } else if (event === 'done') {
                onDone(data);
              }
            }
          }
        }
      }
    } catch (error) {
      onError(error as Error);
    }
  }

  // 取消生成
  async cancelRun(runId: string): Promise<void> {
    await fetch(`${this.base}/runs/${runId}/cancel`, {
      method: 'POST',
    });
  }

  // 删除会话
  async deleteSession(sessionId: string): Promise<void> {
    await fetch(`${this.base}/sessions/${sessionId}`, {
      method: 'DELETE',
    });
  }
}

// 使用示例
const api = new JeevesAPI();

// 创建会话并发送消息
async function startChat() {
  try {
    // 1. 创建会话
    const session = await api.createSession('测试会话');
    console.log('会话已创建:', session.id);

    // 2. 发送消息
    let fullResponse = '';
    await api.chat(
      session.id,
      '你好',
      (delta) => {
        fullResponse += delta;
        console.log('收到:', delta);
      },
      (data) => {
        console.log('完成:', data);
      },
      (error) => {
        console.error('错误:', error);
      }
    );

    // 3. 获取历史
    const messages = await api.getMessages(session.id);
    console.log('消息历史:', messages);
  } catch (error) {
    console.error('操作失败:', error);
  }
}
```

---

### React Hooks 示例

```typescript
import { useState, useCallback } from 'react';

// 自定义 Hook：管理会话列表
function useSessions() {
  const [sessions, setSessions] = useState<SessionDetail[]>([]);
  const [loading, setLoading] = useState(false);
  const api = new JeevesAPI();

  const loadSessions = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.getSessions();
      setSessions(data);
    } catch (error) {
      console.error('加载会话失败:', error);
    } finally {
      setLoading(false);
    }
  }, []);

  const createSession = useCallback(async (title: string) => {
    const session = await api.createSession(title);
    setSessions((prev) => [session, ...prev]);
    return session;
  }, []);

  const deleteSession = useCallback(async (sessionId: string) => {
    await api.deleteSession(sessionId);
    setSessions((prev) => prev.filter((s) => s.id !== sessionId));
  }, []);

  return { sessions, loading, loadSessions, createSession, deleteSession };
}

// 自定义 Hook：流式对话
function useChat(sessionId: string) {
  const [messages, setMessages] = useState<MessageOut[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [currentResponse, setCurrentResponse] = useState('');
  const api = new JeevesAPI();

  const loadMessages = useCallback(async () => {
    const data = await api.getMessages(sessionId);
    setMessages(data);
  }, [sessionId]);

  const sendMessage = useCallback(
    async (content: string) => {
      setStreaming(true);
      setCurrentResponse('');

      // 添加用户消息
      const userMsg: MessageOut = {
        id: `temp_${Date.now()}`,
        seq: messages.length + 1,
        role: 'user',
        content,
        created_at: Date.now() / 1000,
      };
      setMessages((prev) => [...prev, userMsg]);

      await api.chat(
        sessionId,
        content,
        (delta) => {
          setCurrentResponse((prev) => prev + delta);
        },
        (data) => {
          setStreaming(false);
          loadMessages(); // 重新加载完整消息
        },
        (error) => {
          setStreaming(false);
          console.error('发送失败:', error);
        }
      );
    },
    [sessionId, messages, loadMessages]
  );

  return { messages, currentResponse, streaming, loadMessages, sendMessage };
}

// 组件示例
function ChatComponent({ sessionId }: { sessionId: string }) {
  const { messages, currentResponse, streaming, sendMessage } = useChat(sessionId);
  const [input, setInput] = useState('');

  const handleSend = () => {
    if (input.trim()) {
      sendMessage(input);
      setInput('');
    }
  };

  return (
    <div>
      <div className="messages">
        {messages.map((msg) => (
          <div key={msg.id} className={`message ${msg.role}`}>
            <strong>{msg.role}:</strong> {msg.content}
          </div>
        ))}
        {streaming && currentResponse && (
          <div className="message assistant streaming">
            <strong>assistant:</strong> {currentResponse}
          </div>
        )}
      </div>

      <div className="input-area">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyPress={(e) => e.key === 'Enter' && handleSend()}
          disabled={streaming}
        />
        <button onClick={handleSend} disabled={streaming}>
          {streaming ? '发送中...' : '发送'}
        </button>
      </div>
    </div>
  );
}
```

---

## 常见场景

### 场景 1: 带工具调用的对话

当 AI 需要调用工具时（如读取文件、执行代码），会在消息中包含 `tool_calls` 字段：

```json
{
  "role": "assistant",
  "content": "",
  "tool_calls": [
    {
      "id": "call_123",
      "type": "function",
      "function": {
        "name": "read_file",
        "arguments": "{\"path\": \"/workspace/data.txt\"}"
      }
    }
  ]
}
```

工具执行结果会以 `role: "tool"` 的消息返回：

```json
{
  "role": "tool",
  "tool_call_id": "call_123",
  "tool_name": "read_file",
  "content": "文件内容..."
}
```

---

### 场景 2: 审批模式

设置 `approval_mode: "manual"` 后，工具调用需要审批：

```typescript
// 1. 检查是否有待审批的 run
const response = await fetch(`/api/sessions/${sessionId}/active-run`);
const { run_id, pending_approval } = await response.json();

if (pending_approval) {
  // 2. 显示审批界面，用户批准后：
  await fetch(`/api/runs/${run_id}/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ approved: true }),
  });
}
```

---

### 场景 3: 取消生成

```typescript
// 1. 获取当前 run_id
const response = await fetch(`/api/sessions/${sessionId}/active-run`);
const { run_id } = await response.json();

// 2. 取消
if (run_id) {
  await fetch(`/api/runs/${run_id}/cancel`, {
    method: 'POST',
  });
}
```

---

## 调试技巧

### 1. 使用浏览器开发者工具

- **Network 面板**: 查看所有 API 请求/响应
- **Console 面板**: 捕获 JavaScript 错误
- **Application > Local Storage**: 查看客户端存储

### 2. 后端日志

```bash
# 启用 DEBUG 日志
JEEVES_APP__LOG_LEVEL=DEBUG python backend/app/main.py
```

### 3. API 测试工具

- **Postman**: 可视化测试
- **curl**: 命令行测试
- **HTTPie**: 更友好的命令行工具

```bash
# HTTPie 示例
http POST localhost:9000/api/sessions title="测试会话"
```

---

## 下一步

- 📖 [完整 API 文档](./README.md)
- 📝 [数据模型详解](./API_OVERVIEW.md#数据模型)
- 🔧 [配置管理指南](./routes_config.md)
- 💾 [记忆系统使用](./memory_router.md)

---

**提示**: 所有示例代码可在 `docs/api/` 目录找到。
