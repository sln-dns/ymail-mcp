"""MCP-сервер для рабочей почты Яндекса: чтение и черновики.

Отправки нет намеренно: ошибочное письмо коллегам не отзывается, а черновик лежит
в ящике, пока человек его не прочитает и не отправит сам. Флаги писем не меняются —
чтение идёт через BODY.PEEK, см. client.py.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from mcp.server import MCPServer

from .client import Folder, Letter, MailClient, MailError, load_config

mcp = MCPServer("ymail", description="Рабочая почта Яндекса: поиск, чтение, черновики")

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _client() -> MailClient:
    cfg = load_config()
    return MailClient(cfg["YMAIL_USER"], cfg["YMAIL_PASSWORD"])


def _imap_date(value: str) -> str:
    """YYYY-MM-DD → 01-Jan-2026, как требует IMAP."""
    day = datetime.strptime(value, "%Y-%m-%d")
    return f"{day.day:02d}-{MONTHS[day.month - 1]}-{day.year}"


def _line(item: Letter) -> str:
    mark = "•" if item.unread else " "
    return f"{mark} [{item.date}] {item.sender}\n    {item.subject}   uid={item.uid}"


def _base_subject(subject: str) -> str:
    """«Re: Fwd: Тема» → «Тема» — чтобы собрать переписку."""
    return re.sub(r"^(?:\s*(?:re|fwd|fw|ответ|пересылка)\s*:\s*)+", "", subject, flags=re.I).strip()


@mcp.tool()
def ymail_folders() -> str:
    """Список папок почтового ящика."""
    try:
        with _client() as mail:
            folders = mail.folders()
    except MailError as exc:
        return f"Ошибка: {exc}"
    return "\n".join(f"  {f.name}{'  [черновики]' if f.is_drafts else ''}" for f in folders)


@mcp.tool()
def ymail_unread(limit: int = 15, folder: str = "") -> str:
    """Непрочитанные письма: от кого, тема, дата. Статус писем не меняется."""
    try:
        with _client() as mail:
            box = mail.find_folder(folder or None)
            uids = mail.search(box, criteria=["UNSEEN"], limit=limit)
            letters = mail.headers(box, uids)
    except MailError as exc:
        return f"Ошибка: {exc}"
    if not letters:
        return f"В папке «{box.name}» непрочитанных нет."
    return f"Непрочитанных в «{box.name}»: {len(letters)}\n\n" + "\n".join(_line(x) for x in letters)


@mcp.tool()
def ymail_search(
    text: str = "",
    sender: str = "",
    subject: str = "",
    since: str = "",
    until: str = "",
    folder: str = "",
    unread_only: bool = False,
    limit: int = 20,
) -> str:
    """Найти письма. Даты в формате YYYY-MM-DD, папка по умолчанию — Входящие.

    text — по всему письму, sender — по адресу или имени отправителя,
    subject — по теме. Условия складываются.
    """
    criteria: list[str] = []
    try:
        if sender:
            criteria += ["FROM", sender]
        if subject:
            criteria += ["SUBJECT", subject]
        if text:
            criteria += ["TEXT", text]
        if since:
            criteria += ["SINCE", _imap_date(since)]
        if until:
            criteria += ["BEFORE", _imap_date(until)]
        if unread_only:
            criteria += ["UNSEEN"]
    except ValueError:
        return "Даты указываются как YYYY-MM-DD."

    if not criteria:
        return "Нужно задать хотя бы одно условие: text, sender, subject или период."

    try:
        with _client() as mail:
            box = mail.find_folder(folder or None)
            uids = mail.search(box, criteria=criteria, limit=limit)
            letters = mail.headers(box, uids)
    except MailError as exc:
        return f"Ошибка: {exc}"

    if not letters:
        return f"В папке «{box.name}» ничего не найдено."
    return f"Найдено в «{box.name}»: {len(letters)}\n\n" + "\n".join(_line(x) for x in letters)


@mcp.tool()
def ymail_read(uid: str, folder: str = "") -> str:
    """Прочитать письмо по uid (его показывают ymail_search и ymail_unread).

    Письмо НЕ помечается прочитанным.
    """
    try:
        with _client() as mail:
            box = mail.find_folder(folder or None)
            item = mail.letter(box, uid)
    except MailError as exc:
        return f"Ошибка: {exc}"

    head = [
        f"От:   {item.sender}",
        f"Кому: {item.to}",
        f"Тема: {item.subject}",
        f"Дата: {item.date}" + ("   (не прочитано)" if item.unread else ""),
    ]
    if item.attachments:
        head.append(f"Вложения: {', '.join(item.attachments)}")
    return "\n".join(head) + "\n\n" + (item.body or "(пустое письмо)")


@mcp.tool()
def ymail_thread(uid: str, folder: str = "", limit: int = 15) -> str:
    """Собрать переписку по теме письма — все письма с той же темой, по возрастанию даты."""
    try:
        with _client() as mail:
            box = mail.find_folder(folder or None)
            origin = mail.letter(box, uid)
            base = _base_subject(origin.subject)
            uids = mail.search(box, criteria=["SUBJECT", base], limit=limit)
            letters = sorted(mail.headers(box, uids), key=lambda x: x.date)
    except MailError as exc:
        return f"Ошибка: {exc}"

    out = [f"Переписка «{base}» — писем: {len(letters)}\n"]
    for item in letters:
        out.append(f"  [{item.date}] {item.sender}   uid={item.uid}")
    out.append("\nЧтобы прочитать любое — ymail_read с его uid.")
    return "\n".join(out)


@mcp.tool()
def ymail_trash(
    sender: str = "",
    subject: str = "",
    text: str = "",
    since: str = "",
    until: str = "",
    folder: str = "",
    apply: bool = False,
    limit: int = 500,
) -> str:
    """Перенести письма в Корзину по тем же условиям, что и поиск.

    Без apply=True только показывает, сколько писем подпадает, и ничего не трогает.
    Перенос обратим: письма лежат в Корзине, пока она не будет очищена.
    Условия обязательны — без них инструмент ничего не делает.
    """
    criteria: list[str] = []
    try:
        if sender:
            criteria += ["FROM", sender]
        if subject:
            criteria += ["SUBJECT", subject]
        if text:
            criteria += ["TEXT", text]
        if since:
            criteria += ["SINCE", _imap_date(since)]
        if until:
            criteria += ["BEFORE", _imap_date(until)]
    except ValueError:
        return "Даты указываются как YYYY-MM-DD."

    if not criteria:
        return "Нужно задать условие: sender, subject, text или период. Без условий не удаляю."

    try:
        with _client() as mail:
            box = mail.find_folder(folder or None)
            uids = mail.search(box, criteria=criteria, limit=limit)
            if not uids:
                return f"В «{box.name}» под условие ничего не подпадает."
            preview = mail.headers(box, uids[:5])
            if not apply:
                head = "\n".join(_line(x) for x in preview)
                return (
                    f"Под условие подпадает писем: {len(uids)} в «{box.name}».\n"
                    f"Первые из них:\n\n{head}\n\n"
                    "Ничего не перенесено. Повторить с apply=True, чтобы убрать в Корзину."
                )
            moved = mail.move_to_trash(box, uids)
            left = mail.search(box, criteria=criteria, limit=limit)
    except MailError as exc:
        return f"Ошибка: {exc}"

    tail = f" В «{box.name}» под это условие осталось: {len(left)}." if left else ""
    return f"Перенесено в Корзину: {moved} писем из «{box.name}».{tail}"


@mcp.tool()
def ymail_draft(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    reply_to_uid: str = "",
    folder: str = "",
) -> str:
    """Положить черновик письма в папку «Черновики». Письмо НЕ отправляется.

    reply_to_uid — если это ответ: подставит тему «Re:» и свяжет письма в переписку.
    Отправить черновик человек должен сам из почты, прочитав его.
    """
    if not to.strip() or not body.strip():
        return "Нужны адресат и текст письма."

    try:
        with _client() as mail:
            cfg_user = mail.user
            in_reply_to = references = ""
            if reply_to_uid:
                box = mail.find_folder(folder or None)
                origin = mail.letter(box, reply_to_uid)
                in_reply_to = references = origin.message_id
                if not subject.strip():
                    subject = f"Re: {_base_subject(origin.subject)}"

            msg = EmailMessage()
            msg["From"] = cfg_user
            msg["To"] = to
            if cc:
                msg["Cc"] = cc
            msg["Subject"] = subject
            msg["Date"] = formatdate(localtime=True)
            msg["Message-ID"] = make_msgid()
            if in_reply_to:
                msg["In-Reply-To"] = in_reply_to
                msg["References"] = references
            msg.set_content(body)

            drafts = mail.append_draft(msg.as_bytes())

            # сервер мог принять письмо и не сохранить — проверяем, что черновик на месте
            uids = mail.search(drafts, criteria=["SUBJECT", subject], limit=5)
            saved = mail.headers(drafts, uids)
    except MailError as exc:
        return f"Ошибка: {exc}"

    match = next((s for s in saved if s.subject == subject), None)
    if match is None:
        return (
            f"⚠️ Черновик отправлен в «{drafts.name}», но при перечитывании не найден. "
            "Проверь папку черновиков в почте."
        )
    return (
        f"Черновик сохранён в «{drafts.name}» (uid={match.uid}).\n"
        f"Кому: {to}\nТема: {subject}\n\n"
        "Письмо НЕ отправлено — открой почту, проверь и отправь сам."
    )


def main() -> None:
    logging.getLogger("imaplib").setLevel(logging.WARNING)
    mcp.run()


if __name__ == "__main__":
    main()
