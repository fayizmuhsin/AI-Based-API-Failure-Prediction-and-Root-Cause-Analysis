"""Application-scoped RCA repository and engine."""

import logging

from app.core.config import settings
from app.rca.engine import RootCauseAnalysisEngine
from app.rca.repository import RcaRepository

logger = logging.getLogger(__name__)

rca_repository = RcaRepository(
    settings.monitoring_database_path,
    settings.application_database_path,
)


class RcaServiceRuntime:
    def __init__(self) -> None:
        self._engine: RootCauseAnalysisEngine | None = None

    def initialize(self) -> None:
        rca_repository.initialize()
        self._engine = RootCauseAnalysisEngine(rca_repository, settings)

    def process_batch(self) -> int:
        if self._engine is None:
            self.initialize()
        return self._engine.process_batch()


rca_service = RcaServiceRuntime()
