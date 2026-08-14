"""
记忆存储端口。

## 为什么有 port 而只有一个实现

与 infra/llm/port.py 同一个理由：【测试】。内存实现（测试用）直接满足这个
Protocol，service 层的测试就不需要真的读写文件。

第二个理由是边界。service.py 是唯一对外入口，它只依赖这个 Protocol，
所以"记忆存哪里"这件事被限制在 file_store.py 一个文件里。之后想换成
别的形态（比如加一层缓存）不影响 service。
"""

from typing import Any, Protocol

from app.modules.memory.models import MemoryItem, MemoryScope
from app.modules.memory.schema import MemoryTypeSchema


class MemoryStore(Protocol):
    """记忆的物理读写。不做合并、不做校验 —— 那是 service 的事。"""

    async def read(self, scope: MemoryScope, schema: MemoryTypeSchema, rel_path: str) -> MemoryItem | None:
        """读一条。不存在返回 None。解析失败也返回 None 并记 warning。"""
        ...

    async def read_uri(self, uri: str) -> MemoryItem | None:
        """按 uri（相对 data/memory/ 的 POSIX 路径）读一条。"""
        ...

    async def write(
        self,
        scope: MemoryScope,
        schema: MemoryTypeSchema,
        rel_path: str,
        item: MemoryItem,
        *,
        extraction_id: str = "",
        trace_id: str = "",
    ) -> str:
        """写一条，返回 uri。父目录不存在时自动建。"""
        ...

    async def delete_uri(self, uri: str) -> bool:
        """删一条。文件不存在返回 False。"""
        ...

    async def list_items(
        self, scope: MemoryScope, schema: MemoryTypeSchema
    ) -> list[MemoryItem]:
        """
        列举某类记忆。

        单文件类型（profile.md）只返回它自己，不返回同目录下别的类型 ——
        实现要按 memory_type 过滤。
        """
        ...

    async def resolve_path(
        self, scope: MemoryScope, schema: MemoryTypeSchema, fields: dict[str, Any], *, extract_context: Any = None
    ) -> str:
        """渲染出这条记忆该落在哪（相对 scope 根的路径）。"""
        ...

    async def write_overview(
        self, scope: MemoryScope, schema: MemoryTypeSchema, rel_dir: str, content: str
    ) -> str:
        """写目录索引 .overview.md。content 为空时删掉已有的索引文件。"""
        ...

    async def drop_agent(self, agent_id: str) -> int:
        """删掉一个智能体的全部记忆。返回删除的文件数。"""
        ...

    async def drop_session(self, agent_id: str, session_id: str) -> int:
        """删掉一个会话的全部记忆。返回删除的文件数。"""
        ...

    async def iter_all(self) -> list[tuple[str, MemoryItem]]:
        """扫全部记忆文件。索引重建用。返回 [(uri, item), ...]。"""
        ...
