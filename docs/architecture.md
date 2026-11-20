# Local process layout - Phases 1 through 6

The Phase 1 FastAPI platform backend and Vite development server run on ports 8000 and 5173. Phase 2 adds four independent FastAPI processes on ports 8100–8103: API Gateway (8100), User Service (8101), Order Service (8102), and Database Service (8103).

Each service reads its approved name and caller-to-dependency relationships from `config/services.json` and verifies the configured topology. Gateway calls User and Order; Order calls User; User and Order call Database Service. Service-to-service calls use HTTP with a 2.5-second timeout.

Only Database Service opens the simulated application SQLite file at `data/simulated/application.db`. It requires the resolved path to remain under `data/simulated/` and differ from the platform monitoring database path at `data/monitoring/monitoring.db`. The platform backend and monitoring database are not used for simulated service persistence.

Database Service seeds two sample users idempotently at startup and persists orders through its internal HTTP endpoints.

## Phase 3 telemetry flow

Each simulated process instruments FastAPI server requests with the OpenTelemetry SDK and FastAPI instrumentation. An outer request middleware records route-template metrics and structured request logs. The shared service HTTP client creates OpenTelemetry client spans and injects W3C `traceparent` headers, preserving caller-to-dependency trace relationships.

Completed spans are serialized by a custom SDK span exporter. Metric observations, structured logs, and spans enter a bounded per-process queue; a background sender batches them to `POST /api/telemetry/ingest` with a sub-second network timeout. Queue saturation or delivery failure drops telemetry without blocking application routing. Ingestion is not instrumented, so it cannot recursively generate telemetry.

The monitoring API validates the complete batch, canonicalizes approved service names and timestamps, and commits records to `data/monitoring/monitoring.db` before returning HTTP 202. Records use deterministic SHA-256 event IDs for idempotent retries. A storage failure returns HTTP 503 and does not acknowledge successful ingestion. The database is separate from `data/simulated/application.db` and monitoring code does not open the simulated data file.

The monitoring schema has three tables: `telemetry_records` stores metrics, spans, and structured logs with their validated JSON payload and queryable service/time/trace fields; `telemetry_windows` stores one-minute record/failure/latency aggregates; `processing_checkpoints` tracks durable worker progress. Indexes support service/time, timestamp, trace ID, and pending-processing queries.

At backend startup, a lifecycle-managed worker reads pending records in bounded batches of 100. It updates aggregates, marks each record processed, and advances its checkpoint in a single `BEGIN IMMEDIATE` SQLite transaction. SQLite serializes competing writer transactions, so two workers cannot claim the same pending batch. If aggregation fails, the transaction rolls back, the checkpoint does not advance, and records remain pending for retry or recovery after restart. The worker uses a short polling wait when idle and stops during application shutdown.

`GET /api/telemetry/recent` and `GET /api/telemetry/summary` now read persistent storage. The monitoring API does not instrument its own ingestion endpoints.

## Phase 5 anomaly detection

The Phase 4 worker calls an anomaly post-processor after bounded telemetry batches. The detector selects only processed server-request windows whose UTC minute has closed and which have no existing detection run. A unique service/window constraint and a transaction containing the detection run, optional anomaly result, and checkpoint prevent duplicate results after retries or restarts. Detection errors are logged and retried on later worker polls without stopping telemetry ingestion.

Pandas and NumPy derive request, success/failure, error-rate, timeout, average/median/p95 latency, latency-baseline deviation, request-count change, and observed dependency-failure features from persisted metric records and prior telemetry_windows. Incomplete baselines and unavailable dependency observations remain missing; empty request windows are not flagged. Configurable threshold rules retain exact observed values and limits as evidence.

Isolation Forest is implemented with Python standard-library code: seeded random subsampling, random feature/split isolation trees, expected unsuccessful-search path-length correction, and the standard normalized anomaly score. Missing feature values use per-feature median imputation; constant features are excluded from splits. The model scores windows only after explicit training from threshold-clean observed history that excludes previously detected anomalies. The default minimum training set is 30 windows; until sufficient healthy history exists, status is not_ready and threshold rules continue to run. Training metadata and a versioned JSON model payload are stored in model_metadata in the monitoring SQLite database. Scores are relative anomaly scores in the standard 0–1 direction (higher means more anomalous), not failure probabilities. Scikit-learn is not required.

Phase 5 adds detection_runs, anomaly_results, model_metadata, and anomaly_checkpoints. Read-only API endpoints expose model state, recent anomalies, service summaries, and individual feature/rule evidence.

## Phase 6 root cause analysis

The telemetry worker runs anomaly detection, then invokes RCA in a separately guarded post-processing step. RCA failures are logged and retried on later polls; they do not stop ingestion or telemetry aggregation. `rca_queue` durably tracks pending/completed anomaly triggers, attempt counts, and retry errors. Pending items remain pending across process restarts. An investigation and its unique anomaly-trigger links are committed in one SQLite transaction. A unique idempotency key and a unique trigger anomaly constraint prevent duplicate reports.

The RCA engine reads only the platform monitoring database and `config/services.json`. It groups nearby detected anomalies into episodes and loads telemetry from a bounded time window. Metrics provide server error, timeout, latency, and caller-to-dependency failure signals. Structured logs contribute error, health-check, restart, and deployment markers. Trace spans contribute error/exception evidence; shared trace IDs and parent span IDs connect evidence across service boundaries. The engine does not read simulator expected-cause labels.

Dependency edges retain caller -> dependency meaning. For a candidate failed service, reverse traversal finds callers that may be affected; the failed dependency is distinguished from those callers' secondary symptoms. Candidate ranking combines observed-signal points, affected anomalous service coverage, topology impact, earlier onset, and corroboration from distinct signal channels. Scores are capped at 100 ranking points and are not failure probabilities. A candidate is selected as probable only when it meets the configured minimum score and has supporting observed telemetry beyond the triggering anomaly. Otherwise the investigation is stored as inconclusive.

Phase 6 adds `rca_queue`, `investigations`, and `investigation_triggers` to the existing monitoring SQLite database. Investigation JSON stores triggering anomaly IDs, candidates and ranking breakdowns, evidence references, affected services, explanations, conclusion, and methodology. Endpoints are `GET /api/rca/recent`, `GET /api/rca/summary`, `GET /api/rca/{investigation_id}`, and `GET /api/rca/{investigation_id}/candidates`. No frontend changes or new runtime dependencies are introduced.
