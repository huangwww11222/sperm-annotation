#!/bin/sh
set -eu
python -m app.deployment_check
exec "$@"
