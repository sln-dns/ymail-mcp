"""CLI для разведки почтового ящика.

    ymail folders          — папки с человеческими именами
    ymail unread [-n 10]   — непрочитанные
    ymail probe            — проверка связи, кириллического поиска и объёма ящика
"""

from __future__ import annotations

import argparse
import sys

from .client import MailClient, MailError, load_config


def _client() -> MailClient:
    cfg = load_config()
    return MailClient(cfg["YMAIL_USER"], cfg["YMAIL_PASSWORD"])


def cmd_folders(args: argparse.Namespace) -> int:
    with _client() as mail:
        for folder in mail.folders():
            marks = " [черновики]" if folder.is_drafts else ""
            print(f"  {folder.name}{marks}")
            if args.verbose:
                print(f"      raw: {folder.raw}   flags: {folder.flags}")
    return 0


def cmd_unread(args: argparse.Namespace) -> int:
    with _client() as mail:
        folder = mail.find_folder(args.folder)
        uids = mail.search(folder, criteria=["UNSEEN"], limit=args.limit)
        letters = mail.headers(folder, uids)
        if not letters:
            print(f"В папке «{folder.name}» непрочитанных нет.")
            return 0
        print(f"Непрочитанных в «{folder.name}»: {len(letters)}\n")
        for item in letters:
            print(f"  [{item.date}] {item.sender}")
            print(f"      {item.subject}   uid={item.uid}")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """Проверить всё, что нужно знать до написания инструментов."""
    with _client() as mail:
        folders = mail.folders()
        print(f"папок: {len(folders)}")
        for folder in folders:
            print(f"  {folder.name}")

        inbox = mail.find_folder(None)
        all_uids = mail.search(inbox, criteria=["ALL"], limit=100000)
        print(f"\nписем во «Входящих»: {len(all_uids)}")

        unseen = mail.search(inbox, criteria=["UNSEEN"], limit=100000)
        print(f"непрочитанных: {len(unseen)}")

        # поиск по кириллице — главный вопрос к серверу
        try:
            hits = mail.search(inbox, criteria=["TEXT", "тендер"], limit=5)
            print(f"поиск по кириллице работает, совпадений (до 5): {len(hits)}")
        except MailError as exc:
            print(f"поиск по кириллице НЕ работает: {exc}")

        try:
            drafts = mail.drafts_folder()
            print(f"папка черновиков: «{drafts.name}» (raw: {drafts.raw})")
        except MailError as exc:
            print(f"черновики: {exc}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="ymail", description="Разведка почтового ящика Яндекса")
    sub = parser.add_subparsers(dest="command", required=True)

    p_folders = sub.add_parser("folders", help="список папок")
    p_folders.add_argument("-v", "--verbose", action="store_true", help="показать raw-имена и флаги")
    p_folders.set_defaults(func=cmd_folders)

    p_unread = sub.add_parser("unread", help="непрочитанные письма")
    p_unread.add_argument("-n", "--limit", type=int, default=10)
    p_unread.add_argument("-f", "--folder", help="папка (по умолчанию Входящие)")
    p_unread.set_defaults(func=cmd_unread)

    sub.add_parser("probe", help="проверка возможностей ящика").set_defaults(func=cmd_probe)

    args = parser.parse_args()
    try:
        return args.func(args)
    except MailError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
