#!/usr/bin/env bash
set -euo pipefail

minimum_mib="${MIN_FREE_MIB:-${QGC_GAZEBO_MIN_FREE_MIB:-2048}}"
minimum_setting="MIN_FREE_MIB"
if [[ -z "${MIN_FREE_MIB:-}" ]]; then
  minimum_setting="QGC_GAZEBO_MIN_FREE_MIB"
fi
if [[ ! "${minimum_mib}" =~ ^[1-9][0-9]*$ ]]; then
  echo "${minimum_setting} must be a positive integer (MiB); got '${minimum_mib}'." >&2
  exit 2
fi
minimum_kib=$((minimum_mib * 1024))

if [[ "$#" -eq 0 ]]; then
  echo 'Pass at least one path whose filesystem should be checked.' >&2
  exit 2
fi

for requested_path in "$@"; do
  path="${requested_path}"
  while [[ ! -e "${path}" ]]; do
    parent="$(dirname "${path}")"
    if [[ "${parent}" == "${path}" ]]; then
      echo "Cannot find an existing parent for disk-space path: ${requested_path}" >&2
      exit 2
    fi
    path="${parent}"
  done

  df_output="$(df -Pk "${path}")"
  available_kib="$(printf '%s\n' "${df_output}" | awk 'END {print $4}')"
  if [[ ! "${available_kib}" =~ ^[0-9]+$ ]]; then
    echo "Could not read available disk space for ${requested_path}." >&2
    exit 2
  fi
  if (( available_kib < minimum_kib )); then
    available_mib=$((available_kib / 1024))
    echo "Insufficient free disk space: ${requested_path} has ${available_mib} MiB free; need at least ${minimum_mib} MiB." >&2
    echo "Free space or set ${minimum_setting} to a lower positive value to continue." >&2
    exit 1
  fi
done

echo "Disk-space preflight passed (at least ${minimum_mib} MiB free on each checked filesystem)."
