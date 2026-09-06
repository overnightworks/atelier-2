"""Throwaway probe for #1294: proves the Sonar job goes red on an open finding.

Not a real test of production behavior. Two calls inside the one
`pytest.raises` block that can each raise the expected exception trip
python:S5778; this file is removed again once the red run is recorded.
"""

from __future__ import annotations

import pytest


def test_sonar_open_finding_probe_s5778() -> None:
    with pytest.raises(ValueError):
        int("not a number")
        int("still not a number")
