"""Run from the project folder: python -m bump init / serve / simulate."""

import argparse
import asyncio
import json
import logging
import sys

from .config import DEFAULT_RUNTIME, init_runtime, load_settings


async def serve(settings, *, on_ready=None):
    import uvicorn
    from .api import create_app
    from .broker import make_broker
    from .worker import Worker
    from .v1_transport import V1Transport

    app = create_app(settings)
    worker = Worker(settings, store=app.state.store)
    v1 = V1Transport(settings, app.state.v1_store)
    broker = make_broker(settings)
    await broker.start()
    try:
        if not await asyncio.to_thread(worker.start, 8):
            raise RuntimeError("MQTT worker did not become ready.")
        try:
            if await asyncio.to_thread(v1.start, 8):
                app.state.v1_transport = v1
            else:
                logging.warning("v1 compatibility receiver is unavailable; original demo is still available.")
        except (ValueError, OSError, RuntimeError):
            logging.warning("v1 compatibility receiver could not start; original demo is still available.")
        url = f"http://127.0.0.1:{settings['web_port']}"
        print(f"\nBump local demo: {url}", flush=True)
        print(f"Login passwords: {settings['runtime_dir']}/credentials.txt", flush=True)
        print("Sign in, then use the simulated discovery button. Ctrl+C stops this server.\n", flush=True)
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=settings["web_port"], log_level="warning",
            access_log=False,
        ))
        async def announce_after_bind():
            # Uvicorn sets started only after the real HTTP socket is listening.
            while not server.started and not server.should_exit:
                await asyncio.sleep(0.10)
            if server.started:
                try:
                    await asyncio.to_thread(on_ready, url)
                except Exception:
                    print(f"Open {url} in your browser.", flush=True)

        announce = asyncio.create_task(announce_after_bind()) if on_ready else None
        try:
            await server.serve()
        finally:
            if announce is not None:
                announce.cancel()
                await asyncio.gather(announce, return_exceptions=True)
    finally:
        app.state.v1_transport = None
        try:
            await asyncio.to_thread(v1.stop)
        finally:
            try:
                await asyncio.to_thread(worker.stop)
            finally:
                await broker.shutdown()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bump local, hardware-free prototype")
    parser.add_argument("--runtime", default=str(DEFAULT_RUNTIME),
                        help="Private data directory (default: .runtime next to this project)")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Generate local passwords and TLS certificates once")
    init.add_argument("--web-port", type=int, default=8765)
    init.add_argument("--mqtt-port", type=int, default=18883)
    commands.add_parser("serve", help="Start MQTT broker, storage worker and local website")
    simulate = commands.add_parser("simulate", help="Send two synthetic encounters through MQTT")
    simulate.add_argument("--scenario", choices=("normal", "offline", "retry"), default="normal")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        if args.command == "init":
            settings = init_runtime(args.runtime, args.web_port, args.mqtt_port)
            print(f"Runtime ready: {settings['runtime_dir']}")
            print("Demo passwords are in credentials.txt. Do not share the runtime directory.")
        elif args.command == "serve":
            asyncio.run(serve(load_settings(args.runtime)))
        else:
            from .simulator import run_demo
            result = run_demo(load_settings(args.runtime), args.scenario)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 1
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Bump could not complete: {error}", file=sys.stderr)
        if isinstance(error, FileNotFoundError):
            print("Run 'python -m bump init' first.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
