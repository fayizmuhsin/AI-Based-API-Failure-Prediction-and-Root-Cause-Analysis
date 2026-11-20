"""Lifecycle-managed background aggregation worker."""

import logging
import threading
from collections.abc import Callable

from app.telemetry.storage import TelemetryRepository, TelemetryStorageError

logger = logging.getLogger(__name__)


class TelemetryWorker:
    def __init__(
        self,
        repository: TelemetryRepository,
        *,
        batch_size: int = 100,
        poll_interval_seconds: float = 0.5,
        post_processor: Callable[[], int] | None = None,
    ):
        self.repository = repository
        self.batch_size = batch_size
        self.poll_interval_seconds = poll_interval_seconds
        self.post_processor = post_processor
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="telemetry-background-worker",
            daemon=True,
        )
        self._thread.start()
        logger.info("Telemetry worker started (batch size %d)", self.batch_size)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("Telemetry worker did not stop within %.1f seconds", timeout)
            else:
                logger.info("Telemetry worker stopped")
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                processed = self.repository.process_batch(self.batch_size)
                detected = 0
                if self.post_processor is not None:
                    try:
                        detected = self.post_processor()
                    except Exception:
                        # Detection failures are isolated from ingestion and aggregation.
                        logger.exception("Anomaly post-processing failed; it will retry on the next poll")
                if processed or detected:
                    if processed:
                        logger.info("Telemetry worker processed %d telemetry records", processed)
                    if detected:
                        logger.info("Anomaly detector evaluated %d completed windows", detected)
                    continue
            except TelemetryStorageError:
                # Failed transactions leave rows pending and the checkpoint unchanged.
                # A delay avoids a hot retry loop; the next poll safely retries them.
                self._stop.wait(self.poll_interval_seconds)
                continue
            self._stop.wait(self.poll_interval_seconds)
