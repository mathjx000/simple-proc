#!/usr/bin/python3

import argparse
import glob
import mimetypes
import os
import re

parser = argparse.ArgumentParser()
parser.add_argument("-v", "--variable", metavar="key=value", action="append", type=lambda s: s.split('=', maxsplit=1))
parser.add_argument("-o", "--output", nargs=1, required=True)
parser.add_argument("files", nargs="+") # , type=argparse.FileType('rt')
args = parser.parse_args()

MIME_2_DELIMITERS: dict[str | None, tuple[str, str]] = {
    "text/x-python": (r'"""@@', r'@@"""'),
    "text/javascript": (r"\/\*@", r"@\*\/"),

    "text/html": (r"<!--@", r"@-->"),
    "text/xml": (r"<!--@", r"@-->"),
}

COMMON_TOKEN_RULES = [
    ("variable", r"\$\w+"),
    ("macro", r"[a-zA-Z]\w*"),
    ("literal", r'\d+(?:\.\d+)?|"(?:\\?.)*?"'),
    ("group_open", r"\("),
    ("group_close", r"\)"),
    ("whitespace", r"\s+"),
    ("error", r"\S+")
]

def make_tokenizer_pattern(mime: str | None):
    open, close = MIME_2_DELIMITERS.get(mime, (r"\{@", r"@\}"))
    return re.compile(rf"(?P<block_end>{close})|" + (r'|'.join(r"(?P<%s>%s)" % pair for pair in COMMON_TOKEN_RULES)), 0), re.compile(open)

def tokenizer(token_pat: re.Pattern, input: str, endpos: list[int], pos: int = 0):
    for match in token_pat.finditer(input, pos=pos):
        endpos[0] = match.span(0)[1]
        kind = match.lastgroup
        # if kind is None: raise RuntimeError()
        value = match.group()

        match kind:
            case "variable":
                value = value[1:]
            case "literal":
                if value.startswith('"'):
                    value = value[1:-1].replace("\\\"", '"')
                else:
                    value = float(value) if '.' in value else int(value)
            case "whitespace":
                continue
            case "block_end":
                return
            case "error":
                raise RuntimeError(f"syntax error: {value}")

        yield (kind, value)

    raise RuntimeError("unexpected end of file")

class PeekIter:
    def __init__(self, inner) -> None:
        self._inner = inner
        self._peeked = None
        self._hasPeeked = False

    def __iter__(self):
        return self

    def __next__(self):
        val = self.peek()
        self._hasPeeked = False
        self._peeked = None
        return val

    def peek(self):
        self.has_next()
        return self._peeked

    def has_next(self):
        if not self._hasPeeked:
            try:
                self._peeked = next(self._inner)
                self._hasPeeked = True
            except StopIteration:
                return False
        return True


MACROS = {}

def macro_debug(*args):
    print("debug:", ' '.join(map(str, args)))
    yield from []
MACROS["dbg"] = macro_debug

def macro_concat(*args):
    yield "".join(map(str, args))
MACROS["concat"] = macro_concat

for macro_name, reducer in {
    "add": lambda a, b: a + b,
    "sub": lambda a, b: a - b,
    "mul": lambda a, b: a * b,
    "div": lambda a, b: a / b,
}.items():
    def macro_reduce(*args):
        args = iter(args)

        try:
            val = next(args)
        except StopIteration:
            raise RuntimeError(f"macro {macro_name} expected at least one argument")

        for v in args:
            val = reducer(val, v)

        yield val

    MACROS[macro_name] = macro_reduce

def eval_group_untill_end(tokens):
    yield from eval_body(tokens)

    try:
        token = next(tokens)
    except StopIteration:
        raise RuntimeError(f"unclosed group")

    if token[0] != "group_close":
        raise RuntimeError(f"unexpected token {token}")

def eval_body(tokens):
    while tokens.has_next():
        token = tokens.peek()
        if token[0] == "group_close":
            break

        token = next(tokens)
        match token[0]:
            case "group_open":
                yield from eval_group_untill_end(tokens)
            case "variable":
                name = token[1]
                try:
                    global state
                    yield state.variables[name]
                except KeyError:
                    raise RuntimeError(f"variable not found: '{name}'")
            case "literal":
                yield token[1]
            case "macro":
                name = token[1]
                try:
                    yield from MACROS[name](*eval_body(tokens))
                except KeyError:
                    raise RuntimeError(f"macro '{name}' does not exists")

def macro_include_eval(pathname):
    global state

    parent_state = state
    state = State()
    state.variables = parent_state.variables

    pathname = os.path.join(parent_state.relpath, str(pathname))
    state.relpath = os.path.dirname(pathname)

    with open(pathname, mode="rt") as file:
        src_lines = file.readlines()

    mime, _ = mimetypes.guess_type(os.path.basename(pathname))
    token_pattern, block_start = make_tokenizer_pattern(mime)

    dst_lines: list[str] = []
    for line in src_lines:
        dst_parts = []
        last_column = 0

        for start in block_start.finditer(line):
            span = start.span(0)
            if last_column < span[0]:
                dst_parts.append(line[last_column:span[0]])

            endpos_ref = [span[1]]
            tokens = PeekIter(tokenizer(token_pattern, line, endpos_ref, endpos_ref[0]))

            parts = tuple(map(str, eval_body(tokens)))
            if last_column == 0 and not state.emitoutline:
                dst_parts.clear()
            dst_parts += parts

            last_column = endpos_ref[0]

        if state.emitlines and True:
            dst_lines += dst_parts

            if last_column < len(line):
                if state.emitoutline:
                    dst_lines.append(line[last_column:])
                elif line.endswith('\n'):
                    dst_lines.append('\n')

        state.emitoutline = True

    state = parent_state

    yield from dst_lines
MACROS["include_eval"] = macro_include_eval

def macro_include(pathname):
    global state

    pathname = os.path.join(state.relpath, os.path.normpath(str(pathname)))
    with open(pathname, mode="rt") as file:
        lines = file.readlines()

    yield from lines
MACROS["include"] = macro_include

def macro_no_outline():
    global state
    state.emitoutline = False
    yield from []
MACROS["no_outline"] = macro_no_outline

def macro_repeat(n, *args):
    for _ in range(int(n)):
        yield from args
MACROS["repeat"] = macro_repeat

def macro_separated(s, *args):
    separate = False
    for arg in args:
        if separate:
            yield s
        separate = True
        yield arg
MACROS["separated"] = macro_separated

def process_file(source: str, destination: str):
    print(f"processing '{source}' -> '{destination}'")

    lines = macro_include_eval(source)

    try:
        line = next(lines)
    except StopIteration:
        return

    dir = os.path.dirname(destination)
    if len(dir) > 0:
        os.makedirs(dir, exist_ok=True)

    with open(destination, "wt") as f:
        f.write(line)
        f.writelines(lines)

class State:
    def __init__(self) -> None:
        self.variables = {}
        self.emitlines = True
        self.emitoutline = True
        self.relpath = ""

    def __str__(self) -> str:
        return f"variables={self.variables}, emitlines={self.emitlines}, relpath={self.relpath}"

state = State()
state.variables = dict(args.variable) if args.variable is not None else {}

output = args.output[0]

os.makedirs(output, exist_ok=True)

IGNORE_FILE_NAME = ".procignore"

for arg_pathname in args.files:
    arg_pathname = os.path.normpath(arg_pathname)

    if os.path.isfile(arg_pathname):
        process_file(arg_pathname, os.path.join(output, os.path.basename(arg_pathname)))
    elif not os.path.exists(arg_pathname):
        raise RuntimeError(f"{arg_pathname}: file or directory does not exists")
    else:
        ignore_stack: dict[str, re.Pattern] = {}
        lastdir = ""
        for path, dirs, files in os.walk(arg_pathname):
            while not path.startswith(lastdir):
                lastdir, tail = os.path.split(lastdir)
                ignore_stack.pop(tail, None)

            ignore = ignore_stack.get(lastdir, None)
            lastdir = path

            try:
                files.remove(IGNORE_FILE_NAME)

                with open(os.path.join(path, IGNORE_FILE_NAME), mode="rt") as fignore:
                    lines = list(filter(lambda l: len(l) > 0, map(str.strip, filter(lambda l: not l.startswith('#'), fignore.readlines()))))

                ignore = re.compile((ignore.pattern + '|' if ignore is not None else "") + r'|'.join(map(
                    lambda p: glob.translate(p, recursive=True, include_hidden=True),
                    # TODO: test on Windows
                    map(os.path.normpath, lines))
                ))
            except ValueError:
                pass

            ignore_stack[path] = ignore

            for file in files:
                relpathname = os.path.join(os.path.relpath(path, start=arg_pathname), file)

                if ignore is None or ignore.search(relpathname) is None:
                    process_file(
                        os.path.join(path, file),
                        os.path.join(output, relpathname)
                    )
