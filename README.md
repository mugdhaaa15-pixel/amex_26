# Autonomous Travel-Disruption Concierge — working prototype

A real, tested, four-agent pipeline (Monitor → Decision → Execution →
Notification) plus a browser UI that drives it live over the network.

## What's real vs. mocked

| Piece | Status |
|---|---|
| Disruption-detection algorithm (`app/monitor.py`) | Real logic: cancellation check, missed-connection buffer math against per-airport minimum connection times, delay-tolerance check |
| Rebooking decision engine (`app/decision.py`) | Real logic: policy filtering (fare cap, cabin downgrade rule, layover cap, arrival-delay cap) + a weighted multi-criteria scoring function that ranks compliant options |
| Execution agent (`app/execution.py`) | Real retry/error-handling logic. Talks to `AirlineClient` / `HotelClient` interfaces |
| Notification agent (`app/notification.py`) | Real message composition. Talks to a `NotificationSender` interface |
| Live flight feed, flight inventory, airline/hotel/push APIs (`app/data.py`) | **Mocked** — no Amadeus/Sabre/push-provider credentials are available in this environment. Mock clients implement the same interfaces the real ones would, with simulated latency, so swapping in real APIs means editing only `app/data.py` |

The seam is deliberate: everything in `app/` except `data.py` has no idea it's
talking to mocks. A production build replaces `MockAirlineClient` with a real
Amadeus/Sabre client and `events_for_scenario()` with a live feed subscription
— nothing else changes.

## Run it

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Then open `frontend/index.html` in a browser (it talks to `http://localhost:8000`).

## Run the tests

```bash
pip install -r requirements.txt pytest
python -m pytest tests/ -v
```

11 tests cover: cancellation detection, missed-connection buffer math, safe-buffer
non-detection, policy filtering, multi-criteria scoring, hotel-date sync, retry/failure
handling, and three full end-to-end scenario runs.

## API

- `GET /itinerary` — current trip
- `POST /reset` — reset to the original trip
- `POST /simulate/{scenario}` — run the pipeline once, get the full JSON result
- `GET /stream/{scenario}` — Server-Sent Events: watch each agent stage fire live
- `GET /metrics` — running detection speed / resolution time / success rate across all runs this session

`scenario` is one of `cancellation`, `missed`, `delay`.

## Project layout

```
app/
  models.py        domain objects (Itinerary, Leg, Policy, candidates, reports)
  monitor.py        Task 1 — detection algorithm
  decision.py        Task 2 — policy filter + scoring
  execution.py        Task 2/4 — booking + hotel sync, via injected clients
  notification.py    Task 3/4 — message composition + send
  metrics.py          Task 5 — detection speed / success rate tracking
  orchestrator.py    wires the four agents into one pipeline
  data.py              mock itinerary, live feed, inventory, API clients
  main.py             FastAPI surface (REST + SSE)
tests/
  test_pipeline.py    11 tests against the real algorithm and pipeline
frontend/
  index.html           UI that streams from /stream and renders real agent output
```
