"""Test package. Pipeline logging is silenced so test output stays readable."""

import logging

logging.getLogger("aicut").setLevel(logging.CRITICAL)
