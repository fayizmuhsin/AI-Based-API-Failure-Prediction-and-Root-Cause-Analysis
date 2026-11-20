import logging
import os
import sqlite3

import uvicorn
from fastapi import HTTPException
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from services.database.storage import (
    SimulatedDatabaseUnavailable,
    check_database,
    create_order,
    get_order,
    get_user,
    initialize_database,
)
from services.shared.app import create_service_app
from services.shared.config import service_port

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s - %(message)s")
app = create_service_app("database-service", startup=initialize_database, health_check=check_database)


@app.exception_handler(SimulatedDatabaseUnavailable)
async def simulated_database_unavailable(
    request: Request, exc: SimulatedDatabaseUnavailable,
) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc), "status": "error"})


class OrderRecordCreate(BaseModel):
    user_id: str = Field(min_length=1, max_length=100)
    item: str = Field(min_length=1, max_length=200)
    quantity: int = Field(gt=0, le=1000)


@app.get("/internal/users/{user_id}", tags=["simulated data"])
def read_user(user_id: str) -> dict:
    user = get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail=f"User '{user_id}' was not found")
    return user


@app.post("/internal/orders", status_code=201, tags=["simulated data"])
def save_order(order: OrderRecordCreate) -> dict:
    try:
        return create_order(order.user_id, order.item, order.quantity)
    except sqlite3.IntegrityError as exc:
        if "FOREIGN KEY constraint failed" in str(exc):
            raise HTTPException(status_code=404, detail=f"User '{order.user_id}' was not found") from exc
        raise


@app.get("/internal/orders/{order_id}", tags=["simulated data"])
def read_order(order_id: int) -> dict:
    order = get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"Order '{order_id}' was not found")
    return order


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=service_port("database-service"), reload=False)
