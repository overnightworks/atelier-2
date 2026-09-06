"""The page a listing or stream may answer with.

One bound, written once. Every reader that admits a page — the HTTP `limit`,
the instance's event page, the durable query adapters — constructs or checks
this type rather than restating the number.
"""

from __future__ import annotations

from dataclasses import dataclass

MINIMUM_PAGE_ITEMS = 1
"""The fewest items one page may carry."""

MAXIMUM_PAGE_ITEMS = 100
"""How many items one page may carry.

The product admits a page at its edge and in the store with the same bound.
A second copy of this number is the drift #88 Hygiene-4 names.
"""

DEFAULT_PAGE_LIMIT = 50
"""How many items a listing serves when a caller does not ask for fewer."""


@dataclass(frozen=True)
class PageLimit:
    value: int

    def __post_init__(self) -> None:
        if (
            type(self.value) is not int
            or not MINIMUM_PAGE_ITEMS <= self.value <= MAXIMUM_PAGE_ITEMS
        ):
            raise ValueError(
                f"page limit must be an integer from {MINIMUM_PAGE_ITEMS} "
                f"to {MAXIMUM_PAGE_ITEMS}"
            )
