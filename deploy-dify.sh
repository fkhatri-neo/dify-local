#!/usr/bin/env bash
set -euo pipefail

echo "=== Dify Community Edition - Local Deployment ==="
echo ""

# Step 1: Check Docker
echo "[1/5] Checking if Docker is installed..."
if ! command -v docker &>/dev/null; then
  echo "ERROR: Docker is not installed. Install it from https://docs.docker.com/get-docker/ and try again."
  exit 1
fi

echo "[2/5] Checking if Docker daemon is running..."
if ! docker info &>/dev/null; then
  echo "ERROR: Docker is installed but not running. Start Docker Desktop (or the Docker daemon) and try again."
  exit 1
fi
echo "       Docker is ready."

# Step 2: Clone the repo
echo "[3/5] Cloning the Dify repository..."
if [ -d "dify" ]; then
  echo "       Directory 'dify' already exists — skipping clone."
else
  git clone https://github.com/langgenius/dify.git
  echo "       Clone complete."
fi

# Step 3: Navigate into docker directory
echo "[4/5] Preparing environment file..."
cd dify/docker

if [ ! -f ".env.example" ]; then
  echo "ERROR: .env.example not found in dify/docker. The repo structure may have changed."
  exit 1
fi

cp .env.example .env
echo "       .env file created from .env.example."

# Step 4: Start containers
echo "[5/5] Starting containers with docker compose..."
docker compose up -d
echo ""
echo "=== Dify is now starting! ==="
echo "Access the web UI at: http://localhost/install (default port 80)"
echo "Run 'docker compose ps' inside dify/docker to check container status."
