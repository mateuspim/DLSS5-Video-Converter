#!/usr/bin/env sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
output_dir="$project_root/bin/runtime"
output_file="$output_dir/nvngx.dll"
compiler=x86_64-w64-mingw32-clang++

mkdir -p "$output_dir"

"$compiler" \
  --std=c++17 \
  -O2 \
  -static \
  -fms-extensions \
  -fms-compatibility \
  -Wno-pragma-pack \
  -I"$project_root/native/NVIDIA-DLSS/include" \
  "$project_root/native/DLSS5-Feeder/host/dlss5-feed-host64.cpp" \
  "$project_root/native/NVIDIA-DLSS/lib/Windows_x86_64/x64/nvsdk_ngx_d.lib" \
  -lversion \
  -lkernel32 \
  -luser32 \
  -lgdi32 \
  -ladvapi32 \
  -lole32 \
  -Wl,--subsystem,console \
  -o "$output_file"

echo "Built Windows worker: $output_file"
sha256sum "$output_file"
