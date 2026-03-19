#!/usr/bin/env bash
# Download potion-base-8M model files for embedding computation.
# Files are stored in ~/.agentc/models/potion-base-8M/

set -euo pipefail

DATA_DIR="${HOME}/.agentc/models/potion-base-8M"
MODEL_REPO="minishlab/potion-base-8M"
BASE_URL="https://huggingface.co/${MODEL_REPO}/resolve/main"

mkdir -p "$DATA_DIR"

echo "Downloading tokenizer.json..."
curl -fSL "${BASE_URL}/tokenizer.json" -o "${DATA_DIR}/tokenizer.json"

echo "Downloading model.safetensors..."
curl -fSL "${BASE_URL}/model.safetensors" -o "${DATA_DIR}/model.safetensors"

echo "Downloading config.json..."
curl -fSL "${BASE_URL}/config.json" -o "${DATA_DIR}/config.json"

echo "Done. Files saved to ${DATA_DIR}/"
ls -lh "${DATA_DIR}/"
