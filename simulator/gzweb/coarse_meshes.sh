#!/bin/bash

gzwebDir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${gzwebDir}" || exit 1

gzcoarse="${gzwebDir}/build/tools/gzcoarse"

if [[ ! -f "${gzcoarse}" ]]; then
  echo "Error: gzcoarse executable not found, exiting."
  exit 1
fi

echo "gzcoarse found: ${gzcoarse}."

if [[ $# -ne 2 ]]; then
  echo "Error: Please specify percentage and the assets path as argument."
  exit 2
fi

if [[ ! -d "$2" ]]; then
  echo "Error: $2 does not exist."
  exit 1
fi

percent="$1"
echo "Simplify to $1%"

assetDir="$2"
echo "Assets directory: $2"

echo "Removing old coarse dae files."

# Delete coarse meshes if exist
find "${assetDir}" -type f -name "*_coarse.dae" -exec rm -f {} +

# Run gzcoarse on all dae meshes
coarse_all_dae() {
  local file
  while IFS= read -r -d '' file; do
    [[ "${file}" == *_coarse.dae ]] && continue
    echo -e "\e[33mCoarsening \e[39m${file}"
    "${gzcoarse}" "${file}" "${percent}"
  done < <(find "${assetDir}" -type f -name '*.dae' -print0 || true)
}

echo "Simplifying meshes"
coarse_all_dae

echo "Done!"
