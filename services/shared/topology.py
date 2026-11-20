import json

from services.shared.config import settings


EXPECTED_SERVICES = {
    "api-gateway": "API Gateway",
    "user-service": "User Service",
    "order-service": "Order Service",
    "database-service": "Database Service",
}
EXPECTED_DEPENDENCIES = {
    ("api-gateway", "user-service"),
    ("api-gateway", "order-service"),
    ("order-service", "user-service"),
    ("user-service", "database-service"),
    ("order-service", "database-service"),
}


def load_topology() -> dict[str, object]:
    """Read and validate the approved service names and caller-to-dependency edges."""
    try:
        raw = json.loads(settings.service_config_path.read_text(encoding="utf-8"))
        service_names = {item["id"]: item["name"] for item in raw["services"]}
        edges = {
            (item["caller"], item["dependency"])
            for item in raw["dependencies"]
        }
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Invalid service topology configuration: {exc}") from exc

    if service_names != EXPECTED_SERVICES:
        raise RuntimeError("config/services.json must contain the four approved service IDs and names")
    if edges != EXPECTED_DEPENDENCIES:
        raise RuntimeError("config/services.json dependencies do not match the approved topology")
    return raw


def service_details(service_id: str) -> dict[str, object]:
    topology = load_topology()
    service = next(item for item in topology["services"] if item["id"] == service_id)
    dependencies = [
        edge["dependency"]
        for edge in topology["dependencies"]
        if edge["caller"] == service_id
    ]
    return {"id": service["id"], "name": service["name"], "dependencies": dependencies}
