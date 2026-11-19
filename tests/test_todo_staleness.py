"""计划面板新鲜度（todo staleness）回归测试。

**这是修一个真实缺陷的回归**：用户报告"我发 plan 和模型实际的进展完全不匹配"
——截图里 12 项计划停在 4 ✓ / 1 ✢，而实际已推进到 9 项。面板没画错，是数据
过期了，而且**没有任何机制提醒模型去更新**。更糟的是过期期间面板仍以现在
进行时渲染（进行中项显示 ``activeForm``，"正在做什么"），于是它一直在说早已
干完的活。

两道闸，两组断言：

1. **提醒闭环**（本文件主体）：若干轮不写计划 → 系统提示里出现
   :data:`~openx.agent._TODO_STALE_NOTE`；真写一次 ``todo_write`` → 计数归零、
   提醒消失。两个循环（``run`` / ``stream_run``）都要覆盖——只接一个等于只修
   一半（全屏 TUI 走流式，纯 ``run`` 只有 headless 走）。
2. **面板标注**（``tests/tui/test_deck.py`` / ``test_fleet_view.py``）：过期时
   Plan 头行带 ``▲ stale``，用户永远能分清"旧账"与"卡住"。

还有三条容易写错、也容易回归的细节单独立了用例：提醒文本必须**字节稳定**
（含数字就会逐轮变化 → 打断 provider 前缀缓存）、子代理**不提醒**（它的清单
没有界面在渲染，白付 token）、总完成/无清单**不算过期**（没有要维护的东西）。

运行：``python -m pytest tests/test_todo_staleness.py -q``
"""

from __future__ import annotations

import json

import pytest

import openx.config as config_mod
from openx.agent import TODO_STALE_ROUNDS, _TODO_STALE_NOTE
from openx.config import OpenXConfig
from openx.llm import StreamDone
from openx.permissions import PermissionRules

NOTE_HEADER = "## Plan freshness"

#: 一份"未完成"的计划（过期判据要求至少一项不是 completed）
_PLAN = [
    {"content": "step one", "activeForm": "doing step one", "status": "in_progress"},
    {"content": "step two", "activeForm": "", "status": "pending"},
]


def _tc(tc_id: str, name: str, args: dict) -> dict:
    """OpenAI 风格 tool_call 条目。"""
    return {
        "id": tc_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _read(call_id: str = "c0") -> list[dict]:
    """一次只读往返（``read_file`` 是 ALLOW，不会弹权限窗）。"""
    return [_tc(call_id, "read_file", {"file_path": "note.txt"})]


def _write_todos(items: list[dict]) -> list[dict]:
    return [_tc("tw", "todo_write", {"todos": items})]


class RecordingLLM:
    """按剧本应答，并**逐次记录请求里看到的系统提示**。

    记录是为了断言"模型到底看没看到提醒"——这正是缺陷的所在：从前它永远
    看不到，因为没有任何人往提示里放过这句话。
    """

    def __init__(self, tool_rounds: list[list[dict]], final: str = "done"):
        self.tool_rounds = list(tool_rounds)
        self.final = final
        self.seen_systems: list[str] = []

    def _next(self, messages: list[dict]) -> dict:
        self.seen_systems.append(messages[0]["content"])
        idx = len(self.seen_systems) - 1
        if idx < len(self.tool_rounds):
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": self.tool_rounds[idx],
            }
        return {"role": "assistant", "content": self.final}

    async def chat(self, messages, tools=None, stream=True):
        return self._next(messages)

    async def stream_chat(self, messages, tools=None):
        # 生成器是惰性的：这里取消息的时机与真实 provider 一致（循环体已
        # 先把最新系统提示写进 messages[0]）。
        yield StreamDone(response=self._next(messages), token_count=1, input_tokens=1)

    @property
    def stale_seen(self) -> list[bool]:
        return [NOTE_HEADER in s for s in self.seen_systems]


# ── fixtures ────────────────────────────────────────────────────


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离 settings / 会话 / hooks / 内核：不触碰真实 home。"""
    from openx.kernel import reset_kernel

    monkeypatch.setattr(config_mod, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(
        "openx.orchestration.sessions.SESSIONS_DIR", tmp_path / "sessions"
    )
    monkeypatch.setattr(
        "openx.kernel.audit.hooks.SETTINGS_PATH", tmp_path / "no-hooks.json"
    )
    monkeypatch.setattr("openx.orchestration.tasks.TASKS_DIR", tmp_path / "tasks")
    reset_kernel()
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "note.txt").write_text("hello\n")
    yield ws
    reset_kernel()


def _make_agent(ws, llm, *, parent=None):
    from openx.agent import OpenXAgent

    config = OpenXConfig()
    config.workspace = str(ws)
    config.model = "test-model"
    agent = OpenXAgent(config, parent=parent)
    agent.llm = llm
    agent.tool_executor._rules = PermissionRules()  # 忽略真实 settings.json
    return agent


# ── 1. 计数 ─────────────────────────────────────────────────────


class TestCounter:
    async def test_rounds_without_todo_write_accumulate(self, env):
        llm = RecordingLLM([_read("c0"), _read("c1"), _read("c2")])
        agent = _make_agent(env, llm)
        agent.todos[:] = _PLAN

        await agent.run("go")

        assert agent.tool_rounds_since_todo_write == 3

    async def test_todo_write_resets_the_counter(self, env):
        """写一次就归零——这是闭环的另一半（有提醒、也有出路）。"""
        llm = RecordingLLM([
            _read("c0"), _read("c1"),              # 2 轮没写
            _write_todos(_PLAN),                   # 这一轮写了
            _read("c2"), _read("c3"),              # 再 2 轮
        ])
        agent = _make_agent(env, llm)
        await agent.run("go")

        assert agent.tool_rounds_since_todo_write == 2, "写之后只该数新的 2 轮"

    async def test_same_batch_counts_as_written(self, env):
        """同批里既有 todo_write 又有别的工具 → 记为已写（批次粒度）。

        并行批次是"一次决策"，模型在这一批里看到了 todo_write 的回执；
        按工具顺序去猜"谁先谁后"没有意义。
        """
        llm = RecordingLLM([_read("c0") + _write_todos(_PLAN)])
        agent = _make_agent(env, llm)
        agent.todos[:] = _PLAN

        await agent.run("go")

        assert agent.tool_rounds_since_todo_write == 0

    async def test_stream_loop_counts_too(self, env):
        """``stream_run`` 是 TUI / REPL 的真正路径——只接 ``run`` 等于没修。"""
        llm = RecordingLLM([_read("c0"), _read("c1")])
        agent = _make_agent(env, llm)
        agent.todos[:] = _PLAN

        async for _ in agent.stream_run("go"):
            pass

        assert agent.tool_rounds_since_todo_write == 2

    async def test_clear_history_resets(self, env):
        llm = RecordingLLM([_read("c0"), _read("c1")])
        agent = _make_agent(env, llm)
        agent.todos[:] = _PLAN
        await agent.run("go")
        assert agent.tool_rounds_since_todo_write == 2

        agent.clear_history()

        assert agent.tool_rounds_since_todo_write == 0
        assert agent.todos == []


# ── 2. 过期判据 ─────────────────────────────────────────────────


class TestStaleVerdict:
    async def test_not_stale_before_the_threshold(self, env):
        rounds = [_read(f"c{i}") for i in range(TODO_STALE_ROUNDS - 1)]
        agent = _make_agent(env, RecordingLLM(rounds))
        agent.todos[:] = _PLAN

        await agent.run("go")

        assert agent.tool_rounds_since_todo_write == TODO_STALE_ROUNDS - 1
        assert agent.todos_stale is False

    async def test_stale_at_the_threshold(self, env):
        rounds = [_read(f"c{i}") for i in range(TODO_STALE_ROUNDS)]
        agent = _make_agent(env, RecordingLLM(rounds))
        agent.todos[:] = _PLAN

        await agent.run("go")

        assert agent.tool_rounds_since_todo_write == TODO_STALE_ROUNDS
        assert agent.todos_stale is True

    async def test_all_completed_is_never_stale(self, env):
        """全部完成 → 面板折叠成 ``✓ Plan N/N``，那是收尾记录而非进度。"""
        rounds = [_read(f"c{i}") for i in range(TODO_STALE_ROUNDS + 2)]
        agent = _make_agent(env, RecordingLLM(rounds))
        agent.todos[:] = [
            {"content": "a", "activeForm": "", "status": "completed"},
            {"content": "b", "activeForm": "", "status": "completed"},
        ]

        await agent.run("go")

        assert agent.tool_rounds_since_todo_write > TODO_STALE_ROUNDS
        assert agent.todos_stale is False

    async def test_no_plan_is_never_stale(self, env):
        rounds = [_read(f"c{i}") for i in range(TODO_STALE_ROUNDS)]
        agent = _make_agent(env, RecordingLLM(rounds))

        await agent.run("go")

        assert agent.tool_rounds_since_todo_write == TODO_STALE_ROUNDS
        assert agent.todos_stale is False, "没有计划就无所谓过期"

    async def test_child_agent_is_never_stale(self, env):
        """子代理也带 todo_write，但它的清单没有任何界面在渲染。

        给它发提醒只是白付 token——与"模式指令仅顶层注入"同一口径。
        """
        parent = _make_agent(env, RecordingLLM([]))
        llm = RecordingLLM([_read(f"c{i}") for i in range(TODO_STALE_ROUNDS + 1)])
        child = _make_agent(env, llm, parent=parent)
        child.todos[:] = _PLAN

        await child.run("go")

        assert child.tool_rounds_since_todo_write > TODO_STALE_ROUNDS
        assert child.todos_stale is False
        assert not any(llm.stale_seen), "子代理的提示里绝不该出现提醒"


# ── 3. 提醒真的到了模型眼前 ─────────────────────────────────────


class TestReminder:
    async def test_reminder_enters_the_prompt_once_stale(self, env):
        n = TODO_STALE_ROUNDS
        llm = RecordingLLM([_read(f"c{i}") for i in range(n)])
        agent = _make_agent(env, llm)
        agent.todos[:] = _PLAN

        await agent.run("go")

        # 请求 1..n 时计数还 < 阈值（第 k 次请求前计数是 k-1）；第 n+1 次
        # （最终文本那次）计数已到 n → 提醒到场。
        assert len(llm.seen_systems) == n + 1
        assert not any(llm.stale_seen[:n]), "过期前不该有提醒"
        assert llm.stale_seen[n], "越过阈值后必须提醒模型"

    async def test_reminder_disappears_after_a_write(self, env):
        n = TODO_STALE_ROUNDS
        llm = RecordingLLM(
            [_read(f"c{i}") for i in range(n)] + [_write_todos(_PLAN), _read("c9")]
        )
        agent = _make_agent(env, llm)
        agent.todos[:] = _PLAN

        await agent.run("go")

        # 提醒在越线后出现（第 n+1 次请求），写了之后清掉（第 n+3 次请求）
        assert llm.stale_seen[n] is True
        assert llm.stale_seen[-1] is False, "写完之后提醒必须撤掉"

    async def test_reminder_text_is_byte_stable_while_stale(self, env):
        """含数字就会逐轮变化 → 打断 provider 前缀缓存。文本必须恒定。"""
        n = TODO_STALE_ROUNDS
        llm = RecordingLLM([_read(f"c{i}") for i in range(n + 4)])
        agent = _make_agent(env, llm)
        agent.todos[:] = _PLAN

        await agent.run("go")

        stale_systems = [s for s in llm.seen_systems if NOTE_HEADER in s]
        assert len(stale_systems) >= 4, "越线后每轮都该带着提醒"
        assert len(set(stale_systems)) == 1, "过期期间系统提示必须逐轮字节一致"
        # 提醒正文本身也不得含计数字面量（下一步就是"有人加了个数字进去"）
        assert str(TODO_STALE_ROUNDS) not in _TODO_STALE_NOTE

    async def test_reminder_names_the_tool_and_the_way_out(self, env):
        """提醒必须给出**可执行**的出路，否则模型只会一轮轮装作没看见。"""
        assert "todo_write" in _TODO_STALE_NOTE
        assert "empty list" in _TODO_STALE_NOTE or "empty" in _TODO_STALE_NOTE
        assert "in_progress" in _TODO_STALE_NOTE

    async def test_reminder_is_not_baked_into_the_cached_prompt(self, env):
        """提醒只拼在下发的 ``message[0]``，不污染 ``_system_prompt`` 身份。

        那份字段只在装配变化时重建（context 片段会重算），为了一个逐轮变化的
        计数去重建它既浪费也会让缓存键抖动。
        """
        n = TODO_STALE_ROUNDS
        llm = RecordingLLM([_read(f"c{i}") for i in range(n)])
        agent = _make_agent(env, llm)
        agent.todos[:] = _PLAN
        before = agent._system_prompt

        await agent.run("go")

        assert agent.todos_stale is True
        assert agent._system_prompt == before, "缓存提示不该被提醒污染"
        assert NOTE_HEADER not in agent._system_prompt
        assert NOTE_HEADER in llm.seen_systems[-1], "但下发的消息里必须有"

    async def test_stream_loop_also_sends_the_reminder(self, env):
        n = TODO_STALE_ROUNDS
        llm = RecordingLLM([_read(f"c{i}") for i in range(n)])
        agent = _make_agent(env, llm)
        agent.todos[:] = _PLAN

        async for _ in agent.stream_run("go"):
            pass

        assert llm.stale_seen[n], "流式路径同样要提醒（TUI 走的就是这条）"


# ── 4. /todos 的输出也说真话 ────────────────────────────────────


class TestTodosCommandOutput:
    @staticmethod
    def _render(todos, **kwargs) -> str:
        from rich.console import Console as RichConsole

        from openx.ui._components.misc import MiscMixin

        rec = RichConsole(record=True, width=120)
        holder = type("H", (MiscMixin,), {})()
        holder._console = rec
        holder.print_todos(todos, **kwargs)
        return rec.export_text()

    def test_fresh_plan_has_no_warning(self):
        text = self._render(_PLAN, stale=False, stale_rounds=0)
        assert "may be behind" not in text

    def test_stale_plan_warns_with_the_round_count(self):
        text = self._render(_PLAN, stale=True, stale_rounds=12)
        assert "12 tool rounds since the last update" in text
        assert "may be behind" in text

    def test_stale_without_a_count_still_warns(self):
        text = self._render(_PLAN, stale=True)
        assert "not updated for a while" in text

    def test_empty_list_says_so(self):
        assert "No tasks" in self._render([])
