"""
Pytest configuration: put function CodeUri dirs on sys.path
(matches SAM flat packaging: handler.py at package root).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "src" / "worker"
WEBHOOK = ROOT / "src" / "webhook"

# Prefer worker modules for `import handler` conflicts — tests import webhook as
# `import importlib; importlib.import_module` via explicit path switching.
sys.path.insert(0, str(WEBHOOK))
sys.path.insert(0, str(WORKER))
