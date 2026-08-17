#!/usr/bin/env python3
"""
自动生成 API 文档。

从后端路由文件中提取所有端点信息，生成前端开发用的完整 API 文档。
"""

import ast
import re
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
API_DIR = BACKEND_ROOT / "app" / "api"
MODULES_DIR = BACKEND_ROOT / "app" / "modules"
OUTPUT_DIR = PROJECT_ROOT / "docs" / "api"

# 路由文件列表
ROUTE_FILES = [
    ("会话与对话", API_DIR / "routes_chat.py", "/api/sessions"),
    ("配置管理", API_DIR / "routes_config.py", "/api/config"),
    ("定时任务", API_DIR / "routes_cron.py", "/api/cron"),
    ("文件访问", API_DIR / "routes_files.py", "/api/files"),
    ("模型管理", API_DIR / "routes_models.py", "/api/models"),
    ("智能体", MODULES_DIR / "agent" / "agent_router.py", "/api/agents"),
    ("记忆系统", MODULES_DIR / "memory" / "memory_router.py", "/api/memory"),
]


class EndpointInfo:
    """端点信息。"""

    def __init__(
        self,
        method: str,
        path: str,
        function_name: str,
        docstring: str = "",
        params: list[dict] = None,
        response_model: str = "",
    ):
        self.method = method.upper()
        self.path = path
        self.function_name = function_name
        self.docstring = docstring
        self.params = params or []
        self.response_model = response_model

    def __repr__(self):
        return f"<Endpoint {self.method} {self.path}>"


def extract_endpoints(file_path: Path) -> list[EndpointInfo]:
    """从路由文件中提取所有端点。"""
    if not file_path.exists():
        print(f"⚠️  文件不存在: {file_path}")
        return []

    content = file_path.read_text(encoding="utf-8")
    
    endpoints = []
    lines = content.split("\n")
    
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        
        # 匹配 @router.METHOD 装饰器
        match = re.match(r'@router\.(get|post|put|patch|delete)\s*\(', line)
        if match:
            method = match.group(1)
            
            # 提取完整的装饰器（可能跨多行）
            decorator_lines = [line]
            paren_count = line.count('(') - line.count(')')
            j = i + 1
            while paren_count > 0 and j < len(lines):
                decorator_lines.append(lines[j].strip())
                paren_count += lines[j].count('(') - lines[j].count(')')
                j += 1
            
            decorator_text = " ".join(decorator_lines)
            
            # 提取路径
            path_match = re.search(r'["\']([^"\']+)["\']', decorator_text)
            path = path_match.group(1) if path_match else ""
            
            # 提取 summary
            summary_match = re.search(r'summary\s*=\s*["\']([^"\']+)["\']', decorator_text)
            summary = summary_match.group(1) if summary_match else ""
            
            # 提取 response_model
            response_match = re.search(r'response_model\s*=\s*(\w+)', decorator_text)
            response_model = response_match.group(1) if response_match else ""
            
            # 查找函数定义（下一个非空非注释行）
            k = j
            function_name = ""
            docstring = ""
            while k < len(lines):
                func_line = lines[k].strip()
                if func_line and not func_line.startswith("#"):
                    func_match = re.match(r'(?:async\s+)?def\s+(\w+)', func_line)
                    if func_match:
                        function_name = func_match.group(1)
                        
                        # 尝试提取 docstring（下一行开始的三引号）
                        m = k + 1
                        while m < len(lines) and m < k + 10:
                            doc_line = lines[m].strip()
                            if doc_line.startswith('"""') or doc_line.startswith("'''"):
                                # 找到 docstring 开始
                                quote = '"""' if doc_line.startswith('"""') else "'''"
                                doc_parts = []
                                
                                # 单行 docstring
                                if doc_line.count(quote) >= 2:
                                    docstring = doc_line.strip(quote).strip()
                                else:
                                    # 多行 docstring
                                    doc_parts.append(doc_line.replace(quote, "").strip())
                                    m += 1
                                    while m < len(lines):
                                        doc_part = lines[m]
                                        if quote in doc_part:
                                            doc_parts.append(doc_part.replace(quote, "").strip())
                                            break
                                        doc_parts.append(doc_part.strip())
                                        m += 1
                                    docstring = "\n".join(doc_parts)
                                break
                            m += 1
                        break
                k += 1
            
            if path and function_name:
                endpoints.append(
                    EndpointInfo(
                        method=method,
                        path=path,
                        function_name=function_name,
                        docstring=summary or docstring,
                        params=[],
                        response_model=response_model,
                    )
                )
            
            i = j
        else:
            i += 1

    return endpoints


def generate_markdown(module_name: str, prefix: str, endpoints: list[EndpointInfo]) -> str:
    """生成 Markdown 文档。"""
    md = [f"# {module_name} API\n"]
    md.append(f"**路由前缀**: `{prefix}`\n")
    md.append(f"**端点数量**: {len(endpoints)}\n")
    md.append("---\n")

    # 按方法和路径排序
    endpoints.sort(key=lambda e: (e.method, e.path))

    # 按方法分组
    methods = {}
    for ep in endpoints:
        if ep.method not in methods:
            methods[ep.method] = []
        methods[ep.method].append(ep)

    for method in ["GET", "POST", "PUT", "PATCH", "DELETE"]:
        if method not in methods:
            continue

        md.append(f"## {method} 请求\n")

        for ep in methods[method]:
            full_path = f"{prefix}{ep.path}"
            md.append(f"### `{ep.method} {full_path}`\n")

            # 描述
            if ep.docstring:
                # 提取第一行作为简短描述
                lines = ep.docstring.strip().split("\n")
                description = lines[0].strip()
                md.append(f"**描述**: {description}\n")

                # 如果有详细说明，添加到折叠区
                if len(lines) > 1:
                    md.append("\n<details>\n<summary>详细说明</summary>\n\n")
                    md.append("```\n")
                    md.append("\n".join(lines[1:]).strip())
                    md.append("\n```\n")
                    md.append("</details>\n")
            else:
                md.append(f"**描述**: _{ep.function_name}_\n")

            # 路径参数
            path_params = re.findall(r"\{(\w+)\}", ep.path)
            if path_params:
                md.append("\n**路径参数**:\n")
                for param in path_params:
                    md.append(f"- `{param}`: (从路径提取)\n")

            # 查询参数/请求体
            if ep.params:
                if ep.method in ["GET", "DELETE"]:
                    md.append("\n**查询参数**:\n")
                else:
                    md.append("\n**请求参数**:\n")
                for param in ep.params:
                    md.append(f"- `{param['name']}`: {param['type']}\n")

            # 响应模型
            if ep.response_model:
                md.append(f"\n**响应类型**: `{ep.response_model}`\n")

            md.append("\n---\n")

    return "".join(md)


def main():
    """主函数。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print("生成 API 文档")
    print("=" * 80)

    all_endpoints = 0

    for module_name, file_path, prefix in ROUTE_FILES:
        print(f"\n📄 处理: {module_name}")
        print(f"   文件: {file_path.relative_to(PROJECT_ROOT)}")

        endpoints = extract_endpoints(file_path)
        all_endpoints += len(endpoints)

        print(f"   发现: {len(endpoints)} 个端点")

        if endpoints:
            # 生成文档
            markdown = generate_markdown(module_name, prefix, endpoints)

            # 写入文件
            output_file = OUTPUT_DIR / f"{file_path.stem}.md"
            output_file.write_text(markdown, encoding="utf-8")
            print(f"   输出: {output_file.relative_to(PROJECT_ROOT)}")

    # 生成索引
    index_md = ["# Jeeves API 文档索引\n"]
    index_md.append("所有后端 API 接口的完整文档。\n")
    index_md.append("---\n")
    index_md.append("\n## 模块列表\n")

    for module_name, file_path, prefix in ROUTE_FILES:
        doc_file = f"{file_path.stem}.md"
        index_md.append(f"- [{module_name}](./{doc_file}) - `{prefix}`\n")

    index_md.append(f"\n---\n\n**总计**: {all_endpoints} 个端点\n")
    index_md.append(f"\n**生成时间**: `{__file__}`\n")

    index_file = OUTPUT_DIR / "README.md"
    index_file.write_text("".join(index_md), encoding="utf-8")

    print("\n" + "=" * 80)
    print(f"✅ 完成！生成了 {len(ROUTE_FILES)} 个模块的文档")
    print(f"✅ 总计 {all_endpoints} 个端点")
    print(f"📁 输出目录: {OUTPUT_DIR.relative_to(PROJECT_ROOT)}")
    print("=" * 80)


if __name__ == "__main__":
    main()
