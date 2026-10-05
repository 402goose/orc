"""Repository tests (also importable with python -m unittest test.<module>).

Importing the package points FUSION_LAYA_PYTHON at `false`, so a test that
reaches the Laya runtime without a fake backend fails fast instead of loading
the real model. `make test` sets the same value before discovery; this covers
`python -m unittest test.<module>`, the form agents use.

Clear the worker's inherited control workspace so tests use their temporary
directories. The Makefile unsets it separately because its runner discovers
top-level modules without importing this package."""
import os
import shutil

os.environ.pop('FUSION_CONTROL_WORKSPACE', None)
os.environ.setdefault('FUSION_LAYA_PYTHON', shutil.which('false') or '/usr/bin/false')
