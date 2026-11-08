"""应用入口：uvicorn main:app"""

import uvicorn

from app.api import app  # noqa: F401  （供 uvicorn 以 main:app 加载）
from app.config import app_config

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=str(app_config.server.host),
        port=int(app_config.server.port),
        reload=False,
    )
