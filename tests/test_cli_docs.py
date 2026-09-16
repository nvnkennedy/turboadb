"""The CLI reference (CLI.md) and the website's CLI page must document every
command the parser has — and nothing it doesn't — so the docs can't drift."""
import argparse
import os
import re

import turboadb.cli as cli

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _commands():
    for action in cli.build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    return set()


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_cli_md_documents_every_command():
    documented = set(re.findall(r"(?m)^### `turboadb ([a-z-]+)`", _read("CLI.md")))
    assert documented == _commands()


def test_website_cli_page_documents_every_command():
    documented = set(re.findall(r'data-cmd="([a-z-]+)"', _read("docs", "cli.html")))
    assert documented == _commands()


def test_documented_options_exist():
    """Every ``--option`` named in CLI.md is a real option of some command."""
    parser = cli.build_parser()
    real = {flag for action in parser._actions for flag in action.option_strings}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                real.update(flag for a in sub._actions for flag in a.option_strings)
    named = set(re.findall(r"`(--[a-z][a-z0-9-]*)", _read("CLI.md")))
    assert named <= real, sorted(named - real)
