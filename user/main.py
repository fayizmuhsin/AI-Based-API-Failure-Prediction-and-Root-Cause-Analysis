import logging
import os

import uvicorn
from fastapi import HTTPException

from services.shared.app import create_service_app
from services.shared.config import service_port, service_url
from services.shared.http_client import DownstreamError, path_segment, request_json

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s - %(message)s")
app = create_service_app("user-service")
database_url = service_url("DATABASE_SERVICE_URL", "database-service")


@app.get("/users/{user_id}", tags=["users"])
def read_user(user_id: str) -> dict:
    try:
        return request_json(
            "GET", f"{database_url}/internal/users/{path_segment(user_id)}",
            dependency="database-service", route="/internal/users/{user_id}",
        )
    except DownstreamError as exc:
        status_code = exc.status_code if exc.status_code in {404, 503} else 502
        raise HTTPException(status_code=status_code, detail=exc.detail) from exc


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=service_port("user-service"), reload=False)
