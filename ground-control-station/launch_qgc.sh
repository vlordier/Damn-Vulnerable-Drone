#! /bin/bash

if [[ "${HEADLESS:-}" = "1" ]]; then
  echo -e "INFO\t[QGC] HEADLESS SET. RUNNING IN HEADLESS MODE."
  xvfb-run /home/gcs/QGroundControl.AppImage
else
  /home/gcs/QGroundControl.AppImage
fi
