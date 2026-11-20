import logging
import os

import uvicorn
from fastapi import HTTPException
from pydantic import BaseModel, Field

from services.shared.app import create_service_app
from services.shared.config import service_port, service_url
from services.shared.http_client import DownstreamError, path_segment, request_json

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s - %(message)s")
app = create_service_app("api-gateway")
user_url = service_url("USER_SERVICE_URL", "user-service")
order_url = service_url("ORDER_SERVICE_URL", "order-service")


class OrderCreate(BaseModel):
    user_id: str = Field(min_length=1, max_length=100)
    item: str = Field(min_length=1, max_length=200)
    quantity: int = Field(gt=0, le=1000)


def forward(method: str, url: str, payload: dict | None = None, *, dependency: str, route: str) -> dict:
    try:
        return request_json(method, url, payload, dependency=dependency, route=route)
    except DownstreamError as exc:
        status_code = exc.status_code if exc.status_code in {400, 404, 409, 422, 503} else 502
        raise HTTPException(status_code=status_code, detail=exc.detail) from exc


@app.get("/api/users/{user_id}", tags=["gateway routes"])
def get_user(user_id: str) -> dict:
    return forward(
        "GET", f"{user_url}/users/{path_segment(user_id)}",
        dependency="user-service", route="/users/{user_id}",
    )


@app.post("/api/orders", status_code=201, tags=["gateway routes"])
def post_order(order: OrderCreate) -> dict:
    return forward(
        "POST", f"{order_url}/orders", order.model_dump(),
        dependency="order-service", route="/orders",
    )


@app.get("/api/orders/{order_id}", tags=["gateway routes"])
def get_order(order_id: int) -> dict:
    return forward(
        "GET", f"{order_url}/orders/{order_id}",
        dependency="order-service", route="/orders/{order_id}",
    )


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=service_port("api-gateway"), reload=False)
