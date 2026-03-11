"""
Root conftest.py — ensures shark_config.json exists before any module imports core_constants.
Copies shark_config.example.json → shark_config.json if the real config is missing.
This runs BEFORE pytest collects tests, so core_constants.load_full_config() succeeds.
"""
import shutil
import os

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "shark_config.json")
EXAMPLE_PATH = os.path.join(PROJECT_ROOT, "shark_config.example.json")

if not os.path.exists(CONFIG_PATH) and os.path.exists(EXAMPLE_PATH):
    shutil.copy2(EXAMPLE_PATH, CONFIG_PATH)
