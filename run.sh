#!/bin/zsh
# FOR DEVELOPMENT USE ONLY
set -euo pipefail  # Exit on error, undefined vars, pipe failures

# does finnaconnect_token exist
if [ -z "${finnaconnect_token+x}" ]; then
  echo "Access token not found."
  # ask
  read -sp "Please enter your access token: " token_input
  echo
  export finnaconnect_token="$token_input"
fi

# run ./finna/cli if exists
if [ ! -f "${PWD}/finna/cli" ]; then
  echo "Executable not found. Please build the project first."
  exit 1
fi

"${PWD}/finna/cli" "$@"