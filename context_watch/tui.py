"""Interactive read-only watch. No terminal content is executed."""
import curses
import locale
from pathlib import Path
import textwrap
import time

from .cli import snapshot
from .discovery import discover
from .display import FILE_MODES, diagnostic_lines, diagnostic_summary, entry_detail, heading, safe, short
from .reader import RolloutReader
from .views import related_entries, rows_for, VIEWS


def put(screen, y, x, text, attr=0):
    height, width = screen.getmaxyx()
    if y < 0 or y >= height or x >= width - 1:
        return
    try:
        screen.addnstr(y, x, safe(text).replace('\n', ' '), max(0, width - x - 1), attr)
    except curses.error:
        pass


def prompt(screen, initial=''):
    value = initial
    while True:
        height, width = screen.getmaxyx()
        screen.move(height - 1, 0)
        screen.clrtoeol()
        put(screen, height - 1, 0, 'Поиск: ' + value)
        screen.refresh()
        key = screen.get_wch()
        if key in ('\n', '\r', curses.KEY_ENTER):
            return value
        if key == '\x1b':
            return initial
        if key in ('\b', '\x7f', curses.KEY_BACKSPACE):
            value = value[:-1]
        elif isinstance(key, str) and key.isprintable():
            value += key


def pager(screen, title, text):
    position = 0
    while True:
        height, width = screen.getmaxyx()
        lines = []
        for line in safe(text).expandtabs(4).splitlines():
            lines.extend(textwrap.wrap(line, max(10, width - 2), replace_whitespace=False,
                                       drop_whitespace=False) or [''])
        visible = max(1, height - 3)
        position = min(position, max(0, len(lines) - visible))
        screen.erase()
        put(screen, 0, 0, title, curses.A_BOLD)
        for y, line in enumerate(lines[position:position + visible], 1):
            put(screen, y, 0, line)
        put(screen, height - 1, 0, f'↑↓ PgUp/PgDn прокрутка | Home/End | q/Esc назад | {position + 1}/{len(lines)}', curses.A_REVERSE)
        screen.refresh()
        key = screen.get_wch()
        if key in ('q', '\x1b', '\n', '\r'):
            return
        if key in (curses.KEY_DOWN, 'j'):
            position += 1
        elif key in (curses.KEY_UP, 'k'):
            position = max(0, position - 1)
        elif key in (curses.KEY_NPAGE, ' '):
            position += visible
        elif key == curses.KEY_PPAGE:
            position = max(0, position - visible)
        elif key in (curses.KEY_HOME, 'g'):
            position = 0
        elif key in (curses.KEY_END, 'G'):
            position = max(0, len(lines) - visible)


def session_picker(screen, sessions):
    index, offset = 0, 0
    while True:
        height, width = screen.getmaxyx()
        visible = max(1, height - 3)
        index = max(0, min(index, len(sessions) - 1))
        offset = max(0, min(offset, index))
        if index >= offset + visible:
            offset = index - visible + 1
        screen.erase()
        put(screen, 0, 0, 'Выбор чата — Enter открыть, Esc отмена', curses.A_BOLD)
        for y, row in enumerate(sessions[offset:offset + visible], 1):
            put(screen, y, 0, f"{row['id']}  {short(row['title'] or row['cwd'], width)}",
                curses.A_REVERSE if offset + y - 1 == index else 0)
        if not sessions:
            put(screen, 2, 0, 'Сессии не найдены; Esc назад.')
        screen.refresh()
        key = screen.get_wch()
        if key in ('q', '\x1b'):
            return None
        if key in ('\n', '\r', curses.KEY_ENTER) and sessions:
            return sessions[index]
        if key in (curses.KEY_DOWN, 'j'):
            index += 1
        elif key in (curses.KEY_UP, 'k'):
            index -= 1
        elif key == curses.KEY_NPAGE:
            index += visible
        elif key == curses.KEY_PPAGE:
            index -= visible


HELP = '''Codex Context Watch

1–6 или ←→: размер / инструменты / скиллы / категории / события / файлы
↑↓ или j/k: выбрать запись; PgUp/PgDn: страница; Home/End: начало/конец
Enter: полное содержимое записи; у группы — связанные вызовы и ответы
/: поиск по полному тексту, инструменту, категории или пути файла/скилла
c: очистить поиск; h: история после сжатия / все исходные записи
m: в виде «Файлы» переключить все файлы / только .md (включая .MD)
s: выбрать другой чат; p: приостановить обновление; q: выход
d: диагностика неполных, неизвестных и повреждённых данных
?: эта справка

Верхняя метрика «вход» взята из последнего записанного запроса.
Накопленный расход — сумма повторных обработок истории, а не текущий контекст.
≈ — грубая оценка веса текста: округление вверх UTF-8 байт / 4.
Точный размер текста в байтах полезен для поиска самых больших записей.
Схемы инструментов, служебная упаковка и непрозрачные блоки не восстановлены.

Вложенные инструменты распознаны по коду exec; исполнение не доказано.
Связь скилла выведена из команды чтения SKILL.md. Команда могла завершиться ошибкой.
Если exec содержит несколько инструментов, общий ответ не распределяется между ними.
Размеры таких скиллов и вложенных инструментов перекрываются; их нельзя складывать.
Файлы: чтение из shell-команд и операции apply_patch, включая перемещение.
Это предполагаемые операции; выполнение и успех не подтверждены. Enter: вызов и ответ.
Произвольные команды, переменные и динамические пути не восстановлены.

active — наблюдаемая история после последнего сжатия, а не точная копия запроса.
history — исходные записи за весь лог; снимки replacement_history не добавляются второй раз.
Смена чата происходит только по s: обновление другого чата не переключает watch.
'''


def run(screen, reader, args, session):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    file_colors = {}
    try:
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            for pair, (operation, color) in enumerate((('read', curses.COLOR_CYAN),
                    ('create', curses.COLOR_GREEN), ('modify', curses.COLOR_YELLOW),
                    ('delete', curses.COLOR_RED), ('move_from', curses.COLOR_YELLOW),
                    ('move_to', curses.COLOR_YELLOW)), 1):
                curses.init_pair(pair, color, -1)
                file_colors[operation] = curses.color_pair(pair)
    except curses.error:
        file_colors = {}
    screen.keypad(True)
    view_index, selected, offset = 0, 0, 0
    scope, query, paused = args.scope, args.filter, False
    file_mode = args.files
    last_poll, polls = 0.0, 0
    status = ''
    selected_key = None
    while True:
        now = time.monotonic()
        if not paused and now - last_poll >= args.interval:
            try:
                reader.poll()
                status = ''
            except OSError as error:
                status = 'Ожидание лога: ' + str(error)
            last_poll = now
            polls += 1
            if args.iterations and polls > args.iterations:
                return
        report = snapshot(reader, scope, session)
        view = VIEWS[view_index][0]
        rows = rows_for(report, view, query, file_mode)
        if selected_key is not None:
            selected = next((i for i, row in enumerate(rows) if row['key'] == selected_key), selected)
        selected = min(max(0, selected), max(0, len(rows) - 1))
        selected_key = rows[selected]['key'] if rows else None
        height, width = screen.getmaxyx()
        screen.erase()
        if height < 14 or width < 72:
            put(screen, 0, 0, 'Увеличьте терминал до 72×14 или используйте --plain. q: выход')
        else:
            header = heading(report)
            for y, line in enumerate(header[:7]):
                put(screen, y, 0, line, curses.A_BOLD if y == 0 else 0)
            put(screen, 7, 0, ' | '.join(label for _, label in VIEWS), curses.A_REVERSE)
            mode_note = f' | {FILE_MODES[file_mode]} (m)' if view == 'files' else ''
            put(screen, 8, 0, f"{VIEWS[view_index][1]} | {'ПАУЗА' if paused else 'WATCH'}{mode_note} | {report.get('title') or ''} | поиск: {query or '—'} | {len(rows)} строк")
            visible = max(1, height - 12)
            if selected < offset:
                offset = selected
            elif selected >= offset + visible:
                offset = selected - visible + 1
            for y, row in enumerate(rows[offset:offset + visible], 9):
                operations = row.get('operations', [])
                operation = next((op for op in ('delete', 'modify', 'move_from', 'move_to', 'create', 'read')
                                  if op in operations), None)
                attr = file_colors.get(operation, 0)
                if operation and operation != 'read':
                    attr |= curses.A_BOLD
                if offset + y - 9 == selected:
                    attr |= curses.A_REVERSE
                put(screen, y, 0, row['text'], attr)
            if not rows:
                empty = 'Нет файлов. m: .md/все | /: поиск' if view == 'files' else 'Нет записей. Измените фильтр или дождитесь новых событий.'
                put(screen, 9, 0, empty)
            put(screen, height - 2, 0, (status + ' | ' if status else '') + diagnostic_summary(report))
            put(screen, height - 1, 0, '1–6 вид | m .md/все | ↑↓ выбор | Enter открыть | / поиск | h история | s чат | p пауза | ? помощь | q выход', curses.A_REVERSE)
        screen.refresh()
        screen.timeout(min(250, max(30, int(args.interval * 1000))))
        try:
            key = screen.get_wch()
        except curses.error:
            continue
        screen.timeout(-1)
        if key in ('q', '\x03'):
            return
        if key in tuple(str(i + 1) for i in range(len(VIEWS))):
            view_index = int(key) - 1
            selected, offset, selected_key = 0, 0, None
        elif key in (curses.KEY_LEFT, curses.KEY_RIGHT):
            view_index = (view_index + (1 if key == curses.KEY_RIGHT else -1)) % len(VIEWS)
            selected, offset, selected_key = 0, 0, None
        elif key in (curses.KEY_DOWN, 'j'):
            selected += 1
            selected_key = None
        elif key in (curses.KEY_UP, 'k'):
            selected -= 1
            selected_key = None
        elif key in (curses.KEY_NPAGE, curses.KEY_PPAGE):
            selected += (1 if key == curses.KEY_NPAGE else -1) * max(1, height - 12)
            selected_key = None
        elif key in (curses.KEY_HOME, curses.KEY_END):
            selected = 0 if key == curses.KEY_HOME else max(0, len(rows) - 1)
            selected_key = None
        elif key == 'p':
            paused = not paused
        elif key == 'm' and view == 'files':
            file_mode = 'md' if file_mode == 'all' else 'all'
            selected, offset, selected_key = 0, 0, None
        elif key == 'h':
            scope = 'history' if scope == 'active' else 'active'
            selected, offset, selected_key = 0, 0, None
        elif key == 'c':
            query = ''
            selected, offset, selected_key = 0, 0, None
        elif key == '/':
            query = prompt(screen, query)
            selected, offset, selected_key = 0, 0, None
        elif key == '?':
            pager(screen, 'Справка', HELP)
        elif key == 'd':
            pager(screen, 'Диагностика данных', '\n'.join(diagnostic_lines(report)))
        elif key == 's':
            chosen = session_picker(screen, discover(Path(args.home).expanduser(), args.archived))
            if chosen:
                try:
                    candidate = RolloutReader(chosen['path'])
                    candidate.poll()
                    reader, session = candidate, chosen
                    selected, offset, selected_key = 0, 0, None
                    last_poll = 0.0
                except OSError as error:
                    status = str(error)
        elif key in ('\n', '\r', curses.KEY_ENTER) and rows:
            row = rows[selected]
            related = related_entries(report, row)
            if related:
                text = '\n\n' + ('\n\n' + '─' * 60 + '\n\n').join(
                    entry_detail(e, reader.path) for e in related)
            else:
                text = str(row.get('event', {}))
            pager(screen, short(row['text'], width - 1), text)


def watch(reader, args, session=None):
    locale.setlocale(locale.LC_ALL, '')
    try:
        curses.wrapper(run, reader, args, session)
    except curses.error as error:
        raise ValueError('Не удалось открыть TUI. Используйте watch --plain: ' + str(error)) from error
