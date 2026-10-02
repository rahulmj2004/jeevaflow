#!/bin/bash
# Start the backend, run the live end-to-end smoke test, keep serving.

set -e

cd "$(dirname "$0")"

PYTHON=${PYTHON:-venv/bin/python}

echo "================================="
echo "       JEEVAFLOW DEMO"
echo "================================="

echo ""
echo "Starting backend..."

$PYTHON -m uvicorn app.main:app --host 127.0.0.1 --port 8000 &
SERVER_PID=$!

cleanup() {
    echo ""
    echo "Stopping JeevaFlow..."
    kill $SERVER_PID 2>/dev/null || true
}

trap cleanup EXIT

for _ in $(seq 1 30); do
    curl -sf http://127.0.0.1:8000/api/v1/health >/dev/null && break
    sleep 0.5
done

echo ""
echo "Running end-to-end smoke test..."

$PYTHON test_api.py

echo ""
echo "================================="
echo "       DEMO READY"
echo "================================="
echo ""
echo "API:  http://127.0.0.1:8000"
echo "Docs: http://127.0.0.1:8000/docs (development only)"
echo ""

wait $SERVER_PID
