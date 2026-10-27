"""web 工具搜索升级：相关性提取 / API 后端 / 多路扇出合并 / 小模型蒸馏。

覆盖：
- ``_query_terms``：拉丁词 + CJK 串 + 停用词；
- ``_extract_relevant``：按词项选块（替代头部截断）、邻行扩展边界、
  无 prompt / 零命中回落空串；
- API 后端：Tavily / Brave 响应解析、缺 key 报错（走降级链不崩）；
- ``WebSearchTool``：auto 链按 key 有无取舍、多路扇出轮转合并 + URL
  去重、全端故障 vs 真无结果严格区分；
- ``_llm_extract``：蒸馏成功 / 失败 / NO_RELEVANT_CONTENT 三路；
- ``WebFetchTool``：原文缓存 + 按调用提取（启发式注记 / 蒸馏注记）。

运行：``python -m pytest tests/tools/test_web_tools_upgrade.py -q``
"""

from __future__ import annotations

import asyncio

import pytest

from openx.tools import web_tools as wt
from openx.tools.web_tools import (
    WebFetchTool,
    WebSearchTool,
    _clean_api_results,
    _extract_relevant,
    _query_terms,
)


# ── 相关性提取 ────────────────────────────────────────────────────


def test_query_terms_latin_cjk_stopwords():
    terms = _query_terms("What is the Tavily Search API 的定价")
    assert "tavily" in terms and "search" in terms and "api" in terms
    assert "定价" in terms
    assert "what" not in terms and "the" not in terms and "is" not in terms


_PAGE = "\n".join(
    ["导航 Menu Home About"] * 30
    + [
        "Tavily was founded in 2022 by a team of developers.",
        "The Tavily Search API returns scored, LLM-ready results.",
        "Pricing starts at 30 USD per month for the API.",
    ]
    + ["footer links"] * 30
)


def test_extract_relevant_picks_scoring_blocks():
    out = _extract_relevant(_PAGE, "Tavily Search API pricing")
    assert "Pricing starts" in out and "founded" in out
    assert out.count("Menu Home About") <= 2  # 邻行扩展只带进边界处少量
    assert len(out) < len(_PAGE) / 2          # 头部 60 行导航没有被搬进来


def test_extract_relevant_fallbacks():
    assert _extract_relevant(_PAGE, "") == ""                    # 无 prompt
    assert _extract_relevant(_PAGE, "zzz qqq nonexistent") == ""  # 零命中
    assert _extract_relevant("", "tavily") == ""                 # 空文本


# ── API 后端（httpx 打桩）─────────────────────────────────────────


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeClient:
    data: dict = {}
    last_request: dict = {}

    def __init__(self, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, **kw):
        _FakeClient.last_request = {"method": "post", "url": url, **kw}
        return _FakeResponse(_FakeClient.data)

    def get(self, url, **kw):
        _FakeClient.last_request = {"method": "get", "url": url, **kw}
        return _FakeResponse(_FakeClient.data)


_TAVILY = {
    "results": [
        {"title": "Tavily", "url": "https://tavily.com", "content": "LLM search"},
        {"title": "", "url": "https://skipped.example"},  # 缺标题丢弃
        {"title": "Bad", "url": "ftp://x"},               # 非 http 丢弃
    ]
}
_BRAVE = {"web": {"results": [
    {"title": "Brave", "url": "https://brave.com", "description": "independent"},
]}}


def test_tavily_backend(monkeypatch):
    monkeypatch.setenv("OPENX_TAVILY_API_KEY", "tvly-key")
    monkeypatch.setattr(wt.httpx, "Client", _FakeClient)
    _FakeClient.data = _TAVILY
    rows = wt._tavily_search("q", 8)
    assert rows == [{"title": "Tavily", "url": "https://tavily.com",
                     "snippet": "LLM search"}]
    assert _FakeClient.last_request["url"] == "https://api.tavily.com/search"
    assert _FakeClient.last_request["headers"]["Authorization"] == "Bearer tvly-key"


def test_brave_backend(monkeypatch):
    monkeypatch.setenv("OPENX_BRAVE_API_KEY", "brave-key")
    monkeypatch.setattr(wt.httpx, "Client", _FakeClient)
    _FakeClient.data = _BRAVE
    rows = wt._brave_search("q", 8)
    assert rows[0]["url"] == "https://brave.com"
    assert _FakeClient.last_request["headers"]["X-Subscription-Token"] == "brave-key"


def test_clean_api_results_shapes():
    rows = _clean_api_results(
        [{"title": "ok", "url": "https://a", "content": "c"},
         "junk", {"url": "https://no-title"}],
        "content",
    )
    assert len(rows) == 1 and rows[0]["snippet"] == "c"


def test_auto_chain_skips_keyless_backends(monkeypatch):
    monkeypatch.delenv("OPENX_TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("OPENX_BRAVE_API_KEY", raising=False)
    assert WebSearchTool("auto")._provider_order() == ["ddg", "bing"]
    monkeypatch.setenv("OPENX_TAVILY_API_KEY", "k")
    assert WebSearchTool("auto")._provider_order()[0] == "tavily"
    # 固定后端不经 key 判定；非法值归一为 auto
    assert WebSearchTool("brave")._provider_order() == ["brave"]
    monkeypatch.delenv("OPENX_TAVILY_API_KEY")
    assert WebSearchTool("nope")._provider_order() == ["ddg", "bing"]


# ── 多路扇出合并（假爬虫后端）─────────────────────────────────────


def _patch_scrapers(monkeypatch, ddg, bing):
    monkeypatch.setattr(wt, "_ddg_search", ddg)
    monkeypatch.setattr(wt, "_bing_search", bing)


def test_fanout_merge_and_dedupe(monkeypatch):
    monkeypatch.delenv("OPENX_TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("OPENX_BRAVE_API_KEY", raising=False)

    def ddg(q, n):
        return [{"title": f"A-{q}-{i}", "url": f"https://a.example/{q}/{i}",
                 "snippet": "x"} for i in range(n)]

    _patch_scrapers(monkeypatch, ddg, lambda q, n: [])
    out = asyncio.run(
        WebSearchTool("auto").execute("q1", 4, queries=["q2"])
    ).output
    assert "[2 queries run in parallel, merged]" in out
    # 轮转合并：两路的首位相邻出现；同 URL 去重
    assert out.index("A-q1-0") < out.index("A-q2-0") < out.index("A-q1-1")
    assert out.count("a.example") == 4  # 封顶 max_results


def test_fanout_caps_at_four_queries(monkeypatch):
    captured: list[list[str]] = []

    def ddg(q, n):
        captured.append([q])
        return []

    _patch_scrapers(monkeypatch, ddg, lambda q, n: [])
    asyncio.run(WebSearchTool("auto").execute(
        "q1", 2, queries=["q2", "q3", "q4", "q5"]))
    assert len(captured) == 4  # 主 + 3 变体，第 5 条被裁


def test_all_backends_fail_vs_no_results(monkeypatch):
    monkeypatch.delenv("OPENX_TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("OPENX_BRAVE_API_KEY", raising=False)

    def boom(q, n):
        raise RuntimeError("rate-limited")

    _patch_scrapers(monkeypatch, boom, boom)
    r = asyncio.run(WebSearchTool("auto").execute("q", 4))
    assert r.error and "ddg: rate-limited" in r.error and "bing: rate-limited" in r.error

    # 一端正常应答 0 条 = 真无结果；另一端故障降为附注（限流 ≠ 无结果）
    _patch_scrapers(monkeypatch, lambda q, n: [], boom)
    out = asyncio.run(WebSearchTool("auto").execute("q", 4)).output
    assert "No results found" in out and "(note: bing: rate-limited)" in out


# ── 小模型蒸馏（provider 打桩）────────────────────────────────────


class _FakeProvider:
    reply: object = ""
    boom = False

    def __init__(self, settings):
        pass

    async def chat(self, messages, tools=None, stream=False):
        if _FakeProvider.boom:
            raise RuntimeError("provider down")
        return {"role": "assistant", "content": _FakeProvider.reply}


def test_llm_extract_paths(monkeypatch):
    import openx.llm.openai_compat as oc

    monkeypatch.setattr(oc, "OpenAICompatProvider", _FakeProvider)

    _FakeProvider.reply = "Pricing is 30 USD/month."
    assert asyncio.run(wt._llm_extract({}, "page", "pricing")) == \
        "Pricing is 30 USD/month."

    _FakeProvider.reply = "NO_RELEVANT_CONTENT"
    assert asyncio.run(wt._llm_extract({}, "page", "pricing")) is None

    _FakeProvider.reply = ""
    assert asyncio.run(wt._llm_extract({}, "page", "pricing")) is None

    _FakeProvider.boom = True
    assert asyncio.run(wt._llm_extract({}, "page", "pricing")) is None


# ── WebFetchTool：缓存原文、按调用提取 ────────────────────────────


def test_fetch_tool_heuristic_and_cache(monkeypatch):
    monkeypatch.delenv("OPENX_TAVILY_API_KEY", raising=False)
    calls: list[str] = []
    page = "<p>nav junk</p>" * 100 + "<p>Tavily pricing is 30 USD per month.</p>"

    def fake_fetch(url, timeout=20.0):
        calls.append(url)
        return url, f"<html><body>{page}</body></html>"

    monkeypatch.setattr(wt, "_fetch", fake_fetch)
    tool = WebFetchTool()  # 无蒸馏设置：纯启发式

    out = asyncio.run(tool.execute("https://x.example", "Tavily pricing")).output
    assert "relevant passages extracted" in out
    assert "30 USD per month" in out
    assert out.count("nav junk") <= 2
    # 第二次同一 URL：缓存原文，不再抓取；换 prompt 提取照做
    out2 = asyncio.run(tool.execute("https://x.example", "something else")).output
    assert len(calls) == 1


def test_fetch_tool_llm_distill(monkeypatch):
    import openx.llm.openai_compat as oc

    page = "<p>long page</p>" * 50 + "<p>The answer is 42.</p>"
    monkeypatch.setattr(
        wt, "_fetch", lambda url, timeout=20.0: (url, f"<html><body>{page}</body></html>")
    )
    monkeypatch.setattr(oc, "OpenAICompatProvider", _FakeProvider)
    _FakeProvider.reply = "The answer is 42."
    _FakeProvider.boom = False

    tool = WebFetchTool(extract_settings={"model": "mini"})
    out = asyncio.run(tool.execute("https://y.example", "what is the answer")).output
    assert "[distilled by the extractor model from the page]" in out
    assert "The answer is 42." in out

    # 蒸馏失败 → 静默回落启发式（注记换回 relevant passages）
    _FakeProvider.boom = True
    out2 = asyncio.run(tool.execute("https://y.example", "what is the answer")).output
    assert "relevant passages extracted" in out2 and "The answer is 42." in out2


def test_fetch_tool_no_prompt_head_truncation(monkeypatch):
    page = "".join(f"<p>line {i}</p>" for i in range(1000))
    monkeypatch.setattr(
        wt, "_fetch", lambda url, timeout=20.0: (url, f"<html><body>{page}</body></html>")
    )
    tool = WebFetchTool()
    out = asyncio.run(tool.execute("https://z.example")).output
    assert "relevant passages" not in out and "distilled" not in out
    assert "line 0" in out and "line 999" not in out  # 头部截断


# ── grep output_mode ─────────────────────────────────────────────


def _grep_tool(tmp_path):
    from openx.tools.search_tools import GrepTool

    return GrepTool(str(tmp_path))


def test_grep_output_modes(tmp_path):
    (tmp_path / "a.py").write_text("alpha = 1\nalpha_two = 2\nother = 3\n")
    (tmp_path / "b.py").write_text("beta = 1\nbeta_alpha = 2\n")
    tool = _grep_tool(tmp_path)

    # files_with_matches：保序去重的文件列表
    out = asyncio.run(tool.execute("alpha", output_mode="files_with_matches")).output
    assert out.splitlines()[0] == "a.py" and out.splitlines()[1] == "b.py"
    assert "[2 file(s) with matches]" in out

    # count：逐文件计数
    out = asyncio.run(tool.execute("alpha", output_mode="count")).output
    assert "a.py:2" in out and "b.py:1" in out and "[3 match(es) total]" in out

    # content 缺省不变；非法 mode 报错
    out = asyncio.run(tool.execute("alpha")).output
    assert "a.py:1:" in out
    r = asyncio.run(tool.execute("alpha", output_mode="bogus"))
    assert r.error and "Invalid output_mode" in r.error
