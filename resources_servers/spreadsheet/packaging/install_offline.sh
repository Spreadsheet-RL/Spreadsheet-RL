#!/usr/bin/env bash
set -euo pipefail
bundle_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ $(id -u) != 0 ]]; then
  echo 'Run as root inside the service container; chroot/UID isolation needs root.' >&2
  exit 1
fi
source /etc/os-release
if [[ ${ID:-} != ubuntu || ${VERSION_ID:-} != 24.04 || $(uname -m) != x86_64 ]]; then
  echo 'This bundle is qualified for Ubuntu 24.04 x86_64 only.' >&2
  exit 1
fi
cd "$bundle_dir"
sha256sum -c SHA256SUMS >/dev/null
if [[ -e "$bundle_dir/service-venv" ]]; then
  echo 'service-venv already exists; refusing to overwrite it.' >&2
  exit 1
fi
# This is an offline package install in the existing container, not a Docker build.
# Packages and their entire apt dependency closure were downloaded on Modal.
export DEBIAN_FRONTEND=noninteractive
apt-get -y --no-download --no-install-recommends install "$bundle_dir"/debs/*.deb
/usr/bin/python3 -m venv "$bundle_dir/service-venv"
"$bundle_dir/service-venv/bin/python" -m pip install --no-index \
  --find-links "$bundle_dir/wheelhouse" \
  -r "$bundle_dir/source/resources_servers/spreadsheet/service/requirements.txt"
# Root filesystem contains no service keys, oracle files, or live workspaces.
chown -R 0:0 "$bundle_dir/python-root"
chmod 755 "$bundle_dir/python-root"
cd "$bundle_dir/source"
"$bundle_dir/service-venv/bin/python" - "$bundle_dir/python-root" <<'PY'
import sys
from pathlib import Path
from resources_servers.spreadsheet.service.chroot_runner import ChrootPython
ChrootPython(Path(sys.argv[1])).probe()
print('Target chroot + UID + seccomp probe passed.')
PY
echo 'Offline runtime installed. Read the README to prepare the private catalog and service config.'
