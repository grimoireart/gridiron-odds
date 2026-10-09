"""Render index.html from src/page_template.html and a model.json dict (D13).

The template carries two placeholders:

* ``__MODEL_JSON__`` inside ``<script id="model" type="application/json">`` —
  replaced by the serialized model with ``</`` and U+2028/U+2029 escaped so
  no feed string can close the script element or break the JS parser.
* ``__ODDS_URL__`` inside ``<script>window.GRIDIRON_ODDS_URL = "__ODDS_URL__"</script>``
  — the optional live-overlay URL (D6), taken from ``GRIDIRON_ODDS_URL`` at
  build time; empty string disables the overlay. Escaped as a JS string.

Everything here is pure except ``read_template``/``write_index``.

CLI (used by build.py or by hand)::

    python3 -m src.render model.json index.html            # default template
    python3 -m src.render model.json index.html --template src/page_template.html
"""
from __future__ import annotations

import json
import os
import sys

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "page_template.html")
MODEL_PLACEHOLDER = "__MODEL_JSON__"
ODDS_URL_PLACEHOLDER = "__ODDS_URL__"
ODDS_URL_ENV = "GRIDIRON_ODDS_URL"


def escape_json_for_script(text: str) -> str:
    """Make a JSON string safe inside a ``<script>`` element.

    Every ``<`` becomes the JSON escape ``\\u003c`` (the parsed value is unchanged), which
    covers ``</script>`` AND ``<!--`` — the HTML tokenizer treats ``<!--<script>`` inside
    script data as the start of a double-escaped block that swallows the real ``</script>``.
    The two Unicode line separators become ``\\u2028``/``\\u2029`` (legal JSON escapes;
    they would otherwise be literal newlines to some parsers).
    """
    return (
        text.replace("<", "\\u003c")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def serialize_model(model: dict) -> str:
    """Serialize the model at full precision (no rounding; ``allow_nan`` off per contract)."""
    text = json.dumps(model, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return escape_json_for_script(text)


def escape_js_string(value: str) -> str:
    """Escape a value for use inside a double-quoted JS string literal (``<`` included, so
    neither ``</script>`` nor ``<!--`` can reach the HTML tokenizer)."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("<", "\\u003c")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_page(template: str, model: dict, odds_url: str = "") -> str:
    """Return the finished HTML for ``model`` using ``template`` text."""
    if MODEL_PLACEHOLDER not in template:
        raise ValueError(f"template is missing {MODEL_PLACEHOLDER}")
    if ODDS_URL_PLACEHOLDER not in template:
        raise ValueError(f"template is missing {ODDS_URL_PLACEHOLDER}")
    if template.count(MODEL_PLACEHOLDER) != 1:
        raise ValueError(f"template must contain {MODEL_PLACEHOLDER} exactly once")
    # Split on the model placeholder first so the odds-URL substitution never scans feed text
    # (a model string containing the literal placeholder stays as it is).
    head, _, tail = template.partition(MODEL_PLACEHOLDER)
    url = escape_js_string(odds_url or "")
    return head.replace(ODDS_URL_PLACEHOLDER, url) + serialize_model(model) + tail.replace(ODDS_URL_PLACEHOLDER, url)


def read_template(path: str = TEMPLATE_PATH) -> str:
    """Read the template file as UTF-8 text."""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def write_index(model: dict, out_path: str, template_path: str = TEMPLATE_PATH,
                odds_url: str | None = None) -> str:
    """Render ``model`` and write it atomically to ``out_path``; return the HTML.

    ``odds_url`` defaults to the ``GRIDIRON_ODDS_URL`` environment variable.
    """
    if odds_url is None:
        odds_url = os.environ.get(ODDS_URL_ENV, "")
    html = render_page(read_template(template_path), model, odds_url)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(html)
    os.replace(tmp, out_path)
    return html


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    template_path = TEMPLATE_PATH
    if "--template" in args:
        i = args.index("--template")
        template_path = args[i + 1]
        del args[i:i + 2]
    if len(args) != 2:
        print("usage: python3 -m src.render MODEL_JSON OUT_HTML [--template PATH]", file=sys.stderr)
        return 2
    with open(args[0], encoding="utf-8") as fh:
        model = json.load(fh)
    write_index(model, args[1], template_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
