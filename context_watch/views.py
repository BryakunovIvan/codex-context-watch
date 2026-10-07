"""Pure view models shared by the curses interface and its behavior tests."""
from .display import CATEGORY, file_operations, filtered_files, fmt, matches, short, size


VIEWS = [('largest', '1 Размер'), ('tools', '2 Инструменты'), ('skills', '3 Скиллы'),
         ('categories', '4 Категории'), ('timeline', '5 События'), ('files', '6 Файлы')]


def rows_for(report, view, query='', file_mode='all'):
    entries = [e for e in report['entries'] if matches(e, query)]
    rows = []
    if view in ('largest', 'timeline'):
        ordered = sorted(entries, key=lambda e: e['bytes'], reverse=True) if view == 'largest' else entries
        for e in ordered:
            rows.append({'key': e['id'], 'entry': e, 'line': e['line'],
                'text': f"{e['id']} L{e['line']:<6} {size(e['bytes']):>9} ≈{fmt(e['estimated_tokens']):>8}  {short(e['label'], 160)}"})
        if view == 'timeline':
            for event in report['timeline']:
                if not query or query.casefold() in event['type'].casefold():
                    rows.append({'key': 'event-' + str(event['line']), 'event': event,
                                 'line': event['line'], 'text': f"L{event['line']:<6} *** {event['type']} | {event['timestamp']} ***"})
            rows.sort(key=lambda row: row['line'])
    elif view == 'tools':
        for tool in report['tools']:
            if not query or any(e['tool'] == tool['name'] for e in entries):
                rows.append({'key': 'tool-' + tool['name'], 'tool': tool['name'],
                    'text': f"{tool['name']:30} вызовов {tool['calls']:3} / ответов {tool['output_count']:3}  {size(tool['argument_bytes'] + tool['output_bytes']):>9} ≈{fmt(tool['estimated_tokens'])}"})
        for tool in report.get('nested_tools', []):
            if not query or any(tool['name'] in e['nested_tools'] for e in entries):
                rows.append({'key': 'nested-' + tool['name'], 'nested_tool': tool['name'],
                    'text': f"  ↳ {tool['name']:26} в {tool['calls']} блоках кода | общий вес ≈{fmt(tool['estimated_tokens'])} [предположение]"})
    elif view == 'skills':
        for skill in report['skills']:
            if not query or any(skill['path'] in e['skill_paths'] for e in entries):
                note = 'ОБЩИЙ вывод' if skill['shared'] else 'вывод вызова'
                rows.append({'key': 'skill-' + skill['path'], 'skill': skill['path'],
                    'text': f"{skill['name']:30} чтений? {skill['calls']:2}  {size(skill['output_bytes']):>9} ≈{fmt(skill['estimated_tokens']):>8} {note} | {skill['path']}"})
    elif view == 'files':
        for file in filtered_files(report, query, file_mode):
            rows.append({'key': 'file-' + file['path'], 'file': file['path'],
                'operations': file['operations'],
                'text': f"{file['path']} | {file_operations(file['operations'])} | блоков {file['calls']} [предположение]"})
    elif view == 'categories':
        for category in report['categories']:
            if not query or any(e['category'] == category['name'] for e in entries):
                rows.append({'key': 'category-' + category['name'], 'category': category['name'],
                    'text': f"{CATEGORY.get(category['name'], category['name']):30} {category['count']:4} записей  {size(category['bytes']):>9} ≈{fmt(category['estimated_tokens'])}"})
    return rows


def related_entries(report, row):
    if 'entry' in row:
        return [row['entry']]
    return [e for e in report['entries'] if
            ('tool' in row and e['tool'] == row['tool']) or
            ('nested_tool' in row and row['nested_tool'] in e['nested_tools']) or
            ('skill' in row and row['skill'] in e['skill_paths']) or
            ('file' in row and any(a['path'] == row['file'] for a in e.get('file_accesses', []))) or
            ('category' in row and row['category'] == e['category'])]
