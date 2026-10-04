import argparse
from datetime import datetime
import os
from pathlib import Path
import sys
import time

from .discovery import choose, discover
from .display import entry_detail, render_report, report_json, safe, short, size
from .reader import RolloutReader


def positive_float(value):
    import math
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError('Нужно конечное число больше нуля.')
    return parsed


def positive_int(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError('Нужно целое число больше нуля.')
    return parsed


def parser():
    result = argparse.ArgumentParser(description='Локальный анализ контекста Codex. Данные читаются без изменений.')
    result.add_argument('--version', action='version', version='codex-context 1.0.0')
    commands = result.add_subparsers(dest='command', required=True)
    for name in ('sessions', 'report', 'inspect', 'watch'):
        command = commands.add_parser(name)
        command.add_argument('--home', default=os.environ.get('CODEX_HOME', '~/.codex'), help='Каталог данных Codex')
        command.add_argument('--archived', action='store_true', help='Включить архивные сессии')
        command.add_argument('--json', action='store_true', help='Машиночитаемый JSON')
        if name == 'sessions':
            command.add_argument('--limit', type=positive_int, default=30)
            continue
        command.add_argument('--file', type=Path, help='Явный путь к rollout JSONL')
        command.add_argument('--session', help='ID сессии или уникальный префикс; иначе самая свежая')
        command.add_argument('--scope', choices=('active', 'history'), default='active',
                             help='active: история после сжатия; history: исходные записи за всё время')
        command.add_argument('--full', action='store_true', help='Включить полное содержимое записей в JSON')
        if name == 'inspect':
            command.add_argument('--item', required=True, help='ID записи e000001 из отчёта')
        else:
            command.add_argument('--top', type=positive_int, default=15)
            command.add_argument('--filter', default='', help='Поиск по содержимому, инструменту или скиллу')
        if name == 'watch':
            command.add_argument('--interval', type=positive_float, default=0.75)
            command.add_argument('--plain', action='store_true', help='Текстовые снимки вместо TUI')
            command.add_argument('--once', action='store_true', help='Один снимок без ожидания')
            command.add_argument('--iterations', type=positive_int, help='Остановиться после N опросов')
    return result


def snapshot(reader, scope, session=None):
    report = reader.analyzer.report(scope)
    report['path'] = str(reader.path.resolve())
    report['title'] = (session or {}).get('title', '')
    report['file_resets'] = reader.resets
    report['pending_bytes'] = len(reader.pending)
    return report


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        home = Path(args.home).expanduser()
        if args.command == 'sessions':
            sessions = discover(home, args.archived)[:args.limit]
            if args.json:
                import json
                print(json.dumps(sessions, ensure_ascii=False))
            else:
                for session in sessions:
                    date = datetime.fromtimestamp(session['mtime']).astimezone().strftime('%Y-%m-%d %H:%M')
                    print(safe(f"{session['id']}  {date}  {size(session['size']):>10}  {short(session['title'] or session['cwd'], 75)}"))
                if not sessions:
                    print('Логи сессий не найдены.')
            return 0
        session = None
        if args.file:
            path = args.file.expanduser()
        else:
            session = choose(discover(home, args.archived), args.session)
            path = Path(session['path'])
        reader = RolloutReader(path)
        reader.poll()
        if args.command == 'inspect':
            report = snapshot(reader, args.scope, session)
            entry = next((e for e in report['entries'] if e['id'] == args.item), None)
            if entry is None:
                raise ValueError('Запись не найдена; проверьте --scope и ID: ' + args.item)
            if args.json:
                import json
                print(json.dumps(entry, ensure_ascii=False))
            else:
                print(entry_detail(entry, path))
            return 0
        if args.command == 'report' or args.once:
            report = snapshot(reader, args.scope, session)
            print(report_json(report, args.full) if args.json else render_report(report, args.top, args.filter))
            return 0
        if not args.plain and not args.json and sys.stdout.isatty() and sys.stdin.isatty():
            from .tui import watch
            watch(reader, args, session)
            return 0
        count, changed = 0, True
        while True:
            if changed:
                report = snapshot(reader, args.scope, session)
                print(report_json(report, args.full) if args.json else render_report(report, args.top, args.filter), flush=True)
                if not args.json:
                    print('\n' + '─' * 72, flush=True)
            count += 1
            if args.iterations and count >= args.iterations:
                break
            time.sleep(args.interval)
            changed = reader.poll()
        return 0
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        return 0
    except (OSError, ValueError) as error:
        print('Ошибка: ' + safe(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
