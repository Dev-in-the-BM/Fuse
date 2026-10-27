"""Expose distro-installed PyGObject without shadowing the app's venv modules."""

import os
import sys


system_gi_path = os.environ.get("MTK_GI_PATH")
if system_gi_path and system_gi_path not in sys.path:
    # Keep distro packages behind the selected uv environment. Putting this
    # directory on PYTHONPATH would make system extension modules (notably
    # _cffi_backend) override versions installed in that environment.
    sys.path.append(system_gi_path)
