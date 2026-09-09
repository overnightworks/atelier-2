"""Where documentation lives in a Python module's own text.

The changed-narrative gate and the size ratchet both need to answer the same
question -- which columns of which physical line are a docstring or a `#`
comment, rather than code -- so both read it from here instead of keeping two
definitions of "documentation" that can drift apart.
"""

from __future__ import annotations

import ast
import io
import tokenize
from dataclasses import dataclass

_DOCSTRING_HOLDER_NODES = (
    ast.Module,
    ast.ClassDef,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
)


@dataclass(frozen=True, slots=True)
class LineSlice:
    """One line's documentation content: the column range it occupies on
    that physical line, and the text in that range."""

    start_column: int
    end_column: int
    text: str


def docstring_line_slices(
    source_lines: list[str], tree: ast.AST
) -> dict[int, LineSlice]:
    """Per docstring line, only the docstring's own slice of that physical
    line -- never any code sharing the line, such as a one-line function's
    header before the opening quotes. A docstring is the first statement of
    a module, class, or function body, and only that canonical position."""
    slices: dict[int, LineSlice] = {}
    for node in ast.walk(tree):
        if not isinstance(node, _DOCSTRING_HOLDER_NODES) or not node.body:
            continue
        statement = node.body[0]
        if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
            and statement.end_lineno is not None
            and statement.end_col_offset is not None
        ):
            continue
        for line_number in range(statement.lineno, statement.end_lineno + 1):
            line = source_lines[line_number - 1]
            start_column = (
                statement.col_offset if line_number == statement.lineno else 0
            )
            end_column = (
                statement.end_col_offset
                if line_number == statement.end_lineno
                else len(line)
            )
            slices[line_number] = LineSlice(
                start_column, end_column, line[start_column:end_column]
            )
    return slices


def comment_line_slices(source: str) -> dict[int, LineSlice]:
    """Every physical line carrying a `#` comment, mapped to the column range
    from the `#` to the line's own end."""
    return {
        token.start[0]: LineSlice(token.start[1], token.end[1], token.string)
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    }
