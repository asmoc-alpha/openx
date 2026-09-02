"""Workflow engine — deterministic multi-agent orchestration (Phase 10).

工作流引擎（Phase 10）
======================
Claude Code Workflow 工具的 Python 原生移植：工作流就是一个普通 Python
脚本，定义::

    meta = {"name": "...", "description": "...", "phases": [...]}   # 可选

    async def main(agent, parallel, pipeline, phase, log, args):
        ...

``main`` 的五个钩子（全部以关键字参数注入）：

- ``await agent(prompt, label=None, phase=None,
  subagent_type="general-purpose", schema=None)`` —— 派生一个子代理
  （调用方 agent 的 child），返回其**最终文本**；失败返回 ``None``
  （镜像 Claude Code 的 null 语义，调用方自行过滤）。``schema``（JSON
  Schema）给定时子代理须经 ``structured_output`` 交付结果，钩子返回
  **校验过的 Python 对象**；未履行契约同样落 ``None``。
- ``await parallel([lambda: agent(...), ...])`` —— **屏障**：全部 thunk
  并发执行（受并发信号量上限约束），结果按**原顺序**返回，失败的
  thunk 落 ``None``。
- ``await pipeline(items, stage1, stage2, ...)`` —— 阶段之间**无屏障**：
  每个 item 独立走完整条链；阶段以 ``stage(prev_result, original_item,
  index)`` 调用（同步/异步皆可），某阶段抛异常 → 该 item 落 ``None``。
- ``phase(title)`` —— 记录阶段标记（stats.phases + 暗色进度行）。
- ``log(message)`` —— 暗色进度行。

安全与语义
==========
- 并发上限 :data:`DEFAULT_CONCURRENCY`（镜像 Claude Code 的公式
  ``max(2, min(16, cpu - 2))``），可按引擎覆盖。
- :data:`MAX_AGENTS_PER_RUN` 是兜底闸：超过后 ``agent()`` 抛
  :class:`WorkflowError`——安全错误**绝不**被 parallel/pipeline 吞掉
  （它们在捕获一般异常落 None 之前先放行 WorkflowError / CancelledError）。
- 一次运行内派生的**所有**子代理共享同一把 prompt 锁——它们的交互式
  权限弹窗绝不在 raw-mode stdin 上重叠（Phase 3 约束）。
- 弹窗回调传播：父 executor 当前的 ``on_prompt_start``/``on_prompt_end``
  原样拷给每个子 executor（Phase 3 bug-10 契约）。
- 脚本**无沙箱**执行，与 shell 同级信任；``workflow`` 工具侧是 ASK 权限。
- :func:`list_workflows` 绝不执行脚本：``meta`` 用 ``ast`` 静态读取。
- ``_gather_or_cancel``：parallel/pipeline 的 gather 在任一协程抛异常时
  取消其余兄弟并等待落地——绝不留下孤儿任务。
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

import ast
import asyncio
import inspect
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .subagent import load_subagent_specs

# 镜像 Claude Code 的并发上限公式：至少 2、至多 16、留 2 个核给系统。
DEFAULT_CONCURRENCY = max(2, min(16, (os.cpu_count() or 4) - 2))

# 单次运行的子代理数量兜底闸：防止失控脚本（while True: agent(...)）
# 烧穿 token / 派生无限进程。超过即抛 WorkflowError，整个工作流失败。
MAX_AGENTS_PER_RUN = 500


class WorkflowError(Exception):
    """工作流层面的错误：脚本语法/加载失败、缺少 async main、兜底闸、
    未知 subagent_type、保存的工作流缺失等。工具侧捕获后落成错误结果。"""


@dataclass
class WorkflowStats:
    """一次工作流运行的统计。

    - ``agents_run`` —— 成功跑完的子代理数；
    - ``agents_failed`` —— 子代理流消费抛异常的子代理数（返回 None 的那些）；
    - ``total_output_tokens`` —— 各子代理 ``total_output_tokens`` 之和；
    - ``phases`` —— ``phase(title)`` 记录的阶段标题序列；
    - ``started_at`` / ``elapsed_seconds`` —— 起始时间戳与墙钟耗时。
    """

    agents_run: int = 0
    agents_failed: int = 0
    total_output_tokens: int = 0
    phases: list = field(default_factory=list)
    started_at: str = ""
    elapsed_seconds: float = 0.0


class WorkflowEngine:
    """执行一个工作流脚本：``run(source, args, script_name)`` →
    ``(result, WorkflowStats)``。

    ``agent`` 参数是父 :class:`~openx.agent.OpenXAgent`——所有工作流子代理
    都作为它的 child 派生（共享 console / rules / hooks / tasks）。
    """

    def __init__(self, agent: Any, concurrency: Optional[int] = None) -> None:
        self._agent_parent = agent
        # 并发信号量：限制同时在飞的子代理数（Python 3.10+ 的 Semaphore
        # 不在构造时绑定事件循环，无 running loop 时创建也安全）
        self._semaphore = asyncio.Semaphore(concurrency or DEFAULT_CONCURRENCY)
        # 一次运行内所有子代理共享的 prompt 锁（注入每个子 executor）
        self._prompt_lock = asyncio.Lock()
        self._agents_started = 0       # 兜底闸计数（按"开始"计，失败也算）
        self._agent_refs: list = []    # 持有子代理引用防 GC（共享对象生命周期）
        self._stats = WorkflowStats()

    # ── 主入口 ──────────────────────────────────────────────────

    async def run(
        self,
        source: str,
        args: Any = None,
        script_name: str = "<inline>",
    ) -> tuple[Any, WorkflowStats]:
        """编译并执行工作流脚本，返回 ``(main 的返回值, 统计)``。

        脚本错误（语法 / 加载 / 缺 main / main 非 async）与 main 内部的
        任何异常一律收敛成 :class:`WorkflowError`——调用方只需捕获一种异常。
        """
        started = time.monotonic()
        self._stats = WorkflowStats(started_at=time.strftime("%Y-%m-%dT%H:%M:%S"))

        # 1. 编译 + 执行脚本顶层（meta / main 的定义在此阶段产出）
        ns: dict[str, Any] = {}
        try:
            exec(compile(source, script_name, "exec"), ns)
        except SyntaxError as e:
            raise WorkflowError(
                f"Workflow script syntax error in {script_name}: {e}"
            ) from e
        except Exception as e:
            raise WorkflowError(
                f"Workflow script failed to load: {type(e).__name__}: {e}"
            ) from e

        # 2. main 必须存在且是 async def
        main = ns.get("main")
        if main is None:
            raise WorkflowError(
                "Workflow script must define 'async def main(agent, parallel, "
                "pipeline, phase, log, args)'"
            )
        if not asyncio.iscoroutinefunction(main):
            raise WorkflowError(
                "Workflow 'main' must be an async function (async def main(...))"
            )

        # 3. meta 软校验：只用于展示，坏了仅提示、绝不失败
        meta = ns.get("meta")
        if meta is not None and not isinstance(meta, dict):
            self._dim(
                f"warning: workflow meta should be a dict, got "
                f"{type(meta).__name__} — ignoring"
            )

        # 4. 注入五个钩子并运行 main
        try:
            result = await main(
                agent=self._agent,
                parallel=self._parallel,
                pipeline=self._pipeline,
                phase=self._phase,
                log=self._log,
                args=args,
            )
        except (WorkflowError, asyncio.CancelledError):
            raise  # 安全错误与取消原样上抛，绝不二次包装
        except Exception as e:
            raise WorkflowError(
                f"Workflow raised: {type(e).__name__}: {e}"
            ) from e
        finally:
            self._stats.elapsed_seconds = time.monotonic() - started
        return result, self._stats

    # ── 五个钩子 ────────────────────────────────────────────────

    async def _agent(
        self,
        prompt: str,
        label: Optional[str] = None,
        phase: Optional[str] = None,
        subagent_type: str = "general-purpose",
        schema: Optional[dict] = None,
    ) -> Any:
        """派生一个子代理跑 prompt，返回其最终文本；失败返回 None。

        ``schema``（JSON Schema）非 None 时子代理携带结构化输出契约：
        成功 → 返回**校验过的 Python 对象**（非文本，脚本直接取字段）；
        子代理跑完仍未调用 structured_output → 计入失败并返回 None。

        兜底闸按"开始数"计（失败的调用也占名额）；构建失败（如未知
        subagent_type）抛 WorkflowError 上抛，不计入 failed。
        """
        if schema is not None and not isinstance(schema, dict):
            raise WorkflowError(
                f"agent() schema must be a JSON Schema object, "
                f"got {type(schema).__name__}"
            )
        if self._agents_started >= MAX_AGENTS_PER_RUN:
            raise WorkflowError(
                f"Workflow exceeded the {MAX_AGENTS_PER_RUN}-agent safety cap"
            )
        self._agents_started += 1
        async with self._semaphore:
            if phase:
                self._log(f"● [{phase}] {label or str(prompt)[:40]}")
            child = _build_workflow_child(
                self._agent_parent, subagent_type, self._prompt_lock,
                structured_schema=schema,
            )
            self._agent_refs.append(child)
            # 弹窗回调传播（Phase 3 bug-10 契约）：子代理弹窗时同样暂停
            # 父级 InputCapture——拷贝父 executor 的**当前**回调值。
            parent_executor = getattr(self._agent_parent, "tool_executor", None)
            child_executor = getattr(child, "tool_executor", None)
            if parent_executor is not None and child_executor is not None:
                child_executor.on_prompt_start = getattr(
                    parent_executor, "on_prompt_start", None
                )
                child_executor.on_prompt_end = getattr(
                    parent_executor, "on_prompt_end", None
                )
            # fleet 视图 + 流捕获（与 task 工具同款契约：消费子代理流
            # 进内存缓冲，零终端写；状态层可展示工作流子代理运行态）
            fleet = getattr(self._agent_parent, "fleet", None)
            view = (
                fleet.register(label or str(prompt)[:40], subagent_type)
                if fleet is not None else None
            )
            errored = False
            try:
                async for event in child.stream_run(prompt):
                    if view is not None:
                        view.feed(event)
            except asyncio.CancelledError:
                errored = True
                raise
            except Exception:
                # 镜像 Claude Code：agent() 失败 → None，调用方过滤
                errored = True
                self._stats.agents_failed += 1
                return None
            finally:
                if fleet is not None and view is not None:
                    fleet.complete(view, is_error=errored)  # 幂等
            # 终值经 history 重建（与 run() 同源，不经 token 拼接）；
            # 延迟导入防 core→tools 顶层环（同 _build_workflow_child 手法）
            from ..tools.subagent_tool import _child_final_text
            final = _child_final_text(child)
            # 结构化契约：只认 structured_output 捕获的校验结果
            if schema is not None:
                has_result = getattr(child, "has_structured_result", None)
                if has_result is None or not has_result():
                    self._stats.agents_failed += 1
                    return None
                final = child.structured_result
            self._stats.agents_run += 1
            self._stats.total_output_tokens += int(
                getattr(child, "total_output_tokens", 0) or 0
            )
            return final

    async def _parallel(self, thunks: list) -> list:
        """屏障式并发：全部 thunk 同时开跑，结果按原顺序返回。

        单个 thunk 抛一般异常 → 该槽位落 None（绝不连累兄弟）；
        WorkflowError / CancelledError 例外——安全错误必须上抛。
        thunk 同步/异步皆可（返回 awaitable 就 await）。
        """
        if not isinstance(thunks, (list, tuple)):
            raise WorkflowError(
                "parallel() expects a list of thunks (zero-arg callables)"
            )
        for t in thunks:
            if not callable(t):
                raise WorkflowError(f"parallel() thunk is not callable: {t!r}")

        async def one(t):
            try:
                r = t()
                if inspect.isawaitable(r):
                    r = await r
                return r
            except (WorkflowError, asyncio.CancelledError):
                raise
            except Exception:
                return None

        return list(await _gather_or_cancel(*(one(t) for t in thunks)))

    async def _pipeline(self, items: list, *stages) -> list:
        """流水线：每个 item 独立走完整条 stage 链（阶段间**无屏障**）。

        stage 以 ``stage(prev_result, original_item, index)`` 调用（同步/
        异步皆可）；任一 stage 抛一般异常 → 该 item 落 None，其余 item
        不受影响。WorkflowError / CancelledError 上抛。
        """
        if not isinstance(items, (list, tuple)):
            raise WorkflowError("pipeline() expects a list of items")

        async def run_item(item, i):
            r = item
            for s in stages:
                try:
                    r = s(r, item, i)
                    if inspect.isawaitable(r):
                        r = await r
                except (WorkflowError, asyncio.CancelledError):
                    raise
                except Exception:
                    return None
            return r

        return list(await _gather_or_cancel(
            *(run_item(item, i) for i, item in enumerate(items))
        ))

    def _phase(self, title: str) -> None:
        """记录阶段标记：stats.phases + 暗色分隔行。"""
        self._stats.phases.append(str(title))
        self._dim(f"── {title} ──")

    def _log(self, message: str) -> None:
        """暗色进度行（与 agent 流的工具回显同款 [dim] 风格）。"""
        self._dim(str(message))

    # ── 内部 ────────────────────────────────────────────────────

    def _dim(self, message: str) -> None:
        """经父 agent 的 console 打印暗色行；console 不可用时静默降级。"""
        console = getattr(self._agent_parent, "console", None)
        raw = getattr(console, "_console", None)
        if raw is None:
            return
        try:
            raw.print(f"[dim]{message}[/dim]")
        except Exception:
            pass


# ── 子代理构建与保存工作流 ──────────────────────────────────────


def _build_workflow_child(
    parent: Any,
    subagent_type: str,
    prompt_lock: asyncio.Lock,
    structured_schema: Optional[dict] = None,
) -> Any:
    """按 subagent_type 从父 agent 派生工作流子代理。

    **模块级函数**——测试 monkeypatch 它即可注入鸭子子代理。规格经
    :func:`load_subagent_specs` 解析（未知类型 → WorkflowError 列出可用
    规格）；复用 ``subagent_tool.build_child_agent`` 的既有接线路径，
    随后注入**共享 prompt 锁**：一次工作流运行内所有并发子代理的权限
    弹窗必须串行在同一把锁上（raw-mode stdin 绝不能重叠——Phase 3 约束）。
    ``structured_schema`` 透传给子代理的结构化输出契约。

    延迟导入 ``build_child_agent``：``openx.tools`` 反过来导入本模块所在
    包的工具注册路径，顶层导入可能构成初始化期循环。
    """
    from ..tools.subagent_tool import build_child_agent

    specs = load_subagent_specs(str(parent.workspace))
    spec = specs.get(subagent_type)
    if spec is None:
        raise WorkflowError(
            f"Unknown subagent_type '{subagent_type}'. Available: {sorted(specs)}"
        )
    child = build_child_agent(parent, spec, structured_schema=structured_schema)
    # 文档化的覆盖：ToolExecutor 的 prompt_lock 可由构造参数或属性注入
    child.tool_executor._prompt_lock = prompt_lock
    return child


def load_workflow(workspace: str, name: str) -> tuple[str, Path]:
    """读取 ``<workspace>/.openx/workflows/<name>.py`` → ``(源码, 路径)``。

    缺失 → WorkflowError。拒绝含路径分隔符或以点号开头的名字（防目录
    穿越——工作流名只能是纯文件名主干）。
    """
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise WorkflowError(f"Invalid workflow name: {name!r}")
    path = Path(workspace) / ".openx" / "workflows" / f"{name}.py"
    if not path.is_file():
        raise WorkflowError(f"Workflow '{name}' not found at {path}")
    return path.read_text(encoding="utf-8"), path


def list_workflows(workspace: str) -> list[dict]:
    """列出 ``.openx/workflows/*.py`` → ``[{name, description, path}]``（按名排序）。

    **绝不执行脚本**：``meta`` 用 ``ast`` 静态解析——顶层 ``meta = {...}``
    赋值且键值为字符串常量的条目才提取；任何解析失败都降级为
    ``{name: 文件名主干, description: ""}``。
    """
    wdir = Path(workspace) / ".openx" / "workflows"
    if not wdir.is_dir():
        return []
    rows: list[dict] = []
    for path in sorted(wdir.glob("*.py")):
        info: dict[str, Any] = {"name": path.stem, "description": "", "path": path}
        try:
            meta = _parse_meta(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            meta = {}
        if isinstance(meta.get("name"), str) and meta["name"]:
            info["name"] = meta["name"]
        if isinstance(meta.get("description"), str):
            info["description"] = meta["description"]
        rows.append(info)
    return sorted(rows, key=lambda r: r["name"])


def _parse_meta(source: str) -> dict:
    """静态提取脚本顶层 ``meta = {...}`` 字典字面量中的字符串常量键值。

    ast 解析：仅接受 ``ast.Assign`` 目标为名字 ``meta``、值为 Dict 字面量、
    且键为字符串常量的条目；非常量值（如函数调用）整体忽略——列举路径
    对坏文件只降级、绝不执行、绝不抛异常给调用方。
    """
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "meta":
                out: dict[str, str] = {}
                for k, v in zip(node.value.keys, node.value.values):
                    if (
                        isinstance(k, ast.Constant)
                        and isinstance(k.value, str)
                        and isinstance(v, ast.Constant)
                        and isinstance(v.value, str)
                    ):
                        out[k.value] = v.value
                return out
    return {}


# ── 并发原语 ────────────────────────────────────────────────────


async def _gather_or_cancel(*coros) -> list:
    """asyncio.gather + 异常时取消兄弟：绝不留下孤儿任务。

    任一协程抛异常 → 取消其余未完成的兄弟协程并等待它们全部落地，
    再把首个异常重新抛出（与裸 gather 的传播语义一致，但无孤儿）。
    """
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


if __name__ == "__main__":
    import tempfile

    # ── 鸭子替身：自检绝不构造真实 OpenXAgent ──────────────────
    class _FakeRaw:
        def __init__(self):
            self.lines: list[str] = []

        def print(self, s):
            self.lines.append(s)

    class _FakeConsole:
        def __init__(self):
            self._console = _FakeRaw()

    class _FakeExecutor:
        on_prompt_start = None
        on_prompt_end = None

    class _FakeChild:
        def __init__(self):
            self.tool_executor = _FakeExecutor()
            self.total_output_tokens = 7
            self.history = type("H", (), {"messages": []})()

        async def stream_run(self, prompt):
            await asyncio.sleep(0)
            yield f"ok:{prompt}"
            self.history.messages.append(
                {"role": "assistant", "content": f"ok:{prompt}"}
            )

    class _FakeParent:
        workspace = "."
        console = _FakeConsole()
        tool_executor = _FakeExecutor()

    assert 2 <= DEFAULT_CONCURRENCY <= 16
    assert MAX_AGENTS_PER_RUN == 500

    # parallel + pipeline + phase + log：替换模块级构建函数（模块顶层
    # 本就在模块作用域，无需 global 声明——与函数内自检的写法不同）
    _real = _build_workflow_child
    _build_workflow_child = (
        lambda parent, st, lock, structured_schema=None: _FakeChild()
    )
    try:
        _script = (
            "meta = {'name': 'selftest', 'description': 'd'}\n"
            "async def main(agent, parallel, pipeline, phase, log, args):\n"
            "    phase('P1')\n"
            "    rs = await parallel([lambda: agent('a'), lambda: agent('b')])\n"
            "    vs = await pipeline([1, 2], lambda v, o, i: agent(f'v{v}'))\n"
            "    log('done')\n"
            "    return {'rs': rs, 'vs': vs, 'args': args}\n"
        )
        _engine = WorkflowEngine(_FakeParent())
        _result, _stats = asyncio.run(_engine.run(_script, args={"x": 1}))
        assert _result == {
            "rs": ["ok:a", "ok:b"],
            "vs": ["ok:v1", "ok:v2"],
            "args": {"x": 1},
        }
        assert _stats.agents_run == 4 and _stats.agents_failed == 0
        assert _stats.total_output_tokens == 28
        assert _stats.phases == ["P1"] and _stats.elapsed_seconds >= 0.0
        _lines = _FakeParent.console._console.lines
        assert any("P1" in l for l in _lines) and any("done" in l for l in _lines)
        print("engine: parallel+pipeline+phase+log ✓")

        # 失败 thunk / 失败子代理 → None（镜像 Claude Code null 语义）
        _fail_script = (
            "async def main(agent, parallel, pipeline, phase, log, args):\n"
            "    def boom():\n"
            "        raise RuntimeError('kaput')\n"
            "    return await parallel([lambda: agent('fine'), boom])\n"
        )
        _r2, _s2 = asyncio.run(WorkflowEngine(_FakeParent()).run(_fail_script))
        assert _r2 == ["ok:fine", None] and _s2.agents_run == 1
        print("engine: failed thunk → None ✓")
    finally:
        _build_workflow_child = _real

    # 脚本错误收敛为 WorkflowError
    for _bad, _needle in [
        ("def broken(:", "syntax"),
        ("x = 1\n", "must define"),
        ("def main(**kw):\n    return 1\n", "async"),
    ]:
        try:
            asyncio.run(WorkflowEngine(_FakeParent()).run(_bad))
            raise AssertionError("expected WorkflowError")
        except WorkflowError as _e:
            assert _needle in str(_e).lower()
    print("engine: script errors → WorkflowError ✓")

    # 保存工作流：列举（ast 读 meta，坏文件降级）+ 加载 + 缺失报错
    with tempfile.TemporaryDirectory() as _td:
        _wf = Path(_td) / ".openx" / "workflows"
        _wf.mkdir(parents=True)
        (_wf / "hello.py").write_text(
            "meta = {'name': 'hello', 'description': 'Says hi.'}\n"
            "async def main(agent, parallel, pipeline, phase, log, args):\n"
            "    return 1\n",
            encoding="utf-8",
        )
        (_wf / "weird.py").write_text("meta = {'name': build()}\n", encoding="utf-8")
        _rows = list_workflows(_td)
        assert [r["name"] for r in _rows] == ["hello", "weird"]
        assert _rows[0]["description"] == "Says hi." and _rows[1]["description"] == ""
        _src, _path = load_workflow(_td, "hello")
        assert "async def main" in _src and _path.name == "hello.py"
        for _bad_name in ("ghost", "../evil"):
            try:
                load_workflow(_td, _bad_name)
                raise AssertionError("expected WorkflowError")
            except WorkflowError:
                pass
        assert list_workflows(tempfile.mkdtemp()) == []
    print("load/list workflows ✓")
    print("openx/orchestration/workflow.py OK ✓")
