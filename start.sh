#!/usr/bin/env bash
# =============================================================================
# start.sh — boots all four processes for the single-container deployment
# =============================================================================
# Order matters: data_services has no dependencies, the action server
# needs data_services, Rasa needs the action server (+ the model already
# baked in by `docker build`), and Streamlit needs Rasa. Each stage waits
# on the previous one's health/status endpoint rather than a fixed sleep,
# so this is robust to a slow first boot (e.g. a cold, under-powered
# free-tier container).
# =============================================================================
set -e

wait_for() {
    local url="$1" label="$2" attempts=0
    echo "Waiting for ${label}..."
    until curl -sf "$url" > /dev/null 2>&1; do
        attempts=$((attempts + 1))
        if [ "$attempts" -gt 60 ]; then
            echo "ERROR: ${label} did not become ready in time." >&2
            exit 1
        fi
        sleep 2
    done
    echo "${label} is ready."
}

echo "=== 1/4 Starting data_services (geocode/hotels/transport/attractions/weather/currency/carbon/flights, port 8000) ==="
uvicorn data_services.main:app --host 0.0.0.0 --port 8000 &
wait_for "http://localhost:8000/health" "data_services"

echo "=== 2/4 Starting Rasa action server (port 5055) ==="
export DATA_SERVICES_URL="http://localhost:8000"
rasa run actions --port 5055 &
# The action server has no simple /health endpoint by default; give it a
# moment to import actions/ (which imports requests, etc.) before Rasa
# itself starts sending it webhook calls.
sleep 5

echo "=== 3/4 Starting Rasa server (port 5005) ==="
# No ACTIONS_HOST export needed here — endpoints.yml hardcodes
# http://localhost:5055/webhook, which is correct in this single combined
# container since the action server started above is on the same host.
rasa run --enable-api --cors "*" --port 5005 &
wait_for "http://localhost:5005/status" "Rasa server"

echo "=== 4/4 Starting Streamlit dashboard (port ${PORT:-7860}) ==="
export RASA_URL="http://localhost:5005"
exec streamlit run streamlit_app.py \
    --server.port "${PORT:-7860}" \
    --server.address 0.0.0.0 \
    --server.headless true \
    --browser.gatherUsageStats false
