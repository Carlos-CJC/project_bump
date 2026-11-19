#!/usr/bin/env python3
"""Bump 组 9 成果的跨平台启动器。

只依赖标准库，用 pyenv 选定的 Python 3.12 创建项目内 .venv：
    ./run.sh setup          # macOS / Linux
    .\\run.ps1 setup         # Windows (PowerShell)

其余子命令都通过 .venv 里的解释器执行，两端路径一致。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
BACKEND = ROOT / "02_后端源码"
COMPAT = ROOT / "01_前端源码" / "Bump_EIE568_MQTT_v1_兼容演示"
HARDWARE = ROOT / "01_前端源码" / "双板实物"
SERVER_TESTS = ROOT / "04_测试程序" / "服务端"
STAGE = ROOT / ".run" / "server"
REQUIREMENTS = BACKEND / "requirements.txt"

SERVER_SCRIPTS = (
    "smoke_test.py",
    "matching_test.py",
    "connection_stages_test.py",
    "upload_recovery_test.py",
    "sync_status_test.py",
)
NODE_TESTS = ("frontend_compat_lab_test.cjs", "frontend_session_test.cjs")


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def base_env() -> dict[str, str]:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
    # 避免把用户全局 site-packages 带进验证结果。
    env.pop("PYTHONPATH", None)
    return env


def run(cmd: list[str], *, cwd: Path = ROOT, check: bool = False) -> int:
    printable = " ".join(str(part) for part in cmd)
    print(f"$ {printable}", flush=True)
    return subprocess.call([str(part) for part in cmd], cwd=str(cwd), env=base_env())


def fail(message: str) -> int:
    print(f"错误：{message}", file=sys.stderr)
    return 2


def ensure_venv(python: str | None = None) -> Path:
    target = venv_python()
    if target.exists():
        return target
    executable = python or sys.executable
    if python:
        probe = subprocess.run(
            [python, "-c", "import sys; raise SystemExit(0 if sys.version_info[:2]==(3,12) else 1)"],
            capture_output=True,
        )
        if probe.returncode != 0:
            raise SystemExit(f"{python} 不是 Python 3.12。")
    elif sys.version_info[:2] != (3, 12):
        raise SystemExit(
            f"需要 Python 3.12，当前是 {sys.version.split()[0]}。"
            "请先执行 pyenv install 3.12.14，再在 group9 目录重试。"
        )
    print(f"创建虚拟环境：{VENV}", flush=True)
    if run([executable, "-m", "venv", str(VENV)]) != 0 or not target.exists():
        raise SystemExit("虚拟环境创建失败。")
    return target


def importable_smoke(py: Path) -> bool:
    probe = "import fastapi, amqtt, uvicorn, paho, sqlite3; print('deps-ok')"
    return run([py, "-c", probe], check=False) == 0


def cmd_setup(args: argparse.Namespace) -> int:
    py = ensure_venv(args.python)
    if run([py, "-m", "pip", "install", "--disable-pip-version-check", "-r", str(REQUIREMENTS)]) != 0:
        return fail("依赖安装失败，请检查网络或 requirements.txt。")
    if not importable_smoke(py):
        return fail("依赖装完后仍无法导入，请查看上面的报错。")
    result = subprocess.run(
        [str(py), "-c", "import sys, sqlite3; print(sys.version.split()[0], sqlite3.sqlite_version)"],
        capture_output=True, text=True, env=base_env(),
    )
    print(f"环境就绪：{py}")
    print(f"Python / SQLite：{result.stdout.strip()}")
    print("下一步：./run.sh demo（macOS）或 .\\run.ps1 demo（Windows）")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    py = venv_python()
    info = {
        "本进程": sys.version.split()[0],
        "虚拟环境": str(py),
        "虚拟环境存在": py.exists(),
        "系统": f"{sys.platform} / {os.name}",
        "兼容演示": str(COMPAT),
        "后端源码": str(BACKEND),
        "服务测试": str(SERVER_TESTS),
        "node": shutil.which("node") or None,
    }
    if py.exists():
        probe = (
            "import json, fastapi, amqtt, uvicorn, paho.mqtt, sys, sqlite3;"
            "print(json.dumps({'python': sys.version.split()[0], 'fastapi': fastapi.__version__,"
            "'amqtt': amqtt.__version__, 'uvicorn': uvicorn.__version__,"
            "'sqlite': sqlite3.sqlite_version}))"
        )
        result = subprocess.run([str(py), "-c", probe], capture_output=True, text=True, env=base_env())
        info["依赖"] = json.loads(result.stdout) if result.returncode == 0 else result.stderr.strip()
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0 if py.exists() and not isinstance(info.get("依赖"), str) else 1


def cmd_demo(args: argparse.Namespace) -> int:
    py = venv_python()
    cmd = [str(py), str(COMPAT / "launch_local.py")]
    if args.runtime:
        cmd += ["--runtime", args.runtime]
    cmd += ["--web-port", str(args.web_port), "--mqtt-port", str(args.mqtt_port)]
    if args.self_check:
        cmd.append("--self-check")
    if args.no_browser:
        cmd.append("--no-browser")
    return run(cmd, cwd=COMPAT)


def cmd_simulate(args: argparse.Namespace) -> int:
    py = venv_python()
    cmd = [str(py), "-m", "bump", "--runtime", args.runtime] if args.runtime else [str(py), "-m", "bump"]
    cmd += ["simulate", "--scenario", args.scenario]
    return run(cmd, cwd=COMPAT)


def prepare_server(*, force: bool = False) -> Path:
    if force and STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True, exist_ok=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
    shutil.copytree(BACKEND, STAGE, dirs_exist_ok=True, ignore=ignore)
    shutil.copytree(HARDWARE / "web", STAGE / "web", dirs_exist_ok=True)
    for name in sorted(SERVER_TESTS.glob("*.py")):
        shutil.copy2(name, STAGE / name.name)
    return STAGE


def cmd_prepare(args: argparse.Namespace) -> int:
    stage = prepare_server(force=args.force)
    print(f"已准备服务端校验目录：{stage}")
    print("可用命令：")
    print(f"  run server inspect --runtime <目录>")
    print(f"  run server serve --runtime <目录> --mqtt-host 127.0.0.1")
    print(f"  run test --suite server")
    return 0


def cmd_server(args: argparse.Namespace) -> int:
    if not args.forward:
        return fail("请提供 live_server 参数，例如：run.py server init --runtime DIR --device-a ...")
    stage = prepare_server()
    py = venv_python()
    return run([py, str(stage / "live_server.py"), *args.forward], cwd=ROOT)


def cmd_bridge(args: argparse.Namespace) -> int:
    if not args.forward:
        return fail("请提供参数，例如：run.py bridge --runtime group9/.run/state/hardware-live")
    py = venv_python()
    return run([py, str(ROOT / "hivemq_bridge.py"), *args.forward], cwd=ROOT)


def pytest_targets(compat_v1_only: bool) -> list[str]:
    tests = COMPAT / "tests"
    if not compat_v1_only:
        return [str(tests)]
    wanted = sorted(
        path for path in tests.glob("test_*.py")
        if path.name.startswith(("test_v1_", "test_compat_v1_"))
    )
    return [str(path) for path in wanted]


def run_suite(name: str) -> int:
    py = venv_python()
    if name == "compat":
        return run([py, "-m", "pytest", "-q", *pytest_targets(False)], cwd=COMPAT)
    if name == "compat-v1":
        targets = pytest_targets(True)
        if len(targets) < 4:
            return fail("兼容 v1 用例不足 4 个，请检查 01_前端源码 下的 tests 目录。")
        return run([py, "-m", "pytest", "-q", "-p", "no:cacheprovider", *targets], cwd=COMPAT)
    if name == "server":
        stage = prepare_server()
        failures = 0
        for script in SERVER_SCRIPTS:
            if run([py, "-X", "utf8", str(stage / script)], cwd=stage) != 0:
                failures += 1
        return 1 if failures else 0
    if name == "node":
        node = shutil.which("node")
        if not node:
            return fail("未找到 node。安装 Node.js 后重试，或只跑 --suite compat。")
        failures = 0
        for script in NODE_TESTS:
            if run([node, str(COMPAT / "tests" / script)], cwd=COMPAT) != 0:
                failures += 1
        return 1 if failures else 0
    return fail(f"未知测试套件：{name}")


def cmd_test(args: argparse.Namespace) -> int:
    if args.suite == "all":
        suites = ["compat", "server"]
        node = shutil.which("node")
        if node:
            suites.append("node")
        else:
            print("提示：未找到 node，跳过纯前端用例（可用 --suite node 单独检查）。")
    else:
        suites = [args.suite]
    failures = 0
    for suite in suites:
        print(f"\n=== {suite} ===", flush=True)
        if run_suite(suite) != 0:
            failures += 1
    print(f"\n完成：{len(suites) - failures}/{len(suites)} 个套件通过")
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bump 组 9 跨平台启动器")
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup", help="用 pyenv 的 Python 3.12 创建 .venv 并安装依赖")
    setup.add_argument("--python", help="指定用于创建虚拟环境的 Python 解释器")
    setup.set_defaults(func=cmd_setup)

    doctor = sub.add_parser("doctor", help="打印环境与依赖检查结果")
    doctor.set_defaults(func=cmd_doctor)

    demo = sub.add_parser("demo", help="启动 MQTT v1 兼容演示（无主板）")
    demo.add_argument("--runtime", help="账号与数据目录，默认在兼容演示目录下 .runtime")
    demo.add_argument("--web-port", type=int, default=8765)
    demo.add_argument("--mqtt-port", type=int, default=18883)
    demo.add_argument("--self-check", action="store_true")
    demo.add_argument("--no-browser", action="store_true")
    demo.set_defaults(func=cmd_demo)

    simulate = sub.add_parser("simulate", help="向已启动的兼容演示注入模拟偶遇")
    simulate.add_argument("--scenario", choices=("normal", "offline", "retry"), default="normal")
    simulate.add_argument("--runtime")
    simulate.set_defaults(func=cmd_simulate)

    prepare = sub.add_parser("prepare", help="组装实板接收端与服务测试目录到 .run/server")
    prepare.add_argument("--force", action="store_true", help="删除并重建 .run/server")
    prepare.set_defaults(func=cmd_prepare)

    server = sub.add_parser("server", help="透传参数给 live_server.py（init/serve/inspect/ingest）")
    server.add_argument("forward", nargs=argparse.REMAINDER)
    server.set_defaults(func=cmd_server)

    bridge = sub.add_parser("bridge", help="订阅公开 HiveMQ 并写入本地 runtime 数据库")
    bridge.add_argument("forward", nargs=argparse.REMAINDER)
    bridge.set_defaults(func=cmd_bridge)

    test = sub.add_parser("test", help="运行兼容演示、服务端或前端测试")
    test.add_argument("--suite", choices=("all", "compat", "compat-v1", "server", "node"), default="all")
    test.set_defaults(func=cmd_test)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command != "setup":
        target = venv_python()
        if not target.exists():
            return fail("尚未创建虚拟环境。先运行：run.py setup")
        # 统一在 .venv 中执行，避免宿主解释器差异。
        if Path(sys.prefix).resolve() != VENV.resolve():
            return subprocess.call([str(target), str(Path(__file__).resolve()), *argv], env=base_env())
    return args.func(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
