"""Windows entry point. With no arguments, open the Chinese GUI."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import traceback

from backend import AUTHOR, VERSION, repack, unpack


def write_result(path, result):
    if path:
        Path(path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    elif sys.stdout is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))


def gui_check(result_path):
    import tkinter as tk
    from gui import APP_TITLE, MCPKApp
    root = tk.Tk()
    root.withdraw()
    app = MCPKApp(root)
    root.update()
    result = {
        'ok': True, 'frozen_executable': bool(getattr(sys, 'frozen', False)),
        'author': AUTHOR, 'version': VERSION,
        'window_title': root.title(), 'title_matches': root.title() == APP_TITLE,
        'tabs': [app.notebook.tab(tab, 'text').strip() for tab in app.notebook.tabs()],
        'tk_version': root.tk.call('info', 'patchlevel'),
    }
    root.destroy()
    write_result(result_path, result)


def main():
    if len(sys.argv) == 1:
        from gui import run_gui
        run_gui()
        return 0
    parser = argparse.ArgumentParser(description=f'MCPK 解包/原结构回封工具，作者：{AUTHOR}')
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('unpack', 'repack'):
        child = commands.add_parser(command)
        child.add_argument('source')
        child.add_argument('output')
        child.add_argument('--result', required=True, help='UTF-8 JSON 运行结果文件')
    smoke = commands.add_parser('gui-check')
    smoke.add_argument('--result', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'gui-check':
            gui_check(args.result)
            return 0
        operation = unpack if args.command == 'unpack' else repack
        logs = []
        result = operation(args.source, args.output, log=logs.append)
        write_result(args.result, {'ok': True, 'author': AUTHOR, 'version': VERSION,
                                  **result, 'logs': logs})
        return 0
    except Exception as error:
        write_result(args.result, {'ok': False, 'error': str(error), 'traceback': traceback.format_exc()})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
