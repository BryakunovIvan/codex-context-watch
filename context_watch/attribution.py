"""Conservative, non-executing inspection of tool arguments."""
import ast
import json
import re
import shlex


PATH_TOKEN = re.compile(r'[^\s"\'`<>;,{}()\\]+')
NESTED_TOOL = re.compile(r'\btools\.([\w]+)\s*\(')
CMD_LITERAL = re.compile(r'\b(?:cmd|command)\s*:\s*("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')')
READ_COMMAND = re.compile(r'\b(?:cat|sed|head|tail|less|more|rg)\s+(?![<>])|\.(?:read_text|read_bytes)\s*\(')


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
