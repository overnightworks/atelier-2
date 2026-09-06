"""Throwaway probe for #1294: proves the Sonar job goes red on an open finding.

Not a real test of production behavior. The composite `assert a and b` trips
python:S9073; this file is removed again once the red run is recorded.
python:S5778 (two raising calls in one `pytest.raises` block) was tried first
but is excluded project-wide on `tests/**` by `sonar-project.properties`
(rule class e11), so it never surfaces as an open finding.
"""

from __future__ import annotations


def test_sonar_open_finding_probe_s9073() -> None:
    values = (1, 2)
    assert values[0] == 1 and values[1] == 2
