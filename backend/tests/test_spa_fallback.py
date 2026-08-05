"""
SPA 路由回退。

## 为什么单独测

这是个**只在生产模式出现**的 bug：开发时前端走 vite dev server，
它自带 SPA 回退，所以本地怎么点都正常。

而构建后由后端伺服静态文件时，在 /chat 或 /settings 页面按 F5 就白屏 404
—— 应用只有从 "/" 进入才能用。

发现过程：加定时任务页后测 `GET /cron` 返回 404，
以为是自己新加的路由没挂对，结果发现 /chat 和 /settings 也一样。
"""

from typing import Any

import pytest
import pytest_asyncio
from app.core.config import settings


@pytest_asyncio.fixture
async def client() -> Any:
    from app.main import create_app
    from httpx import ASGITransport, AsyncClient

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


needs_dist = pytest.mark.skipif(
    not settings.frontend_dist.is_dir(),
    reason="前端未构建（cd frontend && npm run build）",
)


@needs_dist
class TestSpaFallback:
    async def test_root_serves_index(self, client: Any) -> None:
        r = await client.get("/")
        assert r.status_code == 200
        assert "<div id=\"root\">" in r.text

    @pytest.mark.parametrize(
        "path", ["/chat", "/settings", "/cron", "/chat/ses_abc123"]
    )
    async def test_client_routes_return_index(self, client: Any, path: str) -> None:
        """
        前端路由在服务端没有对应文件，必须回退到 index.html。

        【StaticFiles(html=True) 不够】—— 它只在【目录】上补 index.html，
        所以 "/" 能出页面，但 "/chat" 会 404（那既不是文件也不是目录）。

        实测后果：生产模式下在这些页面按 F5 刷新就白屏。
        """
        r = await client.get(path)
        assert r.status_code == 200, f"{path} 应回退到 index.html"
        assert "<div id=\"root\">" in r.text

    # 不包含 /api/meta —— 它依赖 app.state.registry，那是 lifespan 建的，
    # 而这个 fixture 不跑 lifespan（跑的话会起后台任务，
    # pytest 的 loop 关闭后一堆 "Event loop is closed" 噪声）。
    # 这里要验的是"路由没被静态文件吃掉"，不需要那个接口。
    @pytest.mark.parametrize(
        "path", ["/api/health", "/api/sessions", "/api/cron/tasks"]
    )
    async def test_api_not_swallowed(self, client: Any, path: str) -> None:
        """
        回退不能把 API 也吃掉。

        静态文件挂在 "/" 上，如果挂载顺序错了或回退太激进，
        /api/* 会返回 index.html —— 而前端拿到一段 HTML 去 JSON.parse，
        报的是 "Unexpected token '<'"，完全不指向"路由被静态文件吃了"。
        """
        r = await client.get(path)
        assert r.status_code == 200
        assert "<div id=\"root\">" not in r.text, f"{path} 被静态文件吃掉了"

    async def test_real_asset_still_served(self, client: Any) -> None:
        """
        真实存在的静态资源要走文件，不能被回退成 index.html。

        回退成 HTML 的话浏览器加载 JS 时报语法错误 ——
        而错误信息指向 JS 文件的第一行，看起来像构建产物坏了。
        """
        import re

        root = (await client.get("/")).text
        m = re.search(r'src="(/assets/[^"]+\.js)"', root)
        assert m, "index.html 里找不到 JS 引用"

        r = await client.get(m.group(1))
        assert r.status_code == 200
        assert "javascript" in r.headers.get("content-type", "")

    async def test_missing_asset_falls_back_harmlessly(self, client: Any) -> None:
        """
        不存在的资源拿到 index.html 也无害 —— 前端会显示"页面不存在"。

        不做"路径像不像前端路由"的判断：/api/* 已被前面的路由吃掉，
        能走到这里的要么是前端路由，要么是真的不存在的东西。
        """
        r = await client.get("/definitely-not-a-real-path-xyz")
        assert r.status_code == 200

    async def test_catches_starlette_exception_not_fastapi(self) -> None:
        """
        必须 catch starlette 的 HTTPException。

        ## 踩了两次

        第一次：写成 `if resp.status_code == 404` —— 而 StaticFiles 是
        `raise HTTPException(404)` 不是返回 404 响应，那个分支永远不执行。

        第二次：改成 catch 之后仍然 404，因为 import 的是
        `fastapi.HTTPException` —— 它是 `starlette.exceptions.HTTPException`
        的【子类】，except 子类抓不到父类抛出的实例。

        两次的表现都和"完全没写这段代码"一样。
        """
        import inspect

        from app import main
        from fastapi import HTTPException as FastAPIHTTP
        from starlette.exceptions import HTTPException as StarletteHTTP

        # 确认这个继承关系没变（将来 fastapi 改了的话这个测试会提醒）
        assert issubclass(FastAPIHTTP, StarletteHTTP)
        assert not issubclass(StarletteHTTP, FastAPIHTTP)

        src = inspect.getsource(main.create_app)
        assert "StarletteHTTPException" in src, "必须用 starlette 的那个"
