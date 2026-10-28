"""D9 加载期隔离 · admission 子进程——插件代码的第一次执行不在主进程。

设计（microkernel-design.md D9 定案 + openx-kernel-design.md §1.4）：
**加载期先上 L2，工具运行期维持 L1**。加载期只有 ``import`` + ``self_test()``，
面窄；此前它在主进程 daemon 线程里 ``exec``（write_plugin 的 admit 管线），
10s 超时后线程带全权限泄漏且不可杀。本模块把这次执行搬进**子进程**：

- **父侧**：``run_admission(path, sha256)``——``subprocess.run`` 拉起
  ``python -m openx.kernel.sandbox.admission <path> <sha256>``，超时即 kill
  （子进程死 = 泄漏为零）；文件 sha256 父子两侧各验一次，关掉
  "校验的是 A、执行的是 B" 的 TOCTOU 窗口。
- **子侧**：按平台**尽力而为**沙箱——macOS 经 ``sandbox-exec``（deny
  network、deny 工作区外 file-write；沙箱不可用降级裸子进程 + 告警），
  POSIX 侧再叠 ``resource.setrlimit``（CPU / 内存 / 进程数）。Linux/Windows
  没有开箱的等价物——**做不到的事如实写**，不假装是证明（自演进 §5.1：
  自测是放行的前提，不是放行的替代）。
- **结果协议**：插件的 print 不污染结果——子进程把 self_test 期 stdout
  重定向进缓冲，最后以哨兵行输出 JSON（``openx-admission/v1``）；父侧解析
  哨兵行，哨兵前的原样字节作为 output 回传。

调试逃生口：``OPENX_PLUGIN_ADMISSION=0`` 跳过整道门（告警降级）——
关掉的是隔离，不是校验；风险自担。
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

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

#: 结果哨兵：子进程 stdout 的最后一行以它开头，其余是插件自身输出。
SENTINEL = "@@OPENX_ADMISSION@@"

#: 单次 admission 的默认超时秒（子进程被 kill，无泄漏）。
ADMISSION_TIMEOUT = 15.0

#: 子进程内存上限字节（setrlimit RLIMIT_AS；平台不支持则跳过）。
_CHILD_MEM_BYTES = 512 * 1024 * 1024


@dataclass
class AdmissionResult:
    """一次 admission 的结论：ok / 结构化 error / 插件 self_test 的输出。"""

    ok: bool
    error: str = ""
    output: str = ""


def admission_enabled() -> bool:
    """``OPENX_PLUGIN_ADMISSION=0`` 显式关掉（调试逃生口，告警降级）。"""
    return os.environ.get("OPENX_PLUGIN_ADMISSION", "") != "0"


def file_sha256(path: Path) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


# ── 父侧 ─────────────────────────────────────────────────────────

def run_admission(
    path: Path,
    timeout: float = ADMISSION_TIMEOUT,
    sha256: str | None = None,
) -> AdmissionResult:
    """在受控子进程里 import 模块并跑 ``self_test()``（同步；阻塞调用方）。

    ``sha256`` 缺省由父侧现算；父子各验一次。超时/崩溃/校验失败一律
    ``ok=False``——admission 失败的语义由调用方定（write_plugin 拒绝落盘、
    内核加载拒 import），本模块只出结论。
    """
    path = Path(path)
    if sha256 is None:
        sha256 = file_sha256(path)
    cmd, profile = _child_command(path, sha256)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return AdmissionResult(ok=False, error=f"admission timeout (>{timeout:.0f}s)")
    except OSError as exc:
        return AdmissionResult(ok=False, error=f"cannot spawn admission child: {exc}")
    finally:
        if profile is not None:
            try:
                profile.unlink(missing_ok=True)
            except OSError:
                pass
    return _parse_child_stdout(proc.stdout, proc.stderr)


def _child_command(path: Path, sha256: str) -> tuple[list[str], Path | None]:
    """子进程命令行：macOS 有 sandbox-exec 就套一层（不可用则降级裸跑 + 告警）。

    返回 (命令, 需清理的 profile 路径或 None)。
    """
    base = [sys.executable, "-m", "openx.kernel.sandbox.admission",
            str(path), sha256]
    sandbox_exec = shutil.which("sandbox-exec")
    if sys.platform == "darwin" and sandbox_exec:
        profile = _write_sandbox_profile()
        if profile is not None:
            return [sandbox_exec, "-f", str(profile), *base], profile
        print("openx.kernel.sandbox.admission: sandbox-exec profile unavailable; "
              "falling back to plain subprocess", file=sys.stderr)
    return base, None


_SANDBOX_PROFILE = """
(version 1)
(deny default)
(deny network*)
(allow file-read*)
(allow file-write* (subpath "/private/tmp") (subpath "/tmp"))
(allow process-exec)
(allow process-fork)
(allow sysctl-read)
(allow mach-lookup)
"""


def _write_sandbox_profile() -> Path | None:
    """内置 profile 落临时文件；写不进（罕见）返回 None → 调用方降级。"""
    try:
        fd, name = tempfile.mkstemp(prefix="openx-admission-", suffix=".sb")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_SANDBOX_PROFILE)
        return Path(name)
    except OSError:
        return None


def _parse_child_stdout(stdout: str, stderr: str) -> AdmissionResult:
    """哨兵行 → 结果；没有哨兵行（崩溃/被 OOM 杀）→ 结构化失败。"""
    result_line = None
    output_lines: list[str] = []
    for line in stdout.splitlines():
        if line.startswith(SENTINEL):
            result_line = line[len(SENTINEL):]
        else:
            output_lines.append(line)
    output = "\n".join(output_lines).strip("\n")
    if result_line is None:
        detail = stderr.strip().splitlines()[-1] if stderr.strip() else \
            "child exited without reporting"
        return AdmissionResult(ok=False, error=f"admission child crashed: {detail}",
                               output=output)
    try:
        payload = json.loads(result_line)
    except json.JSONDecodeError:
        return AdmissionResult(ok=False, error="admission child sent malformed result",
                               output=output)
    return AdmissionResult(
        ok=bool(payload.get("ok")),
        error=str(payload.get("error", "")),
        output=output or str(payload.get("output", "")),
    )


# ── 子侧（python -m openx.kernel.sandbox.admission <path> <sha256>）──

def _apply_rlimits() -> None:
    """尽力而为的资源上限：CPU / 内存 / 进程数（平台不支持即跳过）。"""
    try:
        import resource
    except ImportError:  # Windows 无 resource 模块
        return
    try:
        cpu = int(ADMISSION_TIMEOUT + 5)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_AS, (_CHILD_MEM_BYTES,) * 2)
    except (ValueError, OSError):
        pass  # 容器/受限环境可能拒绝收紧——尽力而为，不因它失败


def _run_child(path: str, expected_sha: str) -> int:
    actual = file_sha256(Path(path))
    if actual != expected_sha:
        print(SENTINEL + json.dumps(
            {"ok": False, "error": f"file changed between validation and execution "
             f"(sha256 mismatch)", "output": ""}))
        return 1

    _apply_rlimits()

    # 环境收敛：去掉可能带凭据的 OPENX 侧变量（子进程不需要它们）；
    # 其余环境保留——插件 self_test 可能合理读取 PATH/HOME 等。
    saved = {k: os.environ.pop(k) for k in list(os.environ)
             if k.startswith("OPENX_") and k != "OPENX_PLUGIN_ADMISSION"}

    buf = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = buf
    ok, error = False, ""
    try:
        import importlib.util

        mod_name = f"_openx_admission_{Path(path).stem}"
        mod_spec = importlib.util.spec_from_file_location(mod_name, path)
        if mod_spec is None or mod_spec.loader is None:
            raise ImportError(f"cannot resolve module spec for {path}")
        module = importlib.util.module_from_spec(mod_spec)
        sys.modules[mod_name] = module
        mod_spec.loader.exec_module(module)
        self_test = getattr(module, "self_test", None)
        if not callable(self_test):
            raise TypeError("plugin exports no callable self_test()")
        self_test()
        ok = True
    except BaseException as exc:  # SystemExit/KeyboardInterrupt 也算失败
        error = f"{type(exc).__name__}: {exc}"
    finally:
        sys.stdout = real_stdout
        os.environ.update(saved)

    print(SENTINEL + json.dumps({
        "ok": ok,
        "error": error,
        "output": buf.getvalue()[-8000:],
    }))
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: python -m openx.kernel.sandbox.admission <plugin.py> <sha256>",
              file=sys.stderr)
        sys.exit(2)
    sys.exit(_run_child(sys.argv[1], sys.argv[2]))
