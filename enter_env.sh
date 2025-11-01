#!/bin/bash
set -e

echo "Entering isolated Nix env for Vespula with BlueZ 5.66..."

# Ensure Nix installed
if ! command -v nix &> /dev/null; then
  echo "Nix not found. Installing..."
  sh <(curl -L https://nixos.org/nix/install) --daemon
  source /etc/profile  # Reload env
fi

# Enter flake dev shell
nix develop --command bash -c "
  # Stop system daemons
  sudo systemctl stop bluetooth bluetooth-meshd

  # Start custom BlueZ daemons
  sudo \$PWD/.nix-profile/sbin/bluetoothd --noplugin=sap --experimental &
  sudo \$PWD/.nix-profile/sbin/bluetooth-meshd --debug &

  # Run app
  python3 run_gui.py

  # Cleanup on exit (trap)
  trap 'sudo systemctl start bluetooth bluetooth-meshd' EXIT
"

echo "Exited env. System restored."
