"""新增功能测试：对话记忆、edit_file 唯一匹配、todo、web、ask_user、grep 兼容。

这些用例覆盖本次补全的核心能力。运行：``python -m pytest tests/test_new_features.py -q``
"""

from __future__ import annotations

import httpx
import pytest

from openx.config import OpenXConfig
from openx.llm import StreamDone
from openx.tools.file_tools import EditFileTool
from openx.tools.todo_tools import TodoWriteTool
from openx.tools import web_tools
from openx.tools.ask_user_tool import AskUserTool


# ── Fake LLM，用于 agent 历史记忆测试 ────────────────────────────

class FakeLLM:
    """可脚本化的假 LLM：按顺序返回预设响应。"""

    def __init__(self, responses):
        # responses: list of (content, tool_calls)
        self.responses = list(responses)
        self.call_count = 0

    async def stream_chat(self, messages, tools=None):
        content, tool_calls = self.responses[self.call_count]
        self.call_count += 1
        if content:
            for tok in content.split():
                yield tok + " "
        resp = {"role": "assistant", "content": content or None}
        if tool_calls:
            resp["tool_calls"] = tool_calls
        yield StreamDone(response=resp, token_count=5, input_tokens=10)

    async def chat(self, messages, tools=None, stream=True):
        content, tool_calls = self.responses[self.call_count]
        self.call_count += 1
        resp = {"role": "assistant", "content": content}
        if tool_calls:
            resp["tool_calls"] = tool_calls
        return resp


def _make_agent(tmp_path, monkeypatch, responses):
    """构造一个挂载 FakeLLM 的 OpenXAgent（绕过真实 API）。"""
    from openx.agent import OpenXAgent
    config = OpenXConfig()
    config.workspace = str(tmp_path)
    config.model = "test-model"
    agent = OpenXAgent(config)
    agent.llm = FakeLLM(responses)
    return agent


# ── 对话记忆 ─────────────────────────────────────────────────────

class TestConversationMemory:
    """跨轮对话历史保持。"""

    @pytest.mark.asyncio
    async def test_history_persists_across_turns(self, tmp_path, monkeypatch):
        agent = _make_agent(tmp_path, monkeypatch, [("Hello there", None), ("Hi again", None)])

        # 第一轮
        out1 = []
        async for chunk in agent.stream_run("first message"):
            out1.append(chunk)
        assert "Hello" in "".join(out1)

        # 第一轮后历史应含 user + assistant
        roles1 = [m["role"] for m in agent.history.messages]
        assert roles1 == ["user", "assistant"]

        # 第二轮
        out2 = []
        async for chunk in agent.stream_run("second message"):
            out2.append(chunk)
        assert "Hi again" in "".join(out2)

        # 两轮历史都保留，顺序正确
        roles2 = [m["role"] for m in agent.history.messages]
        assert roles2 == ["user", "assistant", "user", "assistant"]

    @pytest.mark.asyncio
    async def test_clear_history(self, tmp_path, monkeypatch):
        agent = _make_agent(tmp_path, monkeypatch, [("resp", None)])
        async for _ in agent.stream_run("hi"):
            pass
        assert len(agent.history.messages) > 0
        agent.clear_history()
        assert agent.history.messages == []

    @pytest.mark.asyncio
    async def test_token_accumulation(self, tmp_path, monkeypatch):
        agent = _make_agent(tmp_path, monkeypatch, [("resp one", None)])
        async for _ in agent.stream_run("hi"):
            pass
        assert agent.total_output_tokens > 0
        assert agent.total_input_tokens > 0

    def test_fit_history_trims_to_user_boundary(self, tmp_path, monkeypatch):
        # 手动塞入超长历史，验证裁剪后首条仍是 user
        agent = _make_agent(tmp_path, monkeypatch, [])
        agent.history.messages = [
            {"role": "user", "content": "x" * 200_000},
            {"role": "assistant", "content": "y" * 200_000},
            {"role": "user", "content": "recent"},
            {"role": "assistant", "content": "recent reply"},
        ]
        agent.history.fit()
        # 裁剪后第一条必须是 user，且序列合法
        assert agent.history.messages[0]["role"] == "user"


# ── edit_file 唯一匹配 ──────────────────────────────────────────

class TestEditFileUniqueMatch:
    """edit_file 的唯一匹配与 replace_all 语义。"""

    @pytest.mark.asyncio
    async def test_non_unique_without_replace_all_errors(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("dup\nmid\ndup\n")
        tool = EditFileTool(str(tmp_path))
        result = await tool.execute(str(f), "dup", "unique")
        assert not result.success
        assert "not unique" in result.error.lower()

    @pytest.mark.asyncio
    async def test_replace_all_replaces_every(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("dup\nmid\ndup\n")
        tool = EditFileTool(str(tmp_path))
        result = await tool.execute(str(f), "dup", "unique", replace_all=True)
        assert result.success
        assert f.read_text().count("unique") == 2

    @pytest.mark.asyncio
    async def test_unique_single_match_replaces_one(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("only one target here\n")
        tool = EditFileTool(str(tmp_path))
        result = await tool.execute(str(f), "target", "replaced")
        assert result.success
        assert "replaced" in f.read_text()


# ── Todo 工具 ───────────────────────────────────────────────────

class TestTodoWriteTool:
    """todo_write 工具原地更新共享 store。"""

    @pytest.mark.asyncio
    async def test_mutates_shared_store(self):
        store: list = []
        tool = TodoWriteTool(store)
        todos = [
            {"content": "Do A", "activeForm": "Doing A", "status": "in_progress"},
            {"content": "Do B", "activeForm": "Doing B", "status": "pending"},
        ]
        result = await tool.execute(todos=todos)
        assert result.success
        assert len(store) == 2
        assert store[0]["status"] == "in_progress"
        assert "0/2 completed" in result.output  # 进度回显（无 completed）

    @pytest.mark.asyncio
    async def test_invalid_status_normalized(self):
        store: list = []
        tool = TodoWriteTool(store)
        await tool.execute(todos=[{"content": "x", "activeForm": "x", "status": "bogus"}])
        assert store[0]["status"] == "pending"


# ── Web 工具（mock 网络层）─────────────────────────────────────

class TestWebTools:
    """web_fetch / web_search，monkeypatch 真实网络调用。"""

    @pytest.mark.asyncio
    async def test_web_fetch_extracts_text_and_links(self, monkeypatch):
        def fake_fetch(url, timeout=20.0):
            return (url, "<html><body><p>Hello world</p><a href='/x'>link</a></body></html>")
        monkeypatch.setattr(web_tools, "_fetch", fake_fetch)

        tool = web_tools.WebFetchTool()
        result = await tool.execute(url="https://example.com", prompt="greeting")
        assert result.success
        assert "Hello world" in result.output
        assert "Links" in result.output  # 链接段

    @pytest.mark.asyncio
    async def test_web_fetch_cache_hit(self, monkeypatch):
        calls = {"n": 0}

        def fake_fetch(url, timeout=20.0):
            calls["n"] += 1
            return (url, "<html><body><p>cached</p></body></html>")
        monkeypatch.setattr(web_tools, "_fetch", fake_fetch)

        tool = web_tools.WebFetchTool()
        await tool.execute(url="https://example.com")
        await tool.execute(url="https://example.com")  # 应命中缓存
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_web_search_returns_results(self, monkeypatch):
        def fake_search(query, max_results=8):
            return [
                {"title": "Result One", "url": "https://one.example.com", "snippet": "snip1"},
                {"title": "Result Two", "url": "https://two.example.com", "snippet": "snip2"},
            ]
        monkeypatch.setattr(web_tools, "_ddg_search", fake_search)

        tool = web_tools.WebSearchTool()
        result = await tool.execute(query="test")
        assert result.success
        assert "Result One" in result.output
        assert "https://one.example.com" in result.output
        assert "Sources" in result.output

    @pytest.mark.asyncio
    async def test_web_search_no_results(self, monkeypatch):
        # auto 模式会降级到 Bing——两个后端都桩住，避免真实联网
        monkeypatch.setattr(web_tools, "_ddg_search", lambda q, max_results=8: [])
        monkeypatch.setattr(web_tools, "_bing_search", lambda q, max_results=8: [])
        tool = web_tools.WebSearchTool()
        result = await tool.execute(query="nothing")
        assert "No results" in result.output

    @pytest.mark.asyncio
    async def test_web_search_falls_back_to_bing_on_network_error(self, monkeypatch):
        """DDG 网络不可达（如被墙超时）→ 自动降级 Bing 成功。"""
        def ddg_down(q, max_results=8):
            raise httpx.ConnectError("timed out")

        bing_calls = []

        def bing_up(q, max_results=8):
            bing_calls.append((q, max_results))
            return [{"title": "Bing One", "url": "https://b.example.com", "snippet": "bs"}]

        monkeypatch.setattr(web_tools, "_ddg_search", ddg_down)
        monkeypatch.setattr(web_tools, "_bing_search", bing_up)

        tool = web_tools.WebSearchTool()  # provider 默认 auto
        result = await tool.execute(query="test", max_results=3)
        assert result.success
        assert "Bing One" in result.output
        assert bing_calls == [("test", 3)]
        assert tool._sticky == "bing"  # 粘住成功后端

    @pytest.mark.asyncio
    async def test_web_search_sticky_provider_skips_dead_ddg(self, monkeypatch):
        """粘住 Bing 后，后续搜索不再白等 DDG 超时。"""
        ddg_calls = []

        def ddg_down(q, max_results=8):
            ddg_calls.append(q)
            raise httpx.ConnectError("timed out")

        def bing_up(q, max_results=8):
            return [{"title": "T", "url": "https://x.com", "snippet": ""}]

        monkeypatch.setattr(web_tools, "_ddg_search", ddg_down)
        monkeypatch.setattr(web_tools, "_bing_search", bing_up)

        tool = web_tools.WebSearchTool()
        r1 = await tool.execute(query="a")
        r2 = await tool.execute(query="b")
        assert r1.success and r2.success
        assert ddg_calls == ["a"]  # 第二次直达 Bing，DDG 只被试过一次

    @pytest.mark.asyncio
    async def test_web_search_pinned_provider_no_fallback(self, monkeypatch):
        """固定 ddg 后端时，失败不降级，返回错误。"""
        bing_called = []

        def ddg_down(q, max_results=8):
            raise httpx.ConnectError("timed out")

        def bing_up(q, max_results=8):
            bing_called.append(q)
            return [{"title": "T", "url": "https://x.com", "snippet": ""}]

        monkeypatch.setattr(web_tools, "_ddg_search", ddg_down)
        monkeypatch.setattr(web_tools, "_bing_search", bing_up)

        tool = web_tools.WebSearchTool(provider="ddg")
        result = await tool.execute(query="test")
        assert not result.success
        assert "Web search failed" in result.error
        assert bing_called == []  # 固定后端绝不降级

    @pytest.mark.asyncio
    async def test_web_search_all_providers_error_reports_both(self, monkeypatch):
        """auto 模式两后端皆网络错误 → 错误信息含双方诊断。"""
        def down(q, max_results=8):
            raise httpx.ConnectError("timed out")

        monkeypatch.setattr(web_tools, "_ddg_search", down)
        monkeypatch.setattr(web_tools, "_bing_search", down)

        result = await web_tools.WebSearchTool().execute(query="test")
        assert not result.success
        assert "ddg: timed out" in result.error
        assert "bing: timed out" in result.error

    def test_web_search_invalid_provider_defaults_to_auto(self):
        assert web_tools.WebSearchTool(provider="bogus").provider == "auto"

    def test_bing_parser_extracts_results(self):
        html = """
        <html><body>
        <h2><a href="https://outside.example.com">页面级标题（不应混入）</a></h2>
        <ol id="b_results">
          <li class="b_algo">
            <h2><a href="https://one.example.com">Result <strong>One</strong></a></h2>
            <div class="b_caption"><div class="b_deep">
              <p>snippet part1 <b>bold</b> part2</p>
            </div></div>
            <div class="ftr"><a href="https://sitelink.example.com">sitelink（不应生成结果）</a></div>
          </li>
          <li class="b_algo">
            <h2><a href="//two.example.com">Result Two</a></h2>
            <div class="b_caption"><p>second snippet</p></div>
          </li>
          <li class="b_pag"><a href="/next">下一页</a></li>
        </ol>
        </body></html>
        """
        parser = web_tools._BingResultParser()
        parser.feed(html)
        parser.close()
        assert len(parser.results) == 2
        r1, r2 = parser.results
        assert r1["title"] == "Result One"          # <strong> 内文本并入标题
        assert r1["url"] == "https://one.example.com"
        assert "snippet part1" in r1["snippet"] and "part2" in r1["snippet"]
        assert "sitelink" not in r1["title"] + r1["snippet"]
        assert r2["title"] == "Result Two"
        assert r2["snippet"] == "second snippet"
        # 块外的 h2 链接与分页 li 均未混入
        assert all("页面级标题" not in r["title"] for r in parser.results)

    def test_unwrap_bing_url(self):
        import base64
        target = "https://example.com/page?q=1"
        u = "a1" + base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
        ck = f"https://www.bing.com/ck/a?!&&p=xyz&u={u}&ntest=1"
        assert web_tools._unwrap_bing_url(ck) == target
        # 非跳转链接原样返回；坏编码不抛
        plain = "https://python.org/"
        assert web_tools._unwrap_bing_url(plain) == plain
        assert web_tools._unwrap_bing_url("https://www.bing.com/ck/a?u=a1!!!") == "https://www.bing.com/ck/a?u=a1!!!"

    @pytest.mark.asyncio
    async def test_bing_search_end_to_end_with_fake_http(self, monkeypatch):
        """_bing_search：fake httpx.Client 返回真实结构的 HTML → 清洗后结果。"""
        html = """
        <li class="b_algo">
          <h2><a href="https://r.example.com/a">Alpha</a></h2>
          <div class="b_caption"><p>alpha snippet</p></div>
        </li>
        <li class="b_algo">
          <h2><a href="https://r.example.com/b">Beta</a></h2>
          <div class="b_caption"><p>beta snippet</p></div>
        </li>
        """

        class FakeResponse:
            text = html
            def raise_for_status(self):
                pass

        class FakeClient:
            def __init__(self, **kw):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
            def get(self, url, headers=None, params=None):
                assert params["q"] == "python"
                return FakeResponse()

        monkeypatch.setattr(web_tools.httpx, "Client", FakeClient)
        results = web_tools._bing_search("python", max_results=5)
        assert [r["title"] for r in results] == ["Alpha", "Beta"]
        assert results[0]["snippet"] == "alpha snippet"

    def test_html_to_text_strips_script_style(self):
        html = "<html><head><style>x{}</style></head><body><script>bad()</script><p>good</p></body></html>"
        text = web_tools.html_to_text(html)
        assert "good" in text
        assert "bad" not in text
        assert "x{}" not in text


# ── AskUser 工具 ────────────────────────────────────────────────

class TestAskUserTool:
    """ask_user 工具通过 console 获取用户选择。"""

    @pytest.mark.asyncio
    async def test_returns_single_selection(self):
        class FakeConsole:
            def ask_user_question(self, question, options, multi_select):
                assert multi_select is False
                return options[0]["label"]

        tool = AskUserTool(FakeConsole())
        result = await tool.execute(
            question="Which?",
            options=[{"label": "A"}, {"label": "B"}],
            multi_select=False,
        )
        assert result.success
        assert "A" in result.output

    @pytest.mark.asyncio
    async def test_returns_multi_selection(self):
        class FakeConsole:
            def ask_user_question(self, question, options, multi_select):
                return [options[0]["label"], options[1]["label"]]

        tool = AskUserTool(FakeConsole())
        result = await tool.execute(
            question="Which?",
            options=[{"label": "A"}, {"label": "B"}, {"label": "C"}],
            multi_select=True,
        )
        assert "A" in result.output and "B" in result.output

    @pytest.mark.asyncio
    async def test_requires_two_options(self):
        class FakeConsole:
            def ask_user_question(self, question, options, multi_select):
                return []
        tool = AskUserTool(FakeConsole())
        result = await tool.execute(question="?", options=[{"label": "only"}])
        assert not result.success


# ── grep 兼容性（os.walk 在嵌套目录下工作）──────────────────────

class TestGrepCompat:
    """验证 grep 用 os.walk 能递归且跳过忽略目录。"""

    @pytest.mark.asyncio
    async def test_grep_nested_dirs(self, tmp_path):
        from openx.tools.search_tools import GrepTool
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "a.py").write_text("needle here\n")
        (tmp_path / "b.py").write_text("nothing\n")
        # __pycache__ 应被跳过
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "c.py").write_text("needle hidden\n")

        tool = GrepTool(str(tmp_path))
        result = await tool.execute(pattern="needle")
        assert result.success
        assert "sub/a.py" in result.output
        assert "__pycache__" not in result.output
