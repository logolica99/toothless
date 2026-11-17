#!/usr/bin/env bash

set -euo pipefail

readonly WORKER_VM_NAME="toothless-worker"
readonly WORKER_BASE_NAME="toothless-ubuntu-24.04-base"
readonly WORKER_IMAGE="ghcr.io/cirruslabs/ubuntu@sha256:e004f7f4f6765e2b3ae2738a95040c5d03867b667eb21a57ea0c3729aad640b6"
readonly WORKER_CPUS="4"
readonly WORKER_MEMORY_MB="6144"
readonly WORKER_DISK_GB="20"
readonly WORKER_MIN_FREE_GB="30"
readonly REQUIRED_TART_VERSION="2.41.1"
readonly REQUIRED_SOFTNET_VERSION="0.25.0"
readonly REQUIRED_SOFTNET_PATH="/usr/local/libexec/toothless/softnet"
readonly REQUIRED_SOFTNET_SHA256="19768da958f49ea3dca4ba18e5cff34bdceab781063162e72ba90f77b7477105"
readonly WORKER_STORAGE_VOLUME="/Volumes/toothless"
readonly WORKER_TOOLS_BIN="$WORKER_STORAGE_VOLUME/Tools/bin"
readonly WORKER_STORAGE_ROOT="$WORKER_STORAGE_VOLUME/Tools"
readonly TART_HOME="$WORKER_STORAGE_ROOT/Tart"
export TART_HOME
export PATH="/usr/local/bin:$WORKER_TOOLS_BIN:$PATH"
