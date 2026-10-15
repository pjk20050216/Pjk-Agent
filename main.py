"""项目启动入口 —— 使用 uvicorn 启动 FastAPI 应用服务器，监听 8000 端口，开启热重载。"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "server.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
