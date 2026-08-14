"""Web 工具 —— 参考 claude-code 的 WebFetchTool / WebSearchTool。

提供两个只读联网能力，且 **不引入新依赖**（复用已有的 httpx + 标准库）：

- ``web_fetch``：抓取指定 URL，把 HTML 转成纯文本返回给模型分析。
- ``web_search``：联网搜索（无 API key），默认先试 DuckDuckGo Lite，
  网络不可达时自动降级 Bing（国内网络 DDG 被墙，降级后粘住 Bing）。

设计取舍
========
claude-code 的 WebFetch 内部会用一个“小模型”按 prompt 提取信息。OpenX 为
避免额外 API 开销与复杂度，直接返回清洗后的正文，让主模型自行按 ``prompt``
分析——这同样能完成任务，且零额外 token 成本。

WebSearch 在 OpenAI 兼容后端下没有内置搜索服务，故采用 DuckDuckGo Lite
HTML 解析：零配置、无 key，缺点是结果质量依赖第三方页面结构，失败时优雅
降级为错误提示。
"""

from __future__ import annotations

# ── 独立调试支持：允许直接运行本文件（python openx/.../xxx.py）──────
if __name__ == "__main__" and not __package__:
    import sys as _sys
    from pathlib import Path as _Path
    _file = _Path(__file__).resolve()
    _root = _file.parent
    while _root != _root.parent and not (_root / "pyproject.toml").exists():
        _root = _root.parent
    _sys.path.insert(0, str(_root))
    __package__ = ".".join(_file.relative_to(_root).parts[:-1])

import base64
import time
from html.parser import HTMLParser
from io import StringIO
from typing import Any, Optional
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

import httpx

from .base import Tool, ToolResult, truncate_output
from ..permissions import Permission


# ── HTTP 抓取公共逻辑 ────────────────────────────────────────────

# 伪装 UA：部分站点会拒绝默认 httpx UA
_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 搜索后端的超时：连接 5s 封顶——被墙网络的 SYN 丢包会在连接阶段卡满
# 整个超时，短 connect 让 auto 模式的降级等待可控（读仍给足 15s）。
_SEARCH_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


def _fetch(url: str, timeout: float = 20.0) -> tuple[str, str]:
    """抓取 URL，返回 (最终URL, 文本/HTML)。

    HTTP 自动升级为 HTTPS；跟随重定向。返回原始响应体（通常是 HTML）。
    """
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]

    headers = {"User-Agent": _DEFAULT_UA, "Accept": "text/html,*/*"}
    with httpx.Client(follow_redirects=True, timeout=timeout) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        return str(resp.url), resp.text


# ── HTML → 纯文本转换 ────────────────────────────────────────────

class _TextExtractor(HTMLParser):
    """极简 HTML→文本转换器。

    - 去掉 ``<script>``/``<style>``/``<head>`` 等非正文内容；
    - 块级标签后补换行，保留可读性；
    - 收集 ``<a href>`` 链接，附在文末供模型参考。
    """

    # 这些标签内的内容直接丢弃
    _DROP = {"script", "style", "head", "noscript", "svg", "template"}

    def __init__(self) -> None:
        super().__init__()
        self._buf: list[str] = []
        self._links: list[str] = []
        self._drop_depth = 0          # 当前处于多少层“丢弃”标签内
        self._skip_data = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag in self._DROP:
            self._drop_depth += 1
            return
        # 块级元素：前置换行，避免与上一段粘连
        if tag in ("p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr"):
            self._buf.append("\n")
        # 收集链接
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self._links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._DROP:
            self._drop_depth = max(0, self._drop_depth - 1)
            return
        if tag in ("p", "div", "li", "h1", "h2", "h3", "h4", "tr"):
            self._buf.append("\n")

    def handle_data(self, data: str) -> None:
        if self._drop_depth > 0:
            return  # 处于丢弃标签内，忽略文本
        self._buf.append(data)

    def get_text(self) -> str:
        text = "".join(self._buf)
        # 折叠多余空白
        lines = [ln.strip() for ln in text.splitlines()]
        text = "\n".join(ln for ln in lines if ln)
        return text

    def get_links(self) -> list[str]:
        return self._links


def html_to_text(html: str, base_url: str = "") -> str:
    """把 HTML 转成可读纯文本，并在末尾附上去重后的链接。"""
    parser = _TextExtractor()
    parser.feed(html)
    text = parser.get_text()

    links = parser.get_links()
    if links:
        seen: set[str] = set()
        uniq: list[str] = []
        for href in links:
            # 把相对链接补成绝对链接
            abs_href = urljoin(base_url, href) if base_url else href
            if abs_href.startswith(("http://", "https://")) and abs_href not in seen:
                seen.add(abs_href)
                uniq.append(abs_href)
        if uniq:
            text += "\n\nLinks:\n" + "\n".join(f"- {u}" for u in uniq[:30])

    return text


# ── web_fetch ────────────────────────────────────────────────────

class WebFetchTool(Tool):
    """抓取网页并转为文本，供模型按 prompt 分析。"""

    name = "web_fetch"
    description = (
        "Fetch a URL and return its content as cleaned text. "
        "HTTP is upgraded to HTTPS. Use for retrieving docs, articles, or any "
        "web page. Read-only. Results are cached for 15 minutes."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Fully-formed URL to fetch."},
            "prompt": {
                "type": "string",
                "description": "What you want to extract or learn from the page.",
            },
        },
        "required": ["url"],
    }

    # 15 分钟缓存：claude-code 同款策略，重复抓同一 URL 时秒回
    _CACHE_TTL = 15 * 60
    _CACHE_MAX = 50

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, str]] = {}

    @property
    def permission(self) -> Permission:
        return Permission.allow()

    async def execute(self, url: str, prompt: str = "") -> ToolResult:
        # 命中缓存则直接返回
        now = time.monotonic()
        cached = self._cache.get(url)
        if cached and (now - cached[0]) < self._CACHE_TTL:
            text = cached[1]
        else:
            try:
                final_url, html = _fetch(url)
            except Exception as e:
                return ToolResult(error=f"Failed to fetch {url}: {e}")

            text = html_to_text(html, base_url=url)
            # 截断超长正文，避免撑爆上下文
            text, _, _ = truncate_output(text, max_lines=400, max_chars=20_000)

            # 写入缓存，并淘汰最旧条目
            self._cache[url] = (now, text)
            if len(self._cache) > self._CACHE_MAX:
                oldest = min(self._cache, key=lambda k: self._cache[k][0])
                self._cache.pop(oldest, None)

        # 把用户的 prompt 一并回显，方便模型对照分析
        header = f"URL: {url}\n"
        if prompt:
            header += f"Extraction goal: {prompt}\n"
        header += "---\n"
        return ToolResult(output=header + text)


# ── web_search (DuckDuckGo Lite, 无 key) ─────────────────────────

class _DuckDuckGoResultParser(HTMLParser):
    """解析 DuckDuckGo Lite 结果页 HTML。

    Lite 页面结构简单：结果在一个个 ``<a class="result-link">`` 里，
    摘要在随后的 ``<td class="result-snippet">``。这里用宽松的状态机提取：
    遇到结果链接记下标题+URL，后续文本节点视为摘要直到下一个结果。
    """

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._cur: Optional[dict[str, str]] = None
        self._capture_link = False
        self._capture_snippet = False
        self._cur_href = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attrs_d = dict(attrs)
        if tag == "a" and attrs_d.get("class") == "result-link":
            # 新结果开始
            if self._cur:
                self.results.append(self._cur)
            self._cur = {"title": "", "url": attrs_d.get("href", ""), "snippet": ""}
            self._capture_link = True
        elif tag == "td" and attrs_d.get("class") == "result-snippet":
            self._capture_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._capture_link:
            self._capture_link = False
        elif tag == "td" and self._capture_snippet:
            self._capture_snippet = False

    def handle_data(self, data: str) -> None:
        if self._capture_link and self._cur is not None:
            self._cur["title"] += data
        elif self._capture_snippet and self._cur is not None:
            self._cur["snippet"] += data

    def close(self) -> None:  # type: ignore[override]
        super().close()
        if self._cur:
            self.results.append(self._cur)
            self._cur = None


def _ddg_search(query: str, max_results: int = 8) -> list[dict[str, str]]:
    """通过 DuckDuckGo Lite 执行搜索，返回结果列表。"""
    url = "https://lite.duckduckgo.com/lite/"
    headers = {"User-Agent": _DEFAULT_UA}
    data = {"q": query, "kl": "us-en"}

    with httpx.Client(follow_redirects=True, timeout=_SEARCH_TIMEOUT) as client:
        resp = client.post(url, headers=headers, data=data)
        resp.raise_for_status()
        html = resp.text

    parser = _DuckDuckGoResultParser()
    parser.feed(html)
    parser.close()

    # 清洗：补全相对链接、去空标题
    cleaned: list[dict[str, str]] = []
    for r in parser.results:
        href = r.get("url", "")
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = "https://lite.duckduckgo.com" + href
        title = r.get("title", "").strip()
        if not title:
            continue
        cleaned.append({
            "title": title,
            "url": href,
            "snippet": r.get("snippet", "").strip(),
        })
        if len(cleaned) >= max_results:
            break
    return cleaned


# ── web_search 降级后端：Bing（无 key，国内网络可达）─────────────

class _BingResultParser(HTMLParser):
    """解析 Bing 搜索结果页（www.bing.com / cn.bing.com）。

    结果结构稳定：``<li class="b_algo">`` 块内，``<h2><a href>`` 为
    标题+链接，``<div class="b_caption"><p>`` 为摘要。用 li/div 深度
    计数界定块边界：块内的其他链接（sitelinks）不会误生成结果，块外
    的页面级 ``<h2>``（如"相关搜索"）也不会混入。
    """

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._cur: Optional[dict[str, str]] = None
        self._li_depth = 0
        self._algo_at = 0          # 当前 b_algo 块起始的 li 深度（0 = 不在块内）
        self._in_h2 = False
        self._capture_title = False
        self._div_depth = 0        # 仅在 b_algo 块内计数
        self._caption_at = 0       # b_caption 起始的 div 深度（0 = 不在摘要区）
        self._capture_snippet = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attrs_d = dict(attrs)
        classes = (attrs_d.get("class") or "").split()
        if tag == "li":
            self._li_depth += 1
            if self._algo_at == 0 and "b_algo" in classes:
                if self._cur:
                    self.results.append(self._cur)
                self._cur = {"title": "", "url": "", "snippet": ""}
                self._algo_at = self._li_depth
            return
        if self._algo_at == 0:
            return  # 块外内容一律忽略
        if tag == "h2":
            self._in_h2 = True
        elif tag == "a" and self._in_h2 and not self._capture_title:
            self._capture_title = True  # 只认 h2 内的首个链接
            if self._cur is not None:
                self._cur["url"] = attrs_d.get("href", "")
        elif tag == "div":
            self._div_depth += 1
            if self._caption_at == 0 and "b_caption" in classes:
                self._caption_at = self._div_depth
        elif tag == "p" and self._caption_at:
            self._capture_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "li" and self._li_depth > 0:
            if self._li_depth == self._algo_at:
                # b_algo 块结束：复位块内状态
                self._algo_at = 0
                self._caption_at = 0
                self._div_depth = 0
                self._in_h2 = False
                self._capture_title = False
                self._capture_snippet = False
            self._li_depth -= 1
        elif tag == "div" and self._algo_at:
            if self._div_depth == self._caption_at:
                self._caption_at = 0
                self._capture_snippet = False
            self._div_depth = max(0, self._div_depth - 1)
        elif tag == "a" and self._capture_title:
            self._capture_title = False
        elif tag == "h2":
            self._in_h2 = False
        elif tag == "p" and self._capture_snippet:
            self._capture_snippet = False

    def handle_data(self, data: str) -> None:
        if self._capture_title and self._cur is not None:
            self._cur["title"] += data
        elif self._capture_snippet and self._cur is not None:
            piece = data.strip()
            if piece:
                self._cur["snippet"] += (" " if self._cur["snippet"] else "") + piece

    def close(self) -> None:  # type: ignore[override]
        super().close()
        if self._cur:
            self.results.append(self._cur)
            self._cur = None


def _unwrap_bing_url(href: str) -> str:
    """解包 Bing 跳转链接 ``bing.com/ck/a?...&u=a1<base64url>`` 为原始 URL。

    cn.bing.com 通常直给原始链接，但部分区域/版本的 Bing 会把结果 URL
    包成跳转。解不开时原样返回（跳转链接本身仍可用）。
    """
    try:
        parsed = urlparse(href)
        if "bing.com" not in parsed.netloc or not parsed.path.startswith("/ck/a"):
            return href
        u = parse_qs(parsed.query).get("u", [""])[0]
        if not u.startswith("a1"):
            return href
        body = u[2:]
        decoded = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        text = decoded.decode("utf-8", "ignore")
        return text if text.startswith(("http://", "https://")) else href
    except Exception:
        return href


def _bing_search(query: str, max_results: int = 8) -> list[dict[str, str]]:
    """通过 Bing 搜索页执行搜索，返回结果列表（DuckDuckGo 的降级后端）。"""
    url = "https://www.bing.com/search"
    headers = {
        "User-Agent": _DEFAULT_UA,
        "Accept": "text/html,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    params = {"q": query, "count": str(max_results), "setlang": "en"}

    # follow_redirects：国内网络会被 302 到 cn.bing.com，须跟随
    with httpx.Client(follow_redirects=True, timeout=_SEARCH_TIMEOUT) as client:
        resp = client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        html = resp.text

    parser = _BingResultParser()
    parser.feed(html)
    parser.close()

    cleaned: list[dict[str, str]] = []
    for r in parser.results:
        href = _unwrap_bing_url(r.get("url", ""))
        if href.startswith("//"):
            href = "https:" + href
        title = r.get("title", "").strip()
        if not title or not href.startswith(("http://", "https://")):
            continue
        cleaned.append({
            "title": title,
            "url": href,
            "snippet": r.get("snippet", "").strip(),
        })
        if len(cleaned) >= max_results:
            break
    return cleaned


# 后端名 → 搜索函数。execute 内按名字做全局查找调用（不经此表预绑定），
# 保证测试对 web_tools._ddg_search / _bing_search 的 monkeypatch 生效。
_SEARCH_PROVIDERS = ("ddg", "bing")


class WebSearchTool(Tool):
    """联网搜索（DDG 优先，网络不可达自动降级 Bing，均无 key）。"""

    name = "web_search"
    description = (
        "Search the web (DuckDuckGo with automatic Bing fallback; no API key "
        "needed). Returns titles, URLs, and snippets. Use for up-to-date "
        "information beyond your knowledge cutoff. After answering, include a "
        "'Sources:' section with markdown links."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query."},
            "max_results": {
                "type": "integer",
                "description": "Max number of results to return (default 8).",
            },
        },
        "required": ["query"],
    }

    def __init__(self, provider: str = "auto") -> None:
        # provider: "auto"（DDG→Bing 降级链）| "ddg" | "bing"（固定后端）。
        # 非法值归一为 auto——配置错误不应让工具不可用。
        self.provider = provider if provider in ("auto", "ddg", "bing") else "auto"
        # auto 模式下粘住最近一次成功的后端：国内网络首次搜索付一次
        # DDG 连接超时（≤5s）后，同一会话内的后续搜索直达 Bing。
        self._sticky: Optional[str] = None

    @property
    def permission(self) -> Permission:
        return Permission.allow()

    def _provider_order(self) -> list[str]:
        """本次搜索的后端尝试顺序。"""
        if self.provider == "ddg":
            return ["ddg"]
        if self.provider == "bing":
            return ["bing"]
        # auto：粘性后端优先，其余按默认序补全
        base = list(_SEARCH_PROVIDERS)
        if self._sticky in base:
            base.remove(self._sticky)
            base.insert(0, self._sticky)
        return base

    async def execute(self, query: str, max_results: int = 8) -> ToolResult:
        n = max(1, min(max_results, 20))
        results: list[dict[str, str]] = []
        errors: list[str] = []

        for name in self._provider_order():
            fn = _ddg_search if name == "ddg" else _bing_search
            try:
                found = fn(query, max_results=n)
            except Exception as e:
                errors.append(f"{name}: {e}")
                continue
            if found:
                self._sticky = name
                results = found
                break
            errors.append(f"{name}: no results")

        if not results:
            # 全部后端"无结果"（无网络错误）→ 与旧行为一致的软提示；
            # 有任一网络错误 → 报错，让模型知道是连通性问题而非查无此词
            if errors and all(e.endswith(": no results") for e in errors):
                return ToolResult(output=f"No results found for: {query}")
            return ToolResult(error="Web search failed: " + "; ".join(errors))

        lines = [f"Search results for: {query}\n"]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r['title']}")
            lines.append(f"   {r['url']}")
            if r["snippet"]:
                lines.append(f"   {r['snippet']}")
            lines.append("")

        # 提醒模型在回答末尾附 Sources（claude-code 同款要求）
        lines.append("Remember to add a 'Sources:' section with these URLs.")
        output = "\n".join(lines)
        output, _, _ = truncate_output(output, max_chars=12_000)
        return ToolResult(output=output)


if __name__ == "__main__":
    # 独立调试：绝不联网 —— 仅实例化工具、打印 name/description/schema，
    # 并对本地 HTML 字符串做离线解析验证
    for t in (WebFetchTool(), WebSearchTool()):
        print(f"{t.name}: {t.description}")
        print(f"  params: {t.to_openai_schema()['function']['parameters']['properties'].keys()}")
    text = html_to_text("<html><body><p>Hello <b>OpenX</b></p>"
                        "<a href='https://example.com'>x</a></body></html>")
    assert "Hello" in text and "OpenX" in text, text
    print("html_to_text offline parse:", repr(text.splitlines()[0]))
    print("openx/tools/web_tools.py OK ✓")
