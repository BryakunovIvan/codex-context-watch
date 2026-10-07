"""Conservative, non-executing inspection of tool arguments."""
import ast
import json
import posixpath
import re
import shlex


PATH_TOKEN = re.compile(r'[^\s"\'`<>;,{}()\\]+')
NESTED_TOOL = re.compile(r'\btools\.([\w]+)\s*\(')
CMD_LITERAL = re.compile(r'\b(?:cmd|command)\s*:\s*("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')')
READ_COMMAND = re.compile(r'\b(?:cat|sed|head|tail|less|more|rg)\s+(?![<>])|\.(?:read_text|read_bytes)\s*\(')


def tool_arguments(name, source):
    """Yield direct or statically visible nested calls; never evaluate code."""
    if name not in ('functions.exec', 'exec'):
        yield name, source
        return
    masked = mask_strings(source)
    for match in NESTED_TOOL.finditer(masked):
        start = match.end()
        depth, end = 1, start
        while end < len(masked) and depth:
            depth += (masked[end] == '(') - (masked[end] == ')')
            end += 1
        if not depth:
            yield match[1], source[start:end - 1]


def static_literal(source):
    source = source.strip()
    try:
        if source.startswith('"'):
            value = json.loads(source)
        elif source.startswith("'"):
            value = ast.literal_eval(source)
        elif source.startswith('`') and source.endswith('`') and '${' not in source:
            # Plain multiline templates are common for patches. Escaped templates
            # require JS semantics; omit them rather than interpreting executable code.
            value = source[1:-1] if '\\' not in source[1:-1] and '`' not in source[1:-1] else None
        else:
            return None
        return value if isinstance(value, str) else None
    except (ValueError, SyntaxError):
        return None


def static_field(source, key, default=None):
    try:
        parsed = json.loads(source)
        if isinstance(parsed, dict):
            if key not in parsed:
                return default
            value = parsed.get(key)
            return value if isinstance(value, str) else None
    except ValueError:
        pass
    # Only inspect keys outside strings/comments (not code embedded in a patch).
    masked = mask_strings(source)
    field = re.compile(r'\b' + re.escape(key) + r'\s*:')
    for match in field.finditer(masked):
        rest = source[match.end():].lstrip()
        if not rest or rest[0] not in ('"', "'", '`'):
            return None
        quote, end = rest[0], 1
        while end < len(rest):
            if rest[end] == '\\':
                end += 2
            elif rest[end] == quote:
                after = rest[end + 1:].lstrip()
                if after and after[0] not in ',}':
                    return None
                return static_literal(rest[:end + 1])
            else:
                end += 1
    return default


def file_path(path, cwd, literal=False):
    # Variables, expansions and globs cannot be resolved from arguments alone.
    if not path:
        return None
    if not literal and (path == '-' or any(c in path for c in '$`*?[]') or path.startswith('~')):
        return None
    if posixpath.isabs(path):
        return posixpath.normpath(path)
    return posixpath.normpath(posixpath.join(cwd, path)) if cwd else posixpath.normpath(path)


def command_read_paths(command):
    """Recognize literal operands of a small set of shell reading commands."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|()<>\n')
        lexer.whitespace_split = True
        lexer.whitespace = ' \t\r'
        lexer.commenters = ''
        tokens = list(lexer)
    except ValueError:
        return []
    # Heredocs and subshells can contain code or data that look like commands.
    if any('<<' in t or t in ('(', ')') for t in tokens):
        return []
    paths, segment, comment = [], [], False
    for token in tokens + [';']:
        if comment and '\n' not in token:
            continue
        if token.strip('\n') in (';', '&&', '||', '|', '&', ''):
            if segment:
                paths.extend(read_operands(segment))
            segment = []
            comment = False
        elif token.startswith('#'):
            comment = True
        else:
            segment.append(token)
    if segment:
        paths.extend(read_operands(segment))
    # A preceding cd changes the meaning of relative paths; leave this command
    # unclassified until shell control flow can be represented safely.
    if any(t == 'cd' for t in tokens):
        return []
    return paths


def read_operands(tokens):
    command = posixpath.basename(tokens[0])
    if command not in ('cat', 'sed', 'head', 'tail', 'less', 'more', 'rg'):
        return []
    if command == 'sed' and any(t.startswith(('-i', '--in-place')) for t in tokens[1:]):
        return []
    value_flags = {
        'head': {'-n', '-c', '--lines', '--bytes'},
        'tail': {'-n', '-c', '-s', '--lines', '--bytes', '--sleep-interval', '--pid'},
        'sed': {'-e', '--expression', '-f', '--file'},
        'rg': {'-e', '--regexp', '-f', '--file', '-g', '--glob', '--iglob', '-t', '--type',
               '-T', '--type-not', '-m', '--max-count', '--max-depth', '-A', '-B', '-C',
               '--after-context', '--before-context', '--context', '-j', '--threads',
               '--encoding', '--sort', '--sortr', '--ignore-file', '--replace', '-r'},
    }.get(command, set())
    operands, skip, literal = [], False, False
    has_expression = command in ('rg', 'sed') and any(
        t in ('-e', '-f', '--regexp', '--expression', '--file') or
        t.startswith(('--regexp=', '--expression=', '--file=')) or
        (t.startswith(('-e', '-f')) and len(t) > 2) for t in tokens[1:])
    has_expression |= command == 'rg' and '--files' in tokens
    needs_expression = command in ('rg', 'sed') and not has_expression
    for index, token in enumerate(tokens[1:], 1):
        if skip:
            skip = False
            continue
        if token and token[0] in '<>':
            skip = True
            continue
        if token.isdigit() and index + 1 < len(tokens) and tokens[index + 1].startswith(('<', '>')):
            continue
        if not literal and token == '--':
            literal = True
            continue
        if not literal and token.startswith('-') and token != '-':
            skip = token in value_flags
            continue
        if needs_expression:
            needs_expression = False
            continue
        operands.append(token)
    return operands


def analyze_files(name, source, cwd=''):
    accesses = []
    for tool, arguments in tool_arguments(name, source):
        tool = tool.rsplit('.', 1)[-1]
        if tool.endswith('exec_command') or tool in ('shell', 'run_command'):
            command = static_field(arguments, 'cmd') or static_field(arguments, 'command')
            workdir = static_field(arguments, 'workdir', default='')
            if workdir is None:
                continue
            base = file_path(workdir, cwd) if workdir else cwd
            if command is not None and base is not None:
                accesses.extend((p, 'read', base) for p in command_read_paths(command))
        elif tool == 'apply_patch':
            patch = arguments if arguments.startswith('*** Begin Patch') else static_literal(arguments)
            if patch is None:
                patch = static_field(arguments, 'patch') or static_field(arguments, 'input')
            if not patch or not patch.startswith('*** Begin Patch\n'):
                continue
            previous = None
            for line in patch.splitlines()[1:]:
                if line == '*** End Patch':
                    break
                for header, operation in (('*** Add File: ', 'create'),
                                          ('*** Update File: ', 'modify'),
                                          ('*** Delete File: ', 'delete')):
                    if line.startswith(header):
                        previous = (line[len(header):], operation, cwd)
                        accesses.append(previous)
                        break
                if line.startswith('*** Move to: ') and previous and previous[1] == 'modify':
                    accesses[-1] = (previous[0], 'move_from', cwd)
                    accesses.append((line[len('*** Move to: '):], 'move_to', cwd))
                    previous = None
    result = []
    for path, operation, base in accesses:
        path = file_path(path, base, literal=operation != 'read')
        access = {'path': path, 'operation': operation, 'attribution': 'inferred'}
        if path is not None and access not in result:
            result.append(access)
    return result


def mask_strings(source):
    """Preserve offsets while masking strings/comments, including quoted code."""
    chars = list(source)
    i = 0
    while i < len(source):
        start = i
        if source[i] in ('"', "'", '`'):
            quote = source[i]
            i += 1
            while i < len(source):
                if source[i] == '\\':
                    i += 2
                elif source[i] == quote:
                    i += 1
                    break
                else:
                    i += 1
        elif source[i:i + 2] == '//':
            end = source.find('\n', i)
            i = len(source) if end < 0 else end
        elif source[i:i + 2] == '/*':
            end = source.find('*/', i + 2)
            i = len(source) if end < 0 else end + 2
        else:
            i += 1
            continue
        for index in range(start, min(i, len(chars))):
            if chars[index] != '\n':
                chars[index] = ' '
    return ''.join(chars)


def command_text(argument):
    try:
        parsed = json.loads(argument)
        if isinstance(parsed, dict):
            value = parsed.get('cmd', parsed.get('command', ''))
            return value if isinstance(value, str) else ''
    except ValueError:
        pass
    match = CMD_LITERAL.search(argument)
    if match:
        try:
            return json.loads(match[1]) if match[1][0] == '"' else ast.literal_eval(match[1])
        except (ValueError, SyntaxError):
            return ''
    return ''


def analyze_call(name, source):
    nested, commands = [], []
    if name.endswith('exec_command') or name in ('shell', 'run_command'):
        commands.append(command_text(source) or source)
    if name in ('functions.exec', 'exec'):
        masked = mask_strings(source)
        for match in NESTED_TOOL.finditer(masked):
            tool = match[1]
            nested.append(tool)
            if tool.endswith('exec_command') or tool in ('shell', 'run_command'):
                start = match.end()
                depth, end = 1, start
                while end < len(masked) and depth:
                    depth += (masked[end] == '(') - (masked[end] == ')')
                    end += 1
                commands.append(command_text(source[start:end - 1]))
    paths, resources = [], []
    for command in commands:
        if READ_COMMAND.search(command):
            # Tokenize once and check suffixes. A suffix-heavy path regex performs
            # quadratic backtracking on long shell commands with no Markdown path.
            try:
                lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|()<>')
                lexer.whitespace_split = True
                tokens = list(lexer)
            except ValueError:
                tokens = PATH_TOKEN.findall(command)
            found = [token for token in tokens if token.endswith('.md')]
            paths.extend(token for token in found if token == 'SKILL.md' or token.endswith('/SKILL.md'))
            resources.extend(found)
    for resource in resources:
        if '/skills/' in resource and '/references/' in resource:
            paths.append(resource.split('/references/', 1)[0] + '/SKILL.md')
    paths = list(dict.fromkeys(paths))
    shared = len(paths) > 1 or (paths and len(nested) > 1)
    return (list(dict.fromkeys(nested)), paths,
            'shared/inferred' if shared else 'inferred' if paths else 'none',
            list(dict.fromkeys(resources)))
