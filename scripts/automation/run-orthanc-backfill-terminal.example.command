#!/bin/bash

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
exec "$SCRIPT_DIR/run-orthanc-backfill-daemon.example.sh"
