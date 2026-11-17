#!/bin/bash
# Build the .deb from this checkout and install it. Run: sudo bash install.sh
# (The same package is in the CardputerZero AppStore as "Desk Mode".)
set -euo pipefail
(( EUID == 0 )) || { echo 'Run with sudo'; exit 1; }
cd "$(dirname "$0")"

bash build-deb.sh
deb="$(ls -t build | grep '^cz-desk-mode_.*_all\.deb$' | head -1)"
# apt's download user cannot read a home directory; install from a copy.
cp "build/$deb" /tmp/ && apt-get install -y "/tmp/$deb" && rm -f "/tmp/$deb"
sleep 2
systemctl --no-pager status cz-desk-mode.service | head -12
echo 'INSTALLED. Logs: journalctl -u cz-desk-mode -f   Uninstall: sudo apt remove cz-desk-mode'
