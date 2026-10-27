"""Heuristic UNKNOWN-keeps guard. Baseline entries are stable AST signatures, not line numbers.

The scan is deliberately local: direct pid_state calls and simple assigned aliases, lexical
free/remove/release calls (and FIFO filtering via continue), and literal absent/free returns
under broad exception handlers. It is a regression aid, not control-flow proof.
"""
import ast
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_support  # noqa: E402; isolate even when this module is run alone

ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path(__file__).with_name('invariant-lint-baseline.txt')
SENSITIVE = re.compile(r'claim|release|free|dead|gone', re.I)
FREE_CALL = re.compile(r'free|release|remove|unlink|rmdir|discard|pop|clear', re.I)


def name(node):
    return node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ''


def local_nodes(node):
    yield node
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            yield from local_nodes(child)


def pid_value(node, aliases):
    return (isinstance(node, ast.Call) and name(node.func).lstrip('_') == 'pid_state'
            or isinstance(node, ast.Name) and node.id in aliases)


def absent(node):
    if isinstance(node, ast.Constant):
        return node.value is None or isinstance(node.value, bool) or node.value in (0, '', 'free', 'absent', 'dead', 'gone')
    return (isinstance(node, (ast.List, ast.Tuple, ast.Set)) and not node.elts
            or isinstance(node, ast.Dict) and not node.keys
            or name(node) == 'DEAD')


def free_actions(nodes, sensitive):
    """What a subtree does that could free custody; part of every signature, so a baselined branch that
    gains a release (say claim.unlink()) no longer matches its accepted entry."""
    acts = set()
    for root in nodes:
        for n in local_nodes(root):
            if isinstance(n, ast.Call) and FREE_CALL.search(name(n.func)):
                acts.add(name(n.func) + '()')
            elif isinstance(n, ast.Continue):
                acts.add('continue')
            elif sensitive and isinstance(n, ast.Return) and (n.value is None or absent(n.value)):
                acts.add('return-absent')
    return sorted(acts)


def frees(node, sensitive):
    return bool(free_actions([node], sensitive))


def exits(block):
    return bool(block) and isinstance(block[-1], (ast.Return, ast.Continue, ast.Raise, ast.Break))


def alive_compare(test, aliases, ops):
    for c in ast.walk(test) if test is not None else []:
        if isinstance(c, ast.Compare) and len(c.ops) == 1 and isinstance(c.ops[0], ops):
            operands = [c.left, *c.comparators]
            if any(name(x) == 'ALIVE' for x in operands) and any(pid_value(x, aliases) for x in operands):
                return c
    return None


def scan_source(source, filename='fixture.py'):
    tree = ast.parse(source)
    hits = set()

    def visit(container, prefix=''):
        for fn in ast.iter_child_nodes(container):
            if isinstance(fn, ast.ClassDef):
                visit(fn, prefix + fn.name + '.')
            elif isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = prefix + fn.name
                nodes = list(local_nodes(fn))
                aliases = set()
                for _ in range(len(nodes)):
                    before = set(aliases)
                    for n in nodes:
                        if isinstance(n, (ast.Assign, ast.AnnAssign)) and pid_value(n.value, aliases):
                            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                            aliases.update(t.id for t in targets if isinstance(t, ast.Name))
                    if before == aliases:
                        break
                sensitive = bool(SENSITIVE.search(fn.name))
                for n in nodes:
                    if isinstance(n, (ast.If, ast.IfExp, ast.Return)):
                        test = n.value if isinstance(n, ast.Return) else n.test
                        for c in ast.walk(test) if test is not None else []:
                            if not isinstance(c, ast.Compare):
                                continue
                            operands = [c.left, *c.comparators]
                            if any(name(x) == 'ALIVE' for x in operands) and any(pid_value(x, aliases) for x in operands) \
                                    and (frees(n, sensitive) or sensitive and isinstance(n, ast.Return)):
                                acts = ','.join(free_actions([n], sensitive)) or 'return'
                                hits.add(f'{filename}:{qualified}:alive-free:{ast.unparse(c)} -> {acts}')
                    # Guard clause: `if state == ALIVE: return` then a release later in the same block lets
                    # UNKNOWN fall through to the release.
                    for field in ('body', 'orelse', 'finalbody'):
                        block = getattr(n, field, None)
                        if not isinstance(block, list):
                            continue
                        for i, stmt in enumerate(block):
                            c = isinstance(stmt, ast.If) and exits(stmt.body) \
                                and alive_compare(stmt.test, aliases, (ast.Eq, ast.Is))
                            acts = c and free_actions(block[i + 1:], sensitive)
                            if acts:
                                hits.add(f'{filename}:{qualified}:alive-guard-free:{ast.unparse(c)} -> {",".join(acts)}')
                    if isinstance(n, ast.ExceptHandler) and (n.type is None or name(n.type) in ('Exception', 'TimeoutError')):
                        for ret in (x for stmt in n.body for x in local_nodes(stmt) if isinstance(x, ast.Return)):
                            if sensitive and (ret.value is None or absent(ret.value)):
                                handler = ast.unparse(n.type) if n.type else 'bare'
                                hits.add(f'{filename}:{qualified}:except-absent:{handler} -> {ast.unparse(ret)}')
                visit(fn, qualified + '.')
    visit(tree)
    return hits


def scan_tree():
    return set().union(*(scan_source(p.read_text(encoding='utf-8'), 'twinlane/' + p.name)
                         for p in sorted((ROOT / 'twinlane').glob('*.py'))))


class InvariantLintTests(unittest.TestCase):
    def test_baseline_only_shrinks(self):
        baseline = {line.strip() for line in BASELINE.read_text(encoding='utf-8-sig').splitlines()
                    if line.strip() and not line.startswith('#')}
        current = scan_tree()
        self.assertEqual(current - baseline, set(), 'New UNKNOWN-keeps lint hits; inspect and fix the code')
        self.assertEqual(baseline - current, set(), 'Stale baseline hits; shrink the baseline alongside the fix')

    def test_alive_free_patterns_and_aliases(self):
        for condition in ('pid_state(pid) != ALIVE', 'common.pid_state(pid) == common.ALIVE', 'state != common.ALIVE'):
            source = 'def release_claim():\n    state = common.pid_state(pid)\n    if ' + condition + ':\n        lock.unlink()\n'
            self.assertTrue(scan_source(source), condition)
        self.assertTrue(scan_source('def is_gone():\n    return pid_state(pid) != ALIVE\n'))
        self.assertTrue(scan_source('def free_claim():\n    if pid_state(pid) == ALIVE:\n        return False\n    else:\n        remove(path)\n'))

    def test_alive_guard_fallthrough_to_a_release_is_caught(self):
        for exit_stmt in ('return', 'return None', 'continue', 'raise Busy()'):
            body = '    if state == ALIVE:\n        ' + exit_stmt + '\n    lock.unlink()\n'
            if exit_stmt == 'continue':
                body = '    for pid in pids:\n        state = pid_state(pid)\n        if state == ALIVE:\n            continue\n        lock.unlink()\n'
            source = 'def cleanup():\n    state = pid_state(pid)\n' + body
            self.assertTrue(scan_source(source), exit_stmt)
        self.assertFalse(scan_source('def cleanup():\n    state = pid_state(pid)\n    if state != DEAD:\n        return\n    lock.unlink()\n'))
        self.assertFalse(scan_source('def cleanup():\n    if pid_state(pid) == ALIVE:\n        return\n    log(pid)\n'))

    def test_baselined_branch_that_gains_a_release_no_longer_matches(self):
        accepted = 'def _waiters():\n    for st in rows:\n        if _pid_state(st.get("pid")) != common.ALIVE:\n            continue\n        keep(st)\n'
        widened = accepted.replace('            continue\n', '            claim.unlink()\n            continue\n')
        self.assertTrue(scan_source(accepted))
        self.assertFalse(scan_source(accepted) & scan_source(widened))

    def test_dead_only_and_unknown_keep_are_clean(self):
        self.assertFalse(scan_source('def release_claim():\n    if pid_state(pid) == DEAD:\n        lock.unlink()\n'))
        self.assertFalse(scan_source('def release_claim():\n    try:\n        probe()\n    except Exception:\n        return UNKNOWN\n'))
        self.assertFalse(scan_source('def pid_alive():\n    return pid_state(pid) == ALIVE\n'))

    def test_broad_exception_absence(self):
        for handler in ('Exception', 'TimeoutError', ''):
            for value in ('True', 'False', 'None', '[]', '{}', 'DEAD', "'gone'"):
                code = 'def claim_free():\n    try:\n        probe()\n    except' + (' ' + handler if handler else '') + ':\n        return ' + value + '\n'
                self.assertTrue(scan_source(code), (handler, value))
        self.assertFalse(scan_source('def release_claim():\n    try:\n        probe()\n    except FileNotFoundError:\n        return True\n'))


if __name__ == '__main__':
    unittest.main()
