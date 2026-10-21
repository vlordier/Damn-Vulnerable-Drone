#!/bin/bash

usage() {
  cat <<EOF
OPTIONS:
   -h      Show this message
   -m      Build a local model database.
           Option "local" to use only local models.
   -c      Create coarse versions of all models in the local database
   -t      Generate a thumbnail for each model
EOF
  return 0
}

MODELS=0
LOCAL=0
COARSE=0
THUMBNAIL=0

while (($#)); do
  case "$1" in
    -m)
      MODELS=1
      shift
      if (($#)) && [[ "$1" == "local" ]]; then
        LOCAL=1
        shift
      fi
      ;;
    -c)
      COARSE=1
      shift
      ;;
    -t)
      THUMBNAIL=1
      shift
      ;;
    -h | --help)
      usage
      exit 0 # print help and terminate option parsing
      ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2 # terminate after reporting an invalid option
      ;;
  esac
done

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${DIR}" || exit 1

# Install node modules
npm install

# Assemble javascript files
"${DIR}/node_modules/.bin/grunt" build --force

# build the c++ server component
rm -rf build
mkdir build
cd build || exit 1

# Run cmake and check for the exit code
cmake ..

RETVAL=$?
if [[ "${RETVAL}" -ne 0 ]]; then
  echo There are cmake errors, exiting.
  exit 1
fi

# continue building if cmake is happy
make -j 8

cd ../gzbridge || exit 1
"${DIR}/node_modules/.bin/node-gyp" rebuild -d

RETVAL=$?
if [[ "${RETVAL}" -ne 0 ]]; then
  echo There are node-gyp build errors, exiting.
  exit 1
fi

cd "${DIR}" || exit 1

# build a local model database
if [[ "${MODELS}" -eq 1 ]]; then
  # Temporal directory for the repository
  TMP_DIR=$(mktemp -d)
  cd "${TMP_DIR}" || exit 1

  # If no arg given then download from gazebo_models repo
  if [[ "${LOCAL}" -eq 0 ]]; then
    echo -n "Downloading gazebo_models..."
    git clone https://github.com/osrf/gazebo_models.git

    RETVAL=$?
    if [[ "${RETVAL}" -ne 0 ]]; then
      echo There are mercurial clone errors, exiting.
      exit 1
    fi

    echo "Download complete"
    cd gazebo_models || exit 1
    mkdir build
    cd build || exit 1
    echo -n "Installing gazebo_models..."
    if ! { cmake .. -DCMAKE_INSTALL_PREFIX="${DIR}/http/client" && make install >/dev/null 2>&1; }; then
      echo There are build errors, exiting.
      exit 1
    fi

    echo "Install complete"

    # Remove temp dir
    rm -rf "${TMP_DIR}"
    rm -rf "${DIR}/http/client/assets"
    mv "${DIR}/http/client/models" "${DIR}/http/client/assets"
  fi

  cd "${DIR}" || exit 1

  echo "Gather all models on the local machine"

  mkdir -p "${DIR}/http/client/assets"
  "${DIR}/get_local_models.py" "${DIR}/http/client/assets"
  "${DIR}/webify_models_v2.py" "${DIR}/http/client/assets"

else
  mkdir -p "${DIR}/http/client/assets"
  echo "Not cloning the model repo"
fi

if [[ "${THUMBNAIL}" -eq 1 ]]; then
  echo "Generating a thumbnail for each model. Make sure gazebo is not running"
  "${DIR}/tools/gzthumbnails.sh"
fi

# build a local model database
if [[ "${COARSE}" -eq 1 ]]; then
  "${DIR}/coarse_meshes.sh" 50 "${DIR}/http/client/assets/"
fi

echo "Done"
