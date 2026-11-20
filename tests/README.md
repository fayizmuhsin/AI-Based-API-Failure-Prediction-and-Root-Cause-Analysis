# Validation

Run the Phase 2 integration tests from the project root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The Phase 2 suite starts the four services independently on temporary local ports and a temporary SQLite file under `data/simulated/`. It covers service health and topology, Gateway routes, Order-to-User calls, simulated database reads/writes, unknown-user handling, and monitoring database isolation.

The Phase 3 suite starts a local monitoring backend and all four services. It covers valid/invalid ingestion, service metrics/traces/logs, HTTP failures, trace propagation, best-effort delivery failure, non-recursive ingestion, and database separation. The Phase 4 suite uses temporary SQLite files to test schema creation, persistence, idempotent retries, processing/aggregation, transaction rollback, checkpoints, restart recovery, and concurrent workers. The Phase 5 suite uses deterministic telemetry and temporary databases for feature extraction, threshold evidence, model readiness/training/scoring, anomaly persistence, API responses, restart behavior, and duplicate prevention. Run the same command to execute all suites.

## Phase 8 fault and evaluation testing

Phase 8 tests use temporary state and local-only processes:

- `test_phase8_fault_simulation.py` verifies approved targets and modes, bounded process-local leases, automatic expiry, loopback-only control, scenario orchestration, rollback, and isolated database behavior.
- `test_phase8_evaluation.py` runs the real monitoring backend, four simulated services, and fault simulator on temporary loopback ports and temporary SQLite databases.
- `test_phase8_evaluation_metrics.py` uses deterministic fixtures for detector tri-state values, combined detection, duplicate anomaly IDs, RCA status/outcome separation, leakage checks, recovery metrics, JSON artifacts, and run IDs.

All scenario and subprocess state is temporary. The evaluation harness does not open or modify existing application or monitoring databases. The isolated model is trained only in a temporary monitoring database. Existing project databases and trained models are not overwritten.

Run the isolated end-to-end harness and metrics tests:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_phase8_evaluation*.py" -v
```

Run all Phase 8 tests:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_phase8_*.py" -v
```

Run the complete backend regression suite:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Run Python compilation:

```powershell
.\.venv\Scripts\python.exe -m compileall -q backend services tests
```

Evaluation artifacts are written only under `evaluation_output/phase8/{run_id}/`. Unique UUID run directories prevent an evaluation from overwriting an earlier run. Expected scenario labels are post-hoc reporting metadata and must not appear in telemetry, service logs, API responses, or RCA inputs.

The latest final-regression run is `2f3ca062-d460-4d1c-ae4b-b3aaef2fc8c7`. Its measured summary is available at `evaluation_output/phase8/2f3ca062-d460-4d1c-ae4b-b3aaef2fc8c7/summary.md`. The preserved run `552cf781-7efd-4918-a81c-94105afbc153` is historical and has its own run-specific measurements.
