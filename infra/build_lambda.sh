#!/usr/bin/env bash
# Assemble the Lambda deployment bundle into infra/build/ before `cdk deploy`.
# Installs the landiq_rag package + runtime deps targeting the Lambda platform.
# For native wheels (psycopg, pydantic-core, tiktoken) prefer running this on
# Linux or in the Lambda build image; the --python-platform flag fetches
# manylinux wheels where available.
set -euo pipefail
cd "$(dirname "$0")"
rm -rf build
mkdir -p build

uv pip install \
  --target build \
  --python-platform x86_64-manylinux2014 \
  --python-version 3.12 \
  --only-binary=:all: \
  .. \
  || { echo "platform-specific install failed; falling back to local install"; \
       uv pip install --target build ..; }

echo "Lambda bundle assembled in $(pwd)/build"
