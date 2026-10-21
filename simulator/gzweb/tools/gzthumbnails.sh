#!/bin/bash

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASSETS="${DIR}/../http/client/assets"

for dir in "${ASSETS}"/*/; do
  [[ -d "${dir}" ]] || continue
  model_dir="${dir%/}"
  thumbnails="${model_dir}/thumbnails"
  echo "Creating thumbnail for ${model_dir##*/}"
  rm -rf "${thumbnails}"
  if [[ -f "${model_dir}/model.sdf" ]]; then
    # generate thumbnails with green bg
    gzserver -s libModelPropShop.so "${DIR}/green.world" --propshop-save "${thumbnails}" --propshop-model "${model_dir}/model.sdf"
    # make green bg transparent
    convert "${thumbnails}/1.png" -fuzz 30% -transparent '#00ff00' "${thumbnails}/0.png"
    # crop transparent ends
    convert "${thumbnails}/0.png" -trim "${thumbnails}/0.png"
    # add shadow
    convert -background none -fill black \
      "${thumbnails}/0.png" \
      \( +clone -background black -shadow 100x10+0+0 \) +swap \
      -background none -layers merge +repage "${thumbnails}/0.png"
    # remove extra files
    rm -f "${thumbnails}"/{1,2,3,4,5}.png
  fi
done
