"""Web 工具 —— 参考 claude-code 的 WebFetchTool / WebSearchTool。

提供两个只读联网能力，且 **不引入新依赖**（复用已有的 httpx + 标准库）：

- ``web_fetch``：抓取指定 URL，**按 prompt 返回相关内容**——两级提取：
  零成本启发式（查询词项选块，替代"头部前 N 行"截断）+ opt-in 小模型
  蒸馏（``OPENX_WEB_EXTRACT``，mini 角色，claude-code WebFetch 同款）。
- ``web_search``：联网搜索。API 后端（Tavily / Brave，key 走
  ``OPENX_TAVILY_API_KEY`` / ``OPENX_BRAVE_API_KEY``）优先；无 key 回落
  爬虫链 DuckDuckGo Lite → Bing（国内网络 DDG 被墙，降级后粘住 Bing）。
  支持多路查询并行扇出（``queries`` 变体，合并去重）；所有搜索走
  ``asyncio.to_thread``，不阻塞事件循环。

设计取舍
========
页面爬取零配置可用但天花板是爬虫级（反爬/结构变化/片段质量）；API 后端
质量高但需要 key——两者都留着，auto 链按 key 有无自动取舍，固定后端
``OPENX_WEB_SEARCH=tavily|brave|ddg|bing``。小模型蒸馏默认关（可控成本，
opt-in）：失败/超时/无关一律静默回落启发式，web_fetch 绝不因它不可用。
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
import asyncio
import time
from datetime import date
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


# ── 相关性提取（零成本启发式 + opt-in 小模型蒸馏）────────────────

#: 提取的停用词：出现在 prompt 里但对定位正文毫无区分度的高频词。
_STOPWORDS = frozenset(
    "the a an and or of to in on for with is are was were be been what how "
    "why when where which who that this these those it its from as at by "
    "请 什么 怎么 如何 为什么 哪些 哪个 是 的 了 在 和 有 关于".split()
)

#: CJK 串边缘功能字（"的定价" → "定价"）：只剥边缘，不拆词内部。
_CJK_FUNC = "的了着呢吗吧啊呀在有和与及或把被对从向到于之其这个那"


def _query_terms(prompt: str) -> list[str]:
    """prompt → 查询词项：拉丁词（≥2 字符）+ CJK 连续串（剥边缘功能字），
    去停用词去重。"""
    import re as _re

    terms: list[str] = []
    for latin in _re.findall(r"[a-zA-Z0-9_]{2,}", prompt):
        low = latin.lower()
        if low not in _STOPWORDS:
            terms.append(low)
    for cjk in _re.findall(r"[一-鿿]{2,}", prompt):
        cjk = cjk.strip(_CJK_FUNC)
        if len(cjk) >= 2:
            terms.append(cjk)
    seen: set[str] = set()
    uniq = []
    for t in terms:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def _extract_relevant(text: str, prompt: str, budget_chars: int = 12_000) -> str:
    """按 prompt 词项相关性挑选文本块，替代"头部前 N 行"截断。

    ``html_to_text`` 的输出一行 ≈ 一个块级元素（段落/标题/表格行），是天然
    的选择单元：给每行按查询词项命中数打分，选高分行并向邻行各扩 1 行
    （防断句），按原序输出、字符预算封顶。无 prompt / 全部零分 → 返回空串
    （调用方回落头部截断——漫无目标的提取比不提取更糟）。
    """
    terms = _query_terms(prompt)
    if not terms or not text:
        return ""
    lines = text.splitlines()
    scores = []
    for ln in lines:
        low = ln.lower()
        scores.append(sum(1 for t in terms if t in low))
    if not any(scores):
        return ""

    selected = [False] * len(lines)
    order = sorted(range(len(lines)), key=lambda i: scores[i], reverse=True)
    used = 0
    for i in order:
        if scores[i] == 0:
            break  # 分数降序，到零即无可用行
        for j in range(max(0, i - 1), min(len(lines), i + 2)):
            if not selected[j]:
                used += len(lines[j]) + 1
                selected[j] = True
        if used >= budget_chars:
            break

    kept = [ln for ln, sel in zip(lines, selected) if sel]
    out = "\n".join(kept)
    return out[:budget_chars]


async def _llm_extract(settings: dict, text: str, prompt: str) -> Optional[str]:
    """opt-in 小模型蒸馏：页面预选文本 + 提取目标 → 蒸馏答案（尽力而为）。

    直连 provider（``OpenAICompatProvider``），不走 agent/内核——提取是
    一次独立补全，失败/超时/无内容一律返回 ``None``（调用方回落启发式），
    绝不让 web_fetch 因它不可用。无重试（重试归内核，而这里不值得）。
    """
    from ..llm.openai_compat import OpenAICompatProvider

    system = (
        "You are a web-page distiller. Answer ONLY with the information from "
        "the page that is relevant to the extraction goal. Quote exact facts "
        "(numbers, names, URLs, dates). Be concise (under 300 words). "
        "If the page contains nothing relevant, output exactly: NO_RELEVANT_CONTENT"
    )
    user = f"Extraction goal: {prompt}\n\nPage content:\n{text}"
    try:
        provider = OpenAICompatProvider(settings)
        msg = await asyncio.wait_for(
            provider.chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                stream=False,
            ),
            timeout=60.0,
        )
    except Exception:  # noqa: BLE001  蒸馏是增强，任何失败都回落启发式
        return None
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):  # 多段内容：取文本段
        content = "".join(
            p.get("text", "") for p in content if isinstance(p, dict)
        )
    if not isinstance(content, str) or not content.strip():
        return None
    content = content.strip()
    return None if content == "NO_RELEVANT_CONTENT" else content


# ── web_fetch ────────────────────────────────────────────────────

class WebFetchTool(Tool):
    """抓取网页并按 prompt 返回相关内容。

    两级提取：默认**零成本启发式**（``_extract_relevant``，按查询词项选块，
    替代头部截断——现代网页前 400 行多是导航/页眉，答案在中段）；配置
    ``web_fetch_extract``（env ``OPENX_WEB_EXTRACT``）开启后，先启发式预选
    再交 mini 角色小模型**按目标蒸馏**（claude-code WebFetch 同款，省主
    上下文，付一次小模型调用）。蒸馏失败/超时/无关 → 静默回落启发式。
    """

    name = "web_fetch"
    description = (
        "Fetch a URL and return its content relevant to the extraction goal. "
        "HTTP is upgraded to HTTPS. Use for retrieving docs, articles, or any "
        "web page — always pass a prompt describing what you want to learn. "
        "Read-only. Pages are cached for 15 minutes."
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

    # 15 分钟缓存：claude-code 同款策略，重复抓同一 URL 时秒回。
    # 缓存的是**清洗后原文**（预选前）——提取按调用做，同一 URL 换 prompt
    # 不至于拿到上一发的蒸馏残渣。
    _CACHE_TTL = 15 * 60
    _CACHE_MAX = 50
    _RAW_CACHE_CHARS = 80_000   # 原文缓存上限（内存友好）
    _PREFILTER_CHARS = 24_000   # 蒸馏前的启发式预选预算（省小模型 token）
    _HEURISTIC_CHARS = 12_000   # 纯启发式提取预算

    def __init__(self, extract_settings: Optional[dict] = None) -> None:
        self._extract_settings = extract_settings
        self._cache: dict[str, tuple[float, str]] = {}

    @property
    def permission(self) -> Permission:
        return Permission.allow()

    async def execute(self, url: str, prompt: str = "") -> ToolResult:
        # 命中缓存则直接返回（原文，非上一发的提取残渣）
        now = time.monotonic()
        cached = self._cache.get(url)
        if cached and (now - cached[0]) < self._CACHE_TTL:
            raw = cached[1]
        else:
            try:
                final_url, html = await asyncio.to_thread(_fetch, url)
            except Exception as e:
                return ToolResult(error=f"Failed to fetch {url}: {e}")

            raw = html_to_text(html, base_url=url)
            # 原文缓存封顶：超限先头部截断再入缓存（提取在下面按调用做）
            if len(raw) > self._RAW_CACHE_CHARS:
                raw = raw[: self._RAW_CACHE_CHARS]

            # 写入缓存，并淘汰最旧条目
            self._cache[url] = (now, raw)
            if len(self._cache) > self._CACHE_MAX:
                oldest = min(self._cache, key=lambda k: self._cache[k][0])
                self._cache.pop(oldest, None)

        # 提取：小模型蒸馏（opt-in）→ 启发式选块 → 头部截断（逐级回落）
        text = ""
        note = ""
        if prompt:
            if self._extract_settings:
                pre = _extract_relevant(raw, prompt, self._PREFILTER_CHARS)
                distilled = await _llm_extract(
                    self._extract_settings, pre or raw[: self._PREFILTER_CHARS], prompt
                )
                if distilled:
                    text = distilled
                    note = "[distilled by the extractor model from the page]"
            if not text:
                text = _extract_relevant(raw, prompt, self._HEURISTIC_CHARS)
                if text:
                    note = "[relevant passages extracted from the page]"
        if not text:  # 无 prompt / 提取零命中：老行为，头部截断
            text, _, _ = truncate_output(raw, max_lines=400, max_chars=20_000)

        # 把用户的 prompt 一并回显，方便模型对照分析
        header = f"URL: {url}\n"
        if prompt:
            header += f"Extraction goal: {prompt}\n"
        if note:
            header += f"{note}\n"
        header += "---\n"
        output = header + text
        output, _, _ = truncate_output(output, max_lines=800, max_chars=24_000)
        return ToolResult(output=output)


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


def _is_cjk_query(query: str) -> bool:
    """查询含 CJK 统一表意文字 → 按中文区请求搜索后端（locale 只影响排序）。"""
    return any("一" <= ch <= "鿿" for ch in query)  # U+4E00..U+9FFF


def _ddg_search(query: str, max_results: int = 8) -> list[dict[str, str]]:
    """通过 DuckDuckGo Lite 执行搜索，返回结果列表。"""
    url = "https://lite.duckduckgo.com/lite/"
    headers = {"User-Agent": _DEFAULT_UA}
    data = {"q": query, "kl": "cn-zh" if _is_cjk_query(query) else "us-en"}

    with httpx.Client(follow_redirects=True, timeout=_SEARCH_TIMEOUT) as client:
        resp = client.post(url, headers=headers, data=data)
        resp.raise_for_status()
        html = resp.text

    # 限流/验证页：DDG 返回 202 + anomaly modal，解析只会得到 0 条——
    # 必须识别为后端失败（走降级链），而非"查无此词"
    if getattr(resp, "status_code", 200) == 202 or "anomaly-modal" in html:
        raise RuntimeError("rate-limited by DuckDuckGo (anomaly challenge)")

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
    cjk = _is_cjk_query(query)
    headers = {
        "User-Agent": _DEFAULT_UA,
        "Accept": "text/html,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9" if cjk else "en-US,en;q=0.9",
    }
    params = {"q": query, "count": str(max_results), "setlang": "zh-hans" if cjk else "en"}
    if cjk:
        params["cc"] = "cn"

    # follow_redirects：国内网络会被 302 到 cn.bing.com，须跟随
    with httpx.Client(follow_redirects=True, timeout=_SEARCH_TIMEOUT) as client:
        resp = client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        html = resp.text

    parser = _BingResultParser()
    parser.feed(html)
    parser.close()

    # 结构变化/ consent / 验证页：页面有实质内容但既无结果块（b_algo）
    # 也无"无结果"标记（b_no）→ 识别为后端失败而非"查无此词"
    if not parser.results and "b_algo" not in html and "b_no" not in html:
        raise RuntimeError("unexpected Bing page structure (consent/captcha/markup change?)")

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


# ── web_search API 后端（key 制，质量上限高于页面爬取）──────────


def _api_key(env_var: str) -> str:
    """API key 只从环境变量取（``OPENX_TAVILY_API_KEY`` /
    ``OPENX_BRAVE_API_KEY``）——调用期读取，测试可 monkeypatch 环境注入。"""
    import os

    return os.environ.get(env_var, "").strip()


def _tavily_search(query: str, max_results: int = 8) -> list[dict[str, str]]:
    """Tavily Search API（LLM 导向的搜索：结果带相关性 score 与内容摘要）。"""
    key = _api_key("OPENX_TAVILY_API_KEY")
    if not key:
        raise RuntimeError("OPENX_TAVILY_API_KEY not set")
    with httpx.Client(timeout=_SEARCH_TIMEOUT) as client:
        resp = client.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {key}"},
            json={"query": query, "max_results": max_results},
        )
        resp.raise_for_status()
        data = resp.json()
    return _clean_api_results(data.get("results") or [], "content")


def _brave_search(query: str, max_results: int = 8) -> list[dict[str, str]]:
    """Brave Search API（独立索引，无 profile 气泡，结构稳定）。"""
    key = _api_key("OPENX_BRAVE_API_KEY")
    if not key:
        raise RuntimeError("OPENX_BRAVE_API_KEY not set")
    with httpx.Client(timeout=_SEARCH_TIMEOUT) as client:
        resp = client.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={
                "X-Subscription-Token": key,
                "Accept": "application/json",
            },
            params={"q": query, "count": str(max_results)},
        )
        resp.raise_for_status()
        data = resp.json()
    return _clean_api_results((data.get("web") or {}).get("results") or [], "description")


def _clean_api_results(
    rows: list, snippet_key: str
) -> list[dict[str, str]]:
    """API 结果 → 统一形状（title/url/snippet）；缺标题或链接的丢弃。"""
    cleaned: list[dict[str, str]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        title = str(r.get("title", "")).strip()
        url = str(r.get("url", "")).strip()
        if not title or not url.startswith(("http://", "https://")):
            continue
        cleaned.append({
            "title": title,
            "url": url,
            "snippet": str(r.get(snippet_key, "")).strip(),
        })
    return cleaned


# 后端名 → 搜索函数。execute 内按名字做全局查找调用（不经此表预绑定），
# 保证测试对 web_tools._ddg_search 等的 monkeypatch 生效。
_SEARCH_PROVIDERS = ("tavily", "brave", "ddg", "bing")
#: 爬虫后端（无 key 兜底链）；API 后端缺席 key 时 auto 链自动跳过。
_SCRAPER_PROVIDERS = ("ddg", "bing")


def _diversify_domains(results: list[dict[str, str]]) -> list[dict[str, str]]:
    """同域名折叠：每个域名排位最高的一条进主列表，其余按原序附后。

    不丢结果，只重排——top 段保证来源多样性，同一站点刷屏时不再挤掉
    其他来源。
    """
    seen: set[str] = set()
    primary: list[dict[str, str]] = []
    extra: list[dict[str, str]] = []
    for r in results:
        host = urlparse(r.get("url", "")).netloc.lower().removeprefix("www.")
        if host and host in seen:
            extra.append(r)
        else:
            seen.add(host)
            primary.append(r)
    return primary + extra


class WebSearchTool(Tool):
    """联网搜索：API 后端（Tavily/Brave，有 key）优先，爬虫链（DDG→Bing）
    无 key 兜底；多路查询并行扇出，合并去重。"""

    name = "web_search"
    description = (
        "Search the web. Returns titles, URLs, and snippets. Use for "
        "up-to-date information beyond your knowledge cutoff. For broad or "
        "multi-facet questions pass 2-4 differently-phrased `queries` "
        "variants — they run in parallel and results are merged and "
        "deduplicated. Query tips: use a few specific keywords, not a full "
        "sentence; search in English for international topics, in Chinese "
        "for China-specific ones; include the current year for time-sensitive "
        "topics; use site:example.com to scope the search. If results look "
        "irrelevant, rephrase and retry; use web_fetch to read a promising "
        "page in full. After answering, include a 'Sources:' section with "
        "markdown links."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The primary search query."},
            "queries": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Additional query variants (max 3) to run in parallel "
                    "with `query` — different phrasings, keywords, or "
                    "languages. Merged into one deduplicated result list."
                ),
            },
            "max_results": {
                "type": "integer",
                "description": "Max number of results to return (default 8).",
            },
        },
        "required": ["query"],
    }

    def __init__(self, provider: str = "auto") -> None:
        # provider: "auto"（key 制 API 后端优先 → DDG → Bing 降级链，粘住
        # 成功者）| 固定单一后端。非法值归一为 auto——配置错误不应让工具
        # 不可用。
        self.provider = (
            provider if provider in ("auto",) + _SEARCH_PROVIDERS else "auto"
        )
        # auto 模式下粘住最近一次成功的后端：爬虫链首次搜索付一次连接
        # 超时（≤5s）后，同一会话内的后续搜索直达可用后端。
        self._sticky: Optional[str] = None

    @property
    def permission(self) -> Permission:
        return Permission.allow()

    def _provider_order(self) -> list[str]:
        """本次搜索的后端尝试顺序。"""
        if self.provider != "auto":
            return [self.provider]
        # auto：API 后端有 key 才入链；粘性后端优先，其余按默认序补全
        base = [
            name for name in _SEARCH_PROVIDERS
            if name in _SCRAPER_PROVIDERS or _api_key(_API_KEY_VARS[name])
        ]
        if self._sticky in base:
            base.remove(self._sticky)
            base.insert(0, self._sticky)
        return base

    async def _search_chain(
        self, query: str, n: int
    ) -> tuple[list[dict[str, str]], list[str], bool]:
        """单条 query 走后端降级链 → (结果, 逐端错误, 是否正常应答过)。

        搜索是同步 HTTP（httpx.Client），一律 ``asyncio.to_thread``——绝不
        阻塞事件循环（TUI 冻结 / 执行器并行 gather 全被一格查搜索卡住）。
        """
        results: list[dict[str, str]] = []
        errors: list[str] = []
        searched_ok = False
        for name in self._provider_order():
            fn = {
                "tavily": _tavily_search,
                "brave": _brave_search,
                "ddg": _ddg_search,
                "bing": _bing_search,
            }[name]
            try:
                found = await asyncio.to_thread(fn, query, n)
            except Exception as e:
                errors.append(f"{name}: {e}")
                continue
            searched_ok = True
            if found:
                self._sticky = name
                results = found
                break
            errors.append(f"{name}: no results")
        return results, errors, searched_ok

    async def execute(
        self,
        query: str,
        max_results: int = 8,
        queries: Optional[list[str]] = None,
    ) -> ToolResult:
        n = max(1, min(max_results, 20))
        # 扇出：主 query + 变体，去重限 4 条
        all_queries = [query]
        for q in queries or []:
            q = str(q).strip()
            if q and q not in all_queries:
                all_queries.append(q)
        all_queries = all_queries[:4]

        outcomes = await asyncio.gather(
            *(self._search_chain(q, n) for q in all_queries)
        )

        merged: list[dict[str, str]] = []
        errors: list[str] = []
        searched_ok = False
        seen_urls: set[str] = set()
        # 轮转合并：每条 query 的第 1 名排前，其次各条第 2 名……既保多样性
        # 又不偏向首个变体；URL 归一去重防同文异参刷屏。
        for rank in range(n):
            for results, errs, ok in outcomes:
                errors.extend(errs)
                searched_ok = searched_ok or ok
                if rank < len(results):
                    r = results[rank]
                    key = _norm_url(r["url"])
                    if key not in seen_urls:
                        seen_urls.add(key)
                        merged.append(r)

        if not merged:
            if searched_ok:
                # 至少一端正常应答但无结果 → 真·查无此词；其余端的故障
                # 作为附注如实告知（限流 ≠ 无结果，模型可换措辞重试）
                note = f"No results found for: {query}"
                failed = [e for e in errors if not e.endswith(": no results")]
                if failed:
                    note += "\n(note: " + "; ".join(sorted(set(failed))) + ")"
                note += "\nIf this looks wrong, try rephrasing with fewer, more specific keywords."
                return ToolResult(output=note)
            # 全部后端故障（网络/限流/结构变化/key 缺失）→ 报错，让模型
            # 知道是连通性/可用性问题而非查无此词
            return ToolResult(error="Web search failed: " + "; ".join(sorted(set(errors))))

        header = f"Search results for: {query}  (current date: {date.today().isoformat()})"
        if len(all_queries) > 1:
            header += f"\n[{len(all_queries)} queries run in parallel, merged]"
        lines = [header + "\n"]
        for i, r in enumerate(_diversify_domains(merged)[:n], 1):
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


def _norm_url(url: str) -> str:
    """URL 归一（去重键）：scheme/host 小写、去 fragment、去尾部斜杠。"""
    try:
        p = urlparse(url)
        host = (p.netloc or "").lower()
        path = p.path.rstrip("/") or ("/" if p.query else "")
        return f"{host}{path}{'?' + p.query if p.query else ''}"
    except Exception:
        return url


#: API 后端名 → key 环境变量（auto 链按此判定谁有资格入链）。
_API_KEY_VARS = {"tavily": "OPENX_TAVILY_API_KEY", "brave": "OPENX_BRAVE_API_KEY"}


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
