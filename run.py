"""DevFlow AI 一键启动 —— 同时运行网关（8000 外网）和后端（8001 内网）。"""
import multiprocessing
import signal
import sys
import uvicorn


def run_backend():
    uvicorn.run(
        "server.main:app",
        host="127.0.0.1",
        port=8001,
        log_level="info",
        access_log=False,
    )


def run_gateway():
    uvicorn.run(
        "gateway.main:app",
        host="0.0.0.0",
        port=8000,
        log_level="info",
        access_log=True,
    )


if __name__ == "__main__":
    multiprocessing.freeze_support()

    print("=" * 50)
    print("  DevFlow AI")
    print(f"  Gateway : http://0.0.0.0:8000  (public)")
    print(f"  Backend : http://127.0.0.1:8001 (internal)")
    print(f"  Docs    : http://localhost:8000/docs")
    print("=" * 50)

    backend = multiprocessing.Process(target=run_backend, name="devflow-backend")
    gateway = multiprocessing.Process(target=run_gateway, name="devflow-gateway")

    backend.start()
    gateway.start()

    def shutdown(signum, frame):
        print("\nShutting down...")
        backend.terminate()
        gateway.terminate()
        backend.join(timeout=5)
        gateway.join(timeout=5)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    backend.join()
    gateway.join()
