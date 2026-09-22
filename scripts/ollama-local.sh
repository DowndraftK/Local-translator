#!/bin/bash
set -euo pipefail
# Foreground, session-scoped configuration; does not change the user's Ollama settings.
export OLLAMA_HOST=127.0.0.1:11434
export OLLAMA_NO_CLOUD=1
export OLLAMA_NOPRUNE=1
export OLLAMA_MAX_LOADED_MODELS=1
exec ollama serve
