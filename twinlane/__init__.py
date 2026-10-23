"""Coordinate Claude Code and Codex CLI on local included subscriptions."""

__version__ = '0.3.0.dev0'


def main(argv=None):
    from .cli import main as cli_main
    return cli_main(argv)
