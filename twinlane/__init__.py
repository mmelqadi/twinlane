"""Coordinate Claude Code and Codex CLI on local included subscriptions."""

__version__ = '0.2.0'


def main(argv=None):
    from .cli import main as cli_main
    return cli_main(argv)
