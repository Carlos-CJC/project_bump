"""Offline presentation entrypoint. Changes neither system Python nor networking."""
import argparse
import asyncio
import json
from pathlib import Path
import socket
import sys


ROOT = Path(__file__).resolve().parent


def unused_port(preferred, disallow=()):
    if not 1024 <= preferred <= 65535:
        raise ValueError("port must be between 1024 and 65535")
    for choice in (preferred, 0, 0, 0, 0):
        if choice in disallow:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", choice))
            except OSError:
                continue
            port = sock.getsockname()[1]
            if port not in disallow:
                return port
    raise RuntimeError("no free loopback port found")


def main():
    parser = argparse.ArgumentParser(description="Bump offline local presentation")
    parser.add_argument("--runtime", type=Path, default=ROOT / ".runtime")
    parser.add_argument("--web-port", type=int, default=8765)
    parser.add_argument("--mqtt-port", type=int, default=18883)
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    from bump.config import init_runtime, load_settings
    from bump.__main__ import serve
    from bump.api import create_app
    from bump.broker import make_broker
    from bump.worker import Worker
    import amqtt
    import fastapi
    import sqlite3
    import uvicorn
    import ssl
    if args.self_check:
        print(json.dumps({"ok": True, "python": sys.version.split()[0],
                          "sqlite": sqlite3.sqlite_version, "tls": ssl.OPENSSL_VERSION,
                          "fastapi": fastapi.__version__, "mqtt": amqtt.__version__,
                          "uvicorn": uvicorn.__version__,
                          "runtime_inside_project": Path(sys.executable).resolve().is_relative_to(ROOT)},
                         ensure_ascii=True))
        return 0
    runtime = args.runtime.resolve()
    if (runtime / "config.json").exists():
        settings = load_settings(runtime)
    else:
        # A running legacy project may already use the recommended pair. On
        # first launch only, choose unoccupied loopback ports and persist them.
        web = unused_port(args.web_port)
        mqtt = unused_port(args.mqtt_port, (web,))
        settings = init_runtime(runtime, web, mqtt)
    # Never silently overwrite an existing demo database/config if its ports
    # are occupied. The original Bump instance may already be open.
    for port in (settings["web_port"], settings["mqtt"]["port"]):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                raise RuntimeError(f"loopback port {port} is busy; use the existing Bump window "
                                   "or close the conflicting service before restarting") from None
    print("Keep this window open. Press Ctrl+C to stop.", flush=True)
    if not args.no_browser:
        import webbrowser
        show = webbrowser.open
    else:
        show = None
    asyncio.run(serve(settings, on_ready=show))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Bump could not start: {exc}", file=sys.stderr)
        raise SystemExit(1)
