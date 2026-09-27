#!/bin/bash
set -e

echo "Starting test_zone..."

cd "$(dirname "$0")"

docker compose up --build -d

echo "test_zone is running!"
echo "Frontend: http://localhost:80"
echo "Backend: http://localhost:8080"
echo "API: http://localhost:80/api/hello"