#!/usr/bin/env sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
repository=${DLSS5_GITHUB_REPOSITORY:-mateuspim/DLSS5-Video-Converter}
branch=${DLSS5_GITHUB_BRANCH:-docker-linux-wsl}
workflow=windows-worker.yml

if ! command -v gh >/dev/null 2>&1; then
  echo "GitHub CLI (gh) is required: https://cli.github.com/" >&2
  exit 1
fi

gh auth status >/dev/null
previous_run_id=$(gh run list \
  --repo "$repository" \
  --workflow "$workflow" \
  --branch "$branch" \
  --event workflow_dispatch \
  --limit 1 \
  --json databaseId \
  --jq '.[0].databaseId // empty')
gh workflow run "$workflow" --repo "$repository" --ref "$branch"

run_id=""
attempt=0
while [ -z "$run_id" ] && [ "$attempt" -lt 20 ]; do
  attempt=$((attempt + 1))
  candidate_run_id=$(gh run list \
    --repo "$repository" \
    --workflow "$workflow" \
    --branch "$branch" \
    --event workflow_dispatch \
    --limit 1 \
    --json databaseId \
    --jq '.[0].databaseId // empty')
  if [ -n "$candidate_run_id" ] && [ "$candidate_run_id" != "$previous_run_id" ]; then
    run_id=$candidate_run_id
  fi
  [ -n "$run_id" ] || sleep 3
done

if [ -z "$run_id" ]; then
  echo "Timed out waiting for the Windows build run to appear." >&2
  exit 1
fi

gh run watch "$run_id" --repo "$repository" --exit-status
mkdir -p "$project_root/bin/runtime"
gh run download "$run_id" \
  --repo "$repository" \
  --name nvngx-windows-worker \
  --dir "$project_root/bin/runtime"

echo "Worker ready at: $project_root/bin/runtime/nvngx.dll"
