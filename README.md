# AI-Based API Failure Prediction and Root Cause Analysis

A local-first project for simulating API services, collecting telemetry, detecting anomalies, and generating explainable root cause investigations. Phases 1 through 6 provide the FastAPI platform, React health view, four simulated services, OpenTelemetry, persistent monitoring storage, pure-Python Isolation Forest anomaly detection, and durable RCA.

## Project objectives

The project provides a local, reproducible environment for:

- simulating a small microservice application and its dependencies;
- collecting HTTP metrics, trace spans, and structured logs;
- persisting and processing telemetry independently of simulated application data;
- detecting anomalous service behavior with model and rule-based mechanisms;
- ranking plausible RCA candidates from observed evidence;
- exercising bounded service faults and reporting measured evaluation outcomes.

The platform is for local engineering evaluation. It is not a proof of production readiness or a source of causal conclusions.

## System architecture

The platform is split into a monitoring backend, four simulated microservices, a fault controller, an evaluation harness, and a React dashboard:

```text
React/Vite dashboard
        |
        v
FastAPI monitoring backend :8000
  - telemetry ingestion and read-only APIs
  - one-minute window aggregation
  - anomaly detection and RCA workers
  - SQLite monitoring database

API Gateway :8100 -> User Service :8101 -> Database Service :8103
                 \-> Order Service :8102  -> Database Service :8103
                                      \-> User Service :8101

Fault Simulator :8110 -> loopback-only service fault controls
```

API Gateway is the public application entry point. Order Service verifies users through User Service and persists orders through Database Service. User Service reads users through Database Service. Database Service owns the separate simulated application database.

## Technology stack

- Backend: Python, FastAPI, Uvicorn, Pydantic Settings
- Simulated services: four independent FastAPI/Uvicorn local processes
- Telemetry: OpenTelemetry API/SDK and FastAPI instrumentation
- Frontend: React, Vite, Tailwind CSS
- Anomaly analysis: Pure-Python Isolation Forest, Pandas, NumPy, and configurable threshold rules
- Root cause analysis: observed metrics, structured logs, trace spans, service topology, and SQLite
- Phase 4 telemetry storage: SQLite monitoring database with durable processing state
- SQLite paths: simulated application data and platform monitoring data remain separate

## Prerequisites

- Windows 10 or 11
- Python 3.11 or newer
- Node.js 22.12 or newer; use a supported LTS release (includes npm)
- PowerShell

No cloud account, Docker, or paid service is required.

## Windows installation

Open PowerShell in the project directory. Create and activate a virtual environment, then install backend dependencies:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r backend\requirements.txt
```

If PowerShell blocks activation, use `\.venv\Scripts\python.exe` directly in the commands below. Optionally copy `.env.example` to `.env` to override local settings. Defaults point to separate simulated application and platform monitoring database files.

Install frontend dependencies in a second PowerShell window:

```powershell
Set-Location frontend
npm install
```

## Start the backend

From the project root, with the virtual environment active:

```powershell
uvicorn app.main:app --app-dir backend --reload --host 127.0.0.1 --port 8000
```

Interactive API documentation is available at `http://127.0.0.1:8000/docs`.

## Start the frontend

In another PowerShell window:

```powershell
Set-Location frontend
npm run dev
```

Open the local URL printed by Vite (normally `http://127.0.0.1:5173`). The page calls the backend health endpoint and shows loading, success, or unavailable states. To use a different backend URL, set `VITE_API_BASE_URL` before starting Vite; its value should be the API base including `/api`.

## Check the health endpoint

With the backend running:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
```

The JSON response includes application status, name, API version, environment, timestamp, and an API availability check.

## Phase 2 simulated services

The Phase 1 platform backend and frontend retain ports 8000 and 5173. Each simulated service runs as a separate process in its own PowerShell window. Start Database Service first, followed by User Service, Order Service, and API Gateway:

```powershell
# Run each command in a separate PowerShell window from the project root.
.\.venv\Scripts\python.exe -m services.database.main
.\.venv\Scripts\python.exe -m services.user.main
.\.venv\Scripts\python.exe -m services.order.main
.\.venv\Scripts\python.exe -m services.gateway.main
```

| Process | Port | Health endpoint | Operations |
|---|---:|---|---|
| Platform backend | 8000 | `/api/health` | Phase 1 platform health |
| React/Vite frontend | 5173 | `/` | Phase 1 health page |
| API Gateway | 8100 | `/health` | `GET /api/users/{id}`, `POST /api/orders`, `GET /api/orders/{id}` |
| User Service | 8101 | `/health` | `GET /users/{id}` |
| Order Service | 8102 | `/health` | `POST /orders`, `GET /orders/{id}` |
| Database Service | 8103 | `/health` | `GET /internal/users/{id}`, `POST /internal/orders`, `GET /internal/orders/{id}` |

Service names and caller-to-dependency edges are read and checked against `config/services.json`. Service HTTP calls use a 2.5-second timeout. Ports and downstream URLs can be overridden through the variables in `.env.example`.

### Try the complete request flow

With all four services running, read a sample user through the gateway:

```powershell
Invoke-RestMethod http://127.0.0.1:8100/api/users/user-1001
```

Create and retrieve an order through API Gateway. Order Service verifies the user through User Service before it persists the order through Database Service:

```powershell
$order = Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8100/api/orders `
  -ContentType 'application/json' `
  -Body (@{ user_id = 'user-1001'; item = 'Campus notebook'; quantity = 2 } | ConvertTo-Json)
$order
Invoke-RestMethod "http://127.0.0.1:8100/api/orders/$($order.order.id)"
```

Database Service inserts sample users `user-1001` and `user-1002` idempotently at startup. Orders persist in `data/simulated/application.db`. Simulated service code only opens the configured application database path, requires it to remain under `data/simulated/`, and rejects a path equal to the platform monitoring database at `data/monitoring/monitoring.db`.

Run the Phase 2 integration tests from the project root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The Phase 2 regression tests launch all four services as separate local processes on temporary ports and use a temporary database under `data/simulated/`.

## Phase 3 monitoring and telemetry

Start the monitoring backend in its own PowerShell window from the project root:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

Start the four simulated services in four additional PowerShell windows, using the Phase 2 commands above. The Database Service should start before User Service and Order Service; the monitoring backend can start independently. The Vite frontend remains on port 5173 and its Phase 1 health behavior is unchanged.

All four simulated services send validated batches to `POST /api/telemetry/ingest`. The endpoint accepts metric observations, trace spans, and structured request logs. Development inspection endpoints are read-only:

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/api/telemetry/recent?limit=50'
Invoke-RestMethod 'http://127.0.0.1:8000/api/telemetry/summary'
```

Recent records include normalized service ID/name, UTC event and receive timestamps, stable event IDs, and processing status. The summary reads persisted records and groups server and client observations by service, route, method, and dependency, with status counts, success/failure totals, error rate, timeout count, average/min/max latency, and p50/p95 latency. Repeated identical records are idempotent: the endpoint acknowledges the submitted count and reports how many were duplicates.

Each service uses a bounded local queue and a background sender with a short timeout. If the monitoring API is unavailable, telemetry records may be dropped and counted locally; application requests continue. After the backend receives a batch, it validates and commits it to `data/monitoring/monitoring.db` before returning HTTP 202. Storage failures return HTTP 503 instead of acknowledging the batch. The monitoring backend does not instrument its own ingestion endpoints and never opens the simulated application database.

## Phase 4 persistent storage and processing

The monitoring database is created automatically at startup. It contains `telemetry_records` for metric observations, spans, and structured logs; `telemetry_windows` for one-minute aggregation results; and `processing_checkpoints` for durable progress. `telemetry_records` includes indexes for service/time, event time, trace ID, and processing status. A deterministic SHA-256 event ID makes retries idempotent.

The backend starts a lifecycle-managed worker that reads pending records in batches of 100. It aggregates counts, failures, and latency by service, UTC minute, record type, and metric name. Aggregates, processed statuses, and the checkpoint advance in one `BEGIN IMMEDIATE` transaction. SQLite serializes competing worker transactions; if a batch fails, the transaction rolls back, records remain pending, and a later poll retries them. Pending records survive process restarts.

## Phase 5 explainable anomaly detection

The existing worker first persists telemetry windows, then checks for fully closed UTC minute windows. Detection is bounded by APP_ANOMALY_DETECTION_BATCH_SIZE and uses persisted request metrics plus the Phase 4 time-window aggregates. Partial windows are held until they are at least APP_ANOMALY_WINDOW_LATENESS_SECONDS old.

Features include request, success, failure, and timeout counts; error rate; average, median, and p95 latency; deviation from prior service latency; change from the prior request count; and observed downstream failure rate. Missing latency history stays missing through extraction and is median-imputed only inside the trained model. Empty request windows are not produced as anomalies.

Threshold rules run whether or not a model is trained: high error rate, high p95 latency, repeated timeouts, a latency increase against at least three historical windows, observed dependency failures, and a large traffic change against a sufficiently active preceding window. Each result stores the exact feature values and matching rule evidence.

Isolation Forest is trained only on observed, completed windows that pass the threshold health rules and are not already known anomalies. Training is explicit and controlled; the system does not train on every incoming record or create fake baseline data. The default minimum is 30 healthy service windows. Until it is trained, the model status is not_ready and threshold detections continue.

From the project root, run the training command after enough healthy telemetry has accumulated:

~~~powershell
Set-Location backend
..\.venv\Scripts\python.exe -m app.anomaly.train
Set-Location ..
~~~

The model is stored as a versioned JSON payload in the monitoring SQLite database with its training metadata. The public API does not return model bytes or database paths. Isolation Forest scores are not probabilities; higher model scores indicate more unusual patterns, and the model decision is recorded separately from threshold matches.

Read-only endpoints:

~~~powershell
Invoke-RestMethod 'http://127.0.0.1:8000/api/anomalies/model'
Invoke-RestMethod 'http://127.0.0.1:8000/api/anomalies/recent?limit=50'
Invoke-RestMethod 'http://127.0.0.1:8000/api/anomalies/summary'
Invoke-RestMethod 'http://127.0.0.1:8000/api/anomalies/1'
~~~

Thresholds, contamination, random state, tree count, sample size, minimum history, and batch sizing can be overridden with the APP_ANOMALY_* settings shown in .env.example.

## Phase 6 root cause analysis

After each anomaly-processing pass, the background worker queues new anomaly results for RCA. The engine groups anomalies in a configurable time episode, gathers nearby persisted metrics, logs, trace spans, and service events, then ranks candidate causes using evidence strength, timing, severity, dependency failures, and affected upstream callers. Dependency arrows are caller -> dependency; impact analysis traverses reverse edges. Simulator scenario labels are not read by RCA. Each result is stored transactionally in the monitoring database, including trigger anomaly IDs, ranked candidates, evidence references, score explanations, and affected services. Inconclusive investigations are retained when no candidate meets the evidence threshold.

The queue and unique anomaly-to-investigation links make retries and process restarts idempotent. RCA errors are logged and retried by later worker polls without blocking telemetry ingestion. RCA only opens the platform monitoring database; simulated application data remains separate.

With the backend running on port 8000, inspect investigations using:

~~~powershell
Invoke-RestMethod 'http://127.0.0.1:8000/api/rca/recent?limit=50'
Invoke-RestMethod 'http://127.0.0.1:8000/api/rca/summary'
# Replace the ID with an investigation_id returned by /recent.
Invoke-RestMethod 'http://127.0.0.1:8000/api/rca/<investigation-id>'
Invoke-RestMethod 'http://127.0.0.1:8000/api/rca/<investigation-id>/candidates'
~~~

Run the complete Phase 1–6 regression suite from the project root:

~~~powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
~~~


## Phase 7 monitoring dashboard

The React dashboard provides Overview, Services, Telemetry, Anomalies, Root Cause Analysis, and Incidents pages. It reads the existing monitoring REST APIs on port 8000 and checks each simulated service directly on ports 8100, 8101, 8102, and 8103. Start the backend and all four simulated services using the commands above, then start the frontend:

```powershell
Set-Location frontend
npm run dev
```

Open `http://127.0.0.1:5173`. Dashboard data refreshes every five seconds; each source reports its own unavailable state if a service cannot be reached. The dependency diagram uses caller-to-dependency arrows. The dashboard uses native SVG charts and graph rendering, so Phase 7 adds no frontend packages.

To make a production build:

```powershell
npm run build
```

## Complete regression and compilation

Run the complete Python regression suite from the project root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Compile the backend, services, and tests:

```powershell
.\.venv\Scripts\python.exe -m compileall -q backend services tests
```

## Known limitations and future improvements

- The evaluation is a bounded local workload, not a production benchmark.
- The custom Isolation Forest and threshold rules can produce different observations for the same window; a threshold match or model result is not itself RCA proof.
- Detection latency includes window aggregation, processing, and persistence timing.
- Recovery time starts at explicit fault stop and ends at successful verification, so it is an upper bound.
- RCA candidates are ranked by observed evidence and remain ranking points rather than probabilities.
- Expected labels are post-hoc metadata, not ground truth supplied to detection or RCA.
- The isolated harness depends on bounded waits and real subprocess timing; unavailable or malformed observations must remain unavailable rather than be treated as negative detections.
- Statistical metrics such as accuracy, precision, recall, and F1 are not available until a validated labeled statistical evaluation dataset exists.
- Future work could add longer representative workloads, historical repeat runs, confidence intervals, additional telemetry sources, and explicit RCA causal review, but no such evidence is claimed here.

## Phase 8 fault simulation

Phase 8 adds a loopback-only fault control plane and an isolated end-to-end evaluation harness. Fault state is held in process-local, automatically expiring leases owned by `FaultLeaseManager`; it is not stored in the application or monitoring databases. Explicit stops clear leases immediately, and the controller reaper also performs automatic cleanup. Lease durations and the service lifetime limit are bounded: the accepted duration range is greater than zero and at most 60 seconds.

Start the fault simulator in its own PowerShell window from the project root:

```powershell
.\.venv\Scripts\python.exe -m services.fault_simulator.main
```

The simulator listens only on `127.0.0.1:8110` and accepts loopback clients only.

### Fault simulator API

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/health` | Fault simulator health |
| `GET` | `/scenarios` | List the four supported scenarios and their targets |
| `POST` | `/scenarios/{scenario_id}/start` | Start a scenario; optional JSON body: `{"duration_seconds": 30}` |
| `POST` | `/scenarios/{scenario_id}/stop?run_id={run_id}` | Stop the run and clear its leases |
| `GET` | `/runs/{run_id}` | Read a scenario run snapshot |

The simulator and service processes also expose loopback-only service control routes:

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/__control/faults` | Show the target service's active lease |
| `POST` | `/__control/faults` | Start a service-local fault lease |
| `DELETE` | `/__control/faults/{lease_id}` | Stop a matching service-local lease |

For example:

```powershell
Invoke-RestMethod http://127.0.0.1:8110/scenarios
$run = Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8110/scenarios/database_connection_failure/start `
  -ContentType 'application/json' `
  -Body (@{ duration_seconds = 25 } | ConvertTo-Json)
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8110/scenarios/database_connection_failure/stop?run_id=$($run.run_id)"
```

### Supported scenarios

The scenario IDs are declared in `config/fault_scenarios.json`:

| Scenario ID | Fault behavior |
|---|---|
| `database_connection_failure` | Rejects Database Service SQLite connections before opening the simulated database |
| `api_latency_injection` | Adds bounded latency to User Service business requests |
| `upstream_service_unavailable` | Returns HTTP 503 responses from User Service business routes |
| `cascading_service_failure` | Applies the Database Service connection failpoint and exercises Gateway probes through the configured dependency chain |

The implemented simulator supports no external targets or URL inputs. It only addresses the approved loopback service ports.

## End-to-end evaluation workflow

The Phase 8 isolated harness is `test_all_fault_scenarios_end_to_end_with_isolated_state` in `tests/test_phase8_evaluation.py`. It:

1. creates temporary application and monitoring databases;
2. starts the monitoring backend, four services, and fault simulator on unused loopback ports;
3. trains and persists a genuine model only in the temporary monitoring database;
4. sends baseline and fault-period requests;
5. waits for telemetry aggregation and detector persistence;
6. captures HTTP, telemetry, detector, anomaly, RCA, and recovery observations;
7. adds expected scenario labels only after observations are collected;
8. writes JSON and Markdown artifacts under a new run-specific directory;
9. terminates processes and removes temporary state.

Expected labels are reporting-only metadata. The current harness labels are `expected_database_failure`, `expected_latency_injection`, `expected_upstream_unavailable`, and `expected_cascading_failure`. They are not sent to the telemetry pipeline, the service processes, the detector, or RCA. They are not scenario identifiers used to control faults.

Run the isolated evaluation and Step 5 metrics tests:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_phase8_evaluation*.py" -v
```

Run all Phase 8 tests:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_phase8_*.py" -v
```

## Evaluation metrics and artifacts

Each successful harness run creates a uniquely named directory without overwriting earlier runs:

```text
evaluation_output/phase8/{evaluation_run_id}/
  evaluation.json
  summary.md
```

The JSON artifact contains UTC timestamps, one entry per scenario, observed HTTP outcomes, telemetry and trace evidence, model scores and decisions, threshold matches, anomaly records, RCA details, recovery observations, leak checks, and missing measurements.

Metric definitions:

- **Isolation Forest detected** means at least one valid `anomaly` decision was observed. Valid normal decisions mean not detected only when complete; missing or malformed decisions are unavailable.
- **Threshold detected** means at least one valid threshold match list was observed. Explicit complete empty lists mean not detected; missing or malformed matches are unavailable.
- **Either mechanism detected** is deduplicated at scenario level and is true if valid evidence exists from either mechanism. Unavailable values are not silently converted to negative detections.
- **Detection latency** is elapsed time from the first observed fault request start to the earliest anomaly record. It is a bounded run measurement, not a causal guarantee.
- **RCA triggered/completed** describe the investigation lifecycle. An RCA candidate rank score is a ranking point, not a probability. A probable outcome requires a completed investigation with selected candidate evidence; completion alone does not imply a probable RCA.
- **Recovery succeeded** requires successful health checks and a successful business request after fault stop. **Recovery time** is elapsed from explicit fault stop to successful recovery verification, so it is an upper bound rather than the exact restoration instant.
- **HTTP outcomes** count 2xx, 3xx, 4xx, 5xx, and network/unknown responses.
- **Anomaly records** are counted by unique usable anomaly ID. Duplicate ID entries and entries with missing or invalid IDs are reported separately.

Isolation Forest in this project is a **custom pure-Python implementation**; it is not a wrapper around scikit-learn. Isolation Forest scores are anomaly scores, not probabilities. RCA ranking also remains separate from anomaly detection and does not establish causal proof.

### Latest measured evaluation run

The latest final-regression run is `2f3ca062-d460-4d1c-ae4b-b3aaef2fc8c7`, created by the final full backend regression on 2026-11-20:

- [evaluation.json](evaluation_output/phase8/2f3ca062-d460-4d1c-ae4b-b3aaef2fc8c7/evaluation.json)
- [summary.md](evaluation_output/phase8/2f3ca062-d460-4d1c-ae4b-b3aaef2fc8c7/summary.md)

Observed summary:

| Scenario | Isolation Forest | Threshold rules | Either | RCA status/outcome | Recovery |
|---|---|---|---|---|---|
| `database_connection_failure` | not detected | detected | detected | completed / probable | succeeded |
| `api_latency_injection` | not detected | detected | detected | completed / probable | succeeded |
| `upstream_service_unavailable` | detected | detected | detected | completed / probable | succeeded |
| `cascading_service_failure` | detected | detected | detected | completed / probable | succeeded |

Across the run:

- 4/4 scenarios completed.
- Isolation Forest detected 2/4 measured scenarios.
- Threshold rules detected 4/4 measured scenarios.
- Either mechanism detected 4/4 measured scenarios.
- 46 HTTP observations were collected: 29 2xx and 17 5xx.
- 11 unique anomaly IDs were observed; no duplicate IDs or unusable IDs were recorded.
- Mean anomaly detection latency was 53,877.531 ms across four measurable scenarios.
- RCA completed four investigations, all classified as probable from the engine's observed candidate evidence.
- All four services/scenarios recovered; mean recovery verification time was 52,920.641 ms.
- All six expected-label leak checks were false for all four scenarios.

These numbers belong only to the latest final-regression run `2f3ca062-...`. They are not guaranteed performance or a representative accuracy estimate. This run used isolated temporary databases and a bounded fault workload. Accuracy, precision, recall, and F1-score are not calculated because the post-hoc scenario labels do not constitute a statistical labeled-observation dataset.

### Historical evaluation run

Run `552cf781-7efd-4918-a81c-94105afbc153` remains preserved in `evaluation_output/phase8/552cf781-7efd-4918-a81c-94105afbc153/`. It was a prior successful run and recorded 3/4 Isolation Forest detections, 4/4 threshold detections, 4/4 completed scenarios, and 4/4 verified recoveries. Its 3/4 Isolation Forest result is historical and must not be substituted for the latest 2/4 result.
