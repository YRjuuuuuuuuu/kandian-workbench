import argparse
import json
import sys
from .engine import Engine
from .server import make_server


def main():
    if sys.version_info < (3, 11):
        raise SystemExit("需要 Python 3.11 或以上；请使用 CONTENT_ENGINE_PYTHON 指定合适的 Python")
    parser = argparse.ArgumentParser(description="看点工场 · 本地内容工作台")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "init", "check-modes", "tick"])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir")
    parser.add_argument("--modes-dir")
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    engine = Engine(args.data_dir, args.modes_dir)
    if args.demo:
        engine.seed_demo()
    if args.command == "check-modes":
        print(json.dumps({"modes": list(engine.modes.items), "valid": True}, ensure_ascii=False))
    elif args.command == "tick":
        engine.tick()
        print("任务循环已执行；本地排期只产生待办，不操作平台")
    elif args.command == "serve":
        server = make_server(engine, args.port)
        engine.start_worker()
        print(f"看点工场已启动：http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            engine.stop.set()
            server.server_close()
    else:
        print("数据目录已初始化：", engine.data_dir)


if __name__ == "__main__":
    main()
