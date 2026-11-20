import logging
import os

import uvicorn
from fastapi import HTTPException
from pydantic import BaseModel, Field

from services.shared.app import create_service_app
from services.shared.config import service_port, service_url
from services.shared.http_client import DownstreamError, path_segment, request_json

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s - %(message)s")
app = create_service_app("order-service")
user_url = service_url("USER_SERVICE_URL", "user-service")
database_url = service_url("DATABASE_SERVICE_URL", "database-service")


class OrderCreate(BaseModel):
    user_id: str = Field(min_length=1, max_length=100)
    item: str = Field(min_length=1, max_length=200)
    quantity: int = Field(gt=0, le=1000)


def downstream_call(
    method: str, url: str, payload: dict | None = None, *, dependency: str, route: str
) -> dict:
    try:
        return request_json(method, url, payload, dependency=dependency, route=route)
    except DownstreamError as exc:
        status_code = exc.status_code if exc.status_code in {400, 404, 409, 422, 503} else 502
        raise HTTPException(status_code=status_code, detail=exc.detail) from exc


@app.post("/orders", status_code=201, tags=["orders"])
def create_order(order: OrderCreate) -> dict:
    user = downstream_call(
        "GET", f"{user_url}/users/{path_segment(order.user_id)}",
        dependency="user-service", route="/users/{user_id}",
    )
    saved_order = downstream_call(
        "POST",
        f"{database_url}/internal/orders",
        order.model_dump(),
        dependency="database-service",
        route="/internal/orders",
    )
    return {"order": saved_order, "user": user}


@app.get("/orders/{order_id}", tags=["orders"])
def read_order(order_id: int) -> dict:
    order = downstream_call(
        "GET", f"{database_url}/internal/orders/{order_id}",
        dependency="database-service", route="/internal/orders/{order_id}",
    )
    user = downstream_call(
        "GET", f"{user_url}/users/{path_segment(order['user_id'])}",
        dependency="user-service", route="/users/{user_id}",
    )
    return {"order": order, "user": user}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=service_port("order-service"), reload=False)
