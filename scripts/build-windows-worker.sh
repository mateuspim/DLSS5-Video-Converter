#!/usr/bin/env sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
image_name=${DLSS5_BUILDER_IMAGE:-dlss5-windows-worker-builder:20260826}

docker build \
  --file "$project_root/native/Dockerfile.cross" \
  --tag "$image_name" \
  "$project_root"

docker run --rm \
  --user "$(id -u):$(id -g)" \
  --volume "$project_root:/src" \
  "$image_name"

echo "Worker ready at: $project_root/bin/runtime/nvngx.dll"
