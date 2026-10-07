"""Shared terminal formatting; untrusted log text cannot emit terminal controls."""
import json
import unicodedata


CATEGORY = {'base_instructions': 'Базовые инструкции', 'instructions': 'Инструкции',
            'user': 'Пользователь', 'assistant': 'Ассистент', 'tool_call': 'Аргументы вызовов',
            'tool_output': 'Результаты инструментов', 'reasoning': 'Резюме рассуждений',
            'compaction_summary': 'Резюме сжатия'}
FILE_OPERATIONS = {'read': 'Чтение', 'create': 'Создание', 'modify': 'Изменение',
                   'delete': 'Удаление', 'move_from': 'Перемещение: источник',
                   'move_to': 'Перемещение: назначение'}
FILE_MODES = {'all': 'все файлы', 'md': 'только .md'}


def file_operations(operations):
    return ', '.join(FILE_OPERATIONS.get(op, op) for op in operations)


LIMITATIONS = {
    'opaque_compaction': 'Сжатие содержит непрозрачный блок; его размер в токенах неизвестен.',
    'incomplete_compaction': 'Полная история после сжатия не записана; восстановление частичное.',
    'opaque_reasoning': 'Зашифрованные рассуждения не включены в оценку текста.',
    'multimodal_unmeasured': 'Токены изображений, аудио и файлов не оценивались.',
    'base_instructions_from_metadata': 'Базовые инструкции взяты из метаданных; их включение в запрос не подтверждено.',
}


def safe(text):
    result = []
    for char in str(text):
        if char in ('\n', '\t') or unicodedata.category(char)[0] != 'C':
            result.append(char)
        else:
            code = ord(char)
            result.append(f'\\x{code:02x}' if code < 256 else f'\\u{code:04x}')
    return ''.join(result)


def short(text, width=100):
    text = ' '.join(safe(text).split())
    return text if len(text) <= width else text[:max(0, width - 1)] + '…'


def fmt(number):
    return f'{number:,}'.replace(',', ' ') if isinstance(number, (int, float)) else '—'


def size(value):
    if value >= 1024 * 1024:
        return f'{value / (1024 * 1024):.1f} MiB'
    if value >= 1024:
        return f'{value / 1024:.1f} KiB'
    return f'{value} B'


def matches(entry, query):
    needle = query.casefold()
    return not needle or any(needle in str(entry.get(field, '')).casefold()
                             for field in ('text', 'tool', 'label', 'category', 'call_id')) or any(
                             needle in p.casefold() for p in entry.get('skill_paths', [])) or any(
                             needle in a['path'].casefold() for a in entry.get('file_accesses', []))


def filtered_files(report, query='', file_mode='all'):
    files = report.get('files', [])
    if query:
        paths = [f for f in files if query.casefold() in f['path'].casefold()]
        if paths:
            files = paths
        else:
            entry_ids = {e['id'] for e in report['entries'] if matches(e, query)}
            files = [f for f in files if entry_ids.intersection(f['entry_ids'])]
    return [f for f in files if f['path'].casefold().endswith('.md')] if file_mode == 'md' else files


def report_json(report, full=False):
    output = dict(report)
    output['entries'] = [dict(e) if full else {k: v for k, v in e.items() if k != 'text'}
                         for e in report['entries']]
    return json.dumps(output, ensure_ascii=False)


def diagnostic_lines(report):
    lines = ['Оценка текста не включает невидимые схемы инструментов и служебную упаковку запроса.',
             'Размеры скиллов могут перекрываться: общий вывод exec не распределён между чтениями.',
             'Файлы распознаны по аргументам чтения и apply_patch; выполнение и успех не подтверждены.',
             'Произвольные shell-команды, переменные и динамические пути файлов не восстановлены.']
    lines.extend(LIMITATIONS.get(key, key) for key in report['limitations'])
    if report['malformed_lines']:
        lines.append(f"Пропущено повреждённых строк JSON: {report['malformed_lines']}")
    if report.get('invalid_records'):
        lines.append(f"Пропущено записей с неверными типами полей: {report['invalid_records']}")
    if report['unknown_records']:
        lines.append('Неизвестные типы (не входят в оценку): ' + str(report['unknown_records']))
    if report.get('pending_bytes'):
        lines.append(f"Незавершённая строка в буфере: {report['pending_bytes']} байт")
    if report.get('file_resets'):
        lines.append(f"Перечитываний после перезаписи файла: {report['file_resets']}")
    return lines


def diagnostic_summary(report):
    notes = []
    if 'incomplete_compaction' in report['limitations']:
        notes.append('частичное восстановление')
    if report['unknown_records']:
        notes.append(f"неизвестных: {sum(report['unknown_records'].values())}")
    skipped = report['malformed_lines'] + report.get('invalid_records', 0)
    if skipped:
        notes.append(f'повреждённых: {skipped}')
    if 'opaque_compaction' in report['limitations']:
        notes.append('непрозрачное сжатие')
    if 'multimodal_unmeasured' in report['limitations']:
        notes.append('медиа без оценки')
    return ('Данные: ' + '; '.join(notes) if notes else '≈ UTF-8/4; общие ответы перекрываются') + ' | d: диагностика'


def heading(report):
    usage = report['usage']
    lines = [f"Codex Context Watch | {report['metadata'].get('id', '?')} | {report.get('model') or '?'}",
             f"История: {report['scope']} | записей: {len(report['entries'])} | сжатий: {report['compactions']}"]
    if usage:
        percent = usage.get('input_percent')
        percent_text = f' ({percent:.1f}% окна)' if percent is not None else ''
        stale = ' [ДО СЖАТИЯ]' if usage.get('before_compaction') else ''
        lines.append(f"Последний запрос{stale}: вход {fmt(usage.get('input_tokens'))}{percent_text} / выход {fmt(usage.get('output_tokens'))} / кэш {fmt(usage.get('cached_input_tokens'))}")
        lines.append(f"Метрика: {usage.get('timestamp') or 'время неизвестно'} | окно запроса {fmt(usage.get('context_window_at_measurement'))} | новые записи после метрики ≈{fmt(usage.get('new_visible_tokens', 0))}")
        lines.append(f"Накопленный вход всех запросов: {fmt(usage.get('cumulative_input_tokens'))} (это расход, не размер контекста)")
    else:
        lines.append('Счётчиков запроса в логе пока нет.')
    lines.append(f"Наблюдаемый текст: {size(report['visible_bytes'])} / ≈{fmt(report['estimated_visible_tokens'])} токенов по UTF-8/4")
    return lines


def render_report(report, top=15, query='', file_mode='all'):
    lines = heading(report)
    lines += ['', 'Категории:']
    for row in report['categories']:
        lines.append(f"  {CATEGORY.get(row['name'], row['name']):27} {size(row['bytes']):>10}  ≈{fmt(row['estimated_tokens']):>9}  ({row['count']})")
    lines += ['', 'Крупнейшие записи — точный размер текста и приблизительный вес:']
    entries = sorted((e for e in report['entries'] if matches(e, query)), key=lambda e: e['bytes'], reverse=True)
    for e in entries[:top]:
        lines.append(f"  {e['id']}  L{e['line']:<6} {size(e['bytes']):>10}  ≈{fmt(e['estimated_tokens']):>8}  {short(e['label'], 95)}")
    if report['tools']:
        lines += ['', 'Инструменты (аргументы + ответы):']
        for t in report['tools'][:top]:
            lines.append(f"  {t['name']:35} вызовов {t['calls']:3} / ответов {t['output_count']:3}  {size(t['argument_bytes'] + t['output_bytes']):>10}  ≈{fmt(t['estimated_tokens'])}")
    if report.get('nested_tools'):
        lines += ['', 'Вложенные инструменты из кода exec — связанный общий вес, значения перекрываются:']
        for tool in report['nested_tools'][:top]:
            lines.append(f"  ↳ {tool['name']:32} в {tool['calls']:3} блоках  ≈{fmt(tool['estimated_tokens']):>9} [предположение]")
    if report['skills']:
        lines += ['', 'Возможные чтения скиллов — связь выведена из команд, успех чтения не гарантирован:']
        for skill in report['skills'][:top]:
            shared = 'ОБЩИЙ ответ — не складывать' if skill['shared'] else 'оценка связанного вызова'
            lines.append(f"  {skill['name']:30} ответ {size(skill['output_bytes']):>10}  ≈{fmt(skill['estimated_tokens']):>8}  {shared}")
            lines.append(f"    {skill['path']}")
            references = [p for p in skill.get('resources', []) if p != skill['path']]
            for resource in references:
                lines.append('    ресурс: ' + resource)
    if report.get('files'):
        lines += ['', f'Файлы ({FILE_MODES[file_mode]}) — предполагаемые операции из аргументов инструментов:']
        files = filtered_files(report, query, file_mode)
        if not files:
            lines.append('  Нет файлов для выбранного режима и поиска.')
        for file in files[:top]:
            lines.append(f"  {file_operations(file['operations'])} | блоков вызовов: {file['calls']} | {file['path']}")
    lines += [''] + diagnostic_lines(report)
    return safe('\n'.join(lines))


def entry_detail(entry, path):
    lines = [f"{entry['id']} | {entry['category']} | line={entry['line']} | {entry['timestamp']}",
             f"Лог: {path}",
             f"Текст: {entry['bytes']} байт / {entry['characters']} символов / ≈{entry['estimated_tokens']} токенов"]
    if entry['tool']:
        lines.append(f"Инструмент: {entry['tool']} | call_id: {entry['call_id']}")
    if entry['nested_tools']:
        lines.append('Вложенные вызовы (из кода): ' + ', '.join(entry['nested_tools']))
    if entry['skill_paths']:
        lines.append('Возможные чтения скиллов: ' + ', '.join(entry['skill_paths']))
        lines.append('Связь: ' + entry['attribution'])
    if entry.get('read_paths'):
        lines.append('Пути в командах чтения: ' + ', '.join(entry['read_paths']))
    for access in entry.get('file_accesses', []):
        lines.append(f"Файл (предположение): {file_operations([access['operation']])} | {access['path']}")
    if entry['media_blocks']:
        lines.append(f"Медиаблоков: {entry['media_blocks']} (не входят в оценку текста)")
    lines += ['', entry['text']]
    return safe('\n'.join(lines))
