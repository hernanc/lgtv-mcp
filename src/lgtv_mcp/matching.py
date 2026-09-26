"""Forgiving name matching for apps and inputs, and cleanup of TV-provided text."""

import re
import unicodedata

from .errors import Ambiguous, NotFound

_MAX_CANDIDATES = 10
_MAX_TEXT = 200


def pick(query: str, items: dict[str, str], kind: str) -> str:
    """Return the id in ``items`` ({id: label}) that ``query`` refers to.

    An exact id or label match wins (ignoring case and punctuation), otherwise
    a single substring match. Zero or several matches raise with the options.
    """
    q = _norm(query)
    if not q:
        raise NotFound(f"No {kind} name given.")

    exact = [i for i, label in items.items() if q in (_norm(i), _norm(label))]
    if len(exact) == 1:
        return exact[0]

    partial = exact or [i for i, label in items.items() if q in _norm(label) or q in _norm(i)]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise NotFound(f"No {kind} matches '{clean_text(query)}'. List the {kind}s to see names.")

    labels = sorted(items[i] for i in partial)
    shown = ", ".join(labels[:_MAX_CANDIDATES])
    extra = len(labels) - _MAX_CANDIDATES
    more = f" and {extra} more" if extra > 0 else ""
    raise Ambiguous(f"'{clean_text(query)}' matches several {kind}s: {shown}{more}.")


def clean_text(value: object) -> str:
    """Make a TV-provided string safe to display: no control or format characters."""
    if value is None:
        return ""
    text = str(value)
    chars = (" " if ch in "\n\r\t" else ch for ch in text)
    kept = "".join(ch for ch in chars if unicodedata.category(ch)[0] != "C")
    return " ".join(kept.split())[:_MAX_TEXT]


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())
