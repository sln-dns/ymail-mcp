"""IMAP-клиент для рабочей почты Яндекса. Только чтение и черновики.

Устройство продиктовано двумя свойствами протокола:

* Обычный `FETCH BODY[]` проставляет письму флаг `\\Seen`. То есть простое чтение молча
  пометило бы рабочие письма прочитанными, и человек бы их пропустил. Поэтому здесь
  используется **только `BODY.PEEK[]`**, а флаги не трогаются нигде.
* Имена папок приходят в modified UTF-7 (`&BB4EQgQ,BEAEMEIu-` вместо «Отправленные»).
  Человеку показываем расшифрованное имя, а в команды подставляем исходное — так
  не нужен обратный кодировщик и нечему ломаться.
"""

from __future__ import annotations

import base64
import email
import email.header
import email.utils
import imaplib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path

IMAP_HOST = "imap.yandex.ru"
IMAP_PORT = 993
CONFIG = Path.home() / ".config" / "yandex-mail" / ".env"

# письма бывают на мегабайты — читаем не больше, чем помещается в осмысленный ответ
MAX_BODY_CHARS = 20_000


class MailError(RuntimeError):
    """Ошибка работы с почтой, пригодная для показа человеку."""


def load_config() -> dict[str, str]:
    cfg: dict[str, str] = {}
    if CONFIG.exists():
        if CONFIG.stat().st_mode & 0o077:
            raise MailError(f"{CONFIG} доступен не только владельцу. Выполнить: chmod 600 {CONFIG}")
        for line in CONFIG.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            cfg[key.strip()] = value.strip().strip("\"'")

    for key in ("YMAIL_USER", "YMAIL_PASSWORD"):
        if os.environ.get(key):
            cfg[key] = os.environ[key]

    missing = [k for k in ("YMAIL_USER", "YMAIL_PASSWORD") if not cfg.get(k)]
    if missing:
        raise MailError(
            f"Не заданы {', '.join(missing)} в {CONFIG}.\n"
            "Пароль приложения: id.yandex.ru → Безопасность → Пароли приложений →\n"
            "«Почта (IMAP, SMTP)». Обычный пароль от почты не подойдёт.\n"
            "Файл должен быть с правами 600."
        )
    return cfg


# --- кодировки ------------------------------------------------------------


def _b64_to_text(chunk: str) -> str:
    data = chunk.replace(",", "/")
    data += "=" * ((4 - len(data) % 4) % 4)
    return base64.b64decode(data).decode("utf-16-be")


def decode_mailbox(raw: str) -> str:
    """Имя папки из modified UTF-7 в читаемый вид."""
    out: list[str] = []
    i = 0
    while i < len(raw):
        if raw[i] == "&":
            end = raw.find("-", i)
            if end == -1:
                out.append(raw[i:])
                break
            chunk = raw[i + 1 : end]
            out.append("&" if chunk == "" else _b64_to_text(chunk))
            i = end + 1
        else:
            out.append(raw[i])
            i += 1
    return "".join(out)


def decode_header(value: str | None) -> str:
    """Тема и адреса приходят как =?utf-8?B?...?= — приводим к обычному тексту.

    Через make_header, а не ручной склейкой частей: у длинных тем заголовок свёрнут
    на несколько строк, и при простой склейке пропадает пробел на границе
    («Проверка интеграцииymail» вместо «Проверка интеграции ymail»).
    """
    if not value:
        return ""
    try:
        return str(email.header.make_header(email.header.decode_header(value))).strip()
    except (UnicodeDecodeError, LookupError, ValueError):
        # заголовок с битой кодировкой — вытаскиваем что получится
        parts = []
        for text, charset in email.header.decode_header(value):
            parts.append(
                text.decode(charset or "utf-8", errors="replace") if isinstance(text, bytes) else text
            )
        return "".join(parts).strip()


def html_to_text(html: str) -> str:
    out = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    out = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", out)
    out = re.sub(r"<[^>]+>", "", out)
    from html import unescape

    return re.sub(r"\n{3,}", "\n\n", unescape(out)).strip()


# Ключевые слова IMAP-поиска: их нельзя брать в кавычки, всё остальное — значения.
SEARCH_KEYWORDS = frozenset(
    """ALL ANSWERED BCC BEFORE BODY CC DELETED DRAFT FLAGGED FROM HEADER KEYWORD LARGER
    NEW NOT OLD ON OR RECENT SEEN SENTBEFORE SENTON SENTSINCE SINCE SMALLER SUBJECT TEXT
    TO UID UNANSWERED UNDELETED UNDRAFT UNFLAGGED UNKEYWORD UNSEEN CHARSET UTF-8""".split()
)
_DATE_RE = re.compile(r"^\d{1,2}-[A-Z][a-z]{2}-\d{4}$")


def _prepare_search(args: list[str]) -> tuple[list[object], bool]:
    """Подготовить аргументы SEARCH.

    imaplib вставляет bytes в команду как есть — без кавычек. Поэтому значения с
    пробелами разваливаются на токены и сервер отвечает `Command syntax error`.
    Значения берём в кавычки сами, а кириллицу отдаём байтами (сервер читает их
    по объявленному CHARSET UTF-8).
    """
    prepared: list[object] = []
    has_non_ascii = False
    for arg in args:
        token = str(arg)
        if token.upper() in SEARCH_KEYWORDS or _DATE_RE.match(token):
            prepared.append(token)
            continue
        if not token.isascii():
            # кириллица: кавычки нужны, значение уходит байтами под CHARSET UTF-8
            has_non_ascii = True
            prepared.append(f'"{token}"'.encode("utf-8"))
        elif " " in token or '"' in token:
            prepared.append(f'"{token}"')
        else:
            # Яндекс отвечает NO на закавыченный адрес вида "user@domain.ru",
            # но принимает его же без кавычек — поэтому простые токены не трогаем
            prepared.append(token)
    return prepared, has_non_ascii


# --- модель ---------------------------------------------------------------


@dataclass
class Folder:
    raw: str
    name: str
    flags: str

    @property
    def is_drafts(self) -> bool:
        return "\\Drafts" in self.flags or self.name.casefold() in ("черновики", "drafts")


@dataclass
class Letter:
    uid: str
    subject: str
    sender: str
    to: str
    date: str
    unread: bool
    body: str = ""
    attachments: tuple[str, ...] = ()
    message_id: str = ""


class MailClient:
    """Синхронный IMAP-клиент. Ничего не помечает прочитанным и ничего не удаляет."""

    def __init__(self, user: str, password: str, *, timeout: float = 30.0):
        self.user = user
        self._password = password
        self._timeout = timeout
        self._imap: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> MailClient:
        try:
            self._imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=self._timeout)
            self._imap.login(self.user, self._password)
        except imaplib.IMAP4.error as exc:
            raise MailError(
                f"Яндекс отклонил вход ({exc}).\n"
                "Причины по убыванию вероятности:\n"
                "  1. В YMAIL_PASSWORD обычный пароль, а нужен пароль приложения «Почта (IMAP, SMTP)».\n"
                "  2. Администратор Яндекс 360 закрыл доступ по протоколам для организации.\n"
                "  3. В YMAIL_USER не полный адрес вида name@domain.ru."
            ) from exc
        except OSError as exc:
            raise MailError(f"Почтовый сервер недоступен: {exc}") from exc
        return self

    def __exit__(self, *exc: object) -> None:
        if self._imap is not None:
            try:
                self._imap.close()
            except Exception:
                pass
            try:
                self._imap.logout()
            except Exception:
                pass

    @property
    def imap(self) -> imaplib.IMAP4_SSL:
        if self._imap is None:
            raise MailError("Клиент используется вне контекстного менеджера")
        return self._imap

    # --- папки ---

    def folders(self) -> list[Folder]:
        code, data = self.imap.list()
        if code != "OK":
            raise MailError("Не удалось получить список папок")
        result: list[Folder] = []
        for row in data:
            line = row.decode(errors="replace") if isinstance(row, bytes) else str(row)
            match = re.match(r'\((?P<flags>[^)]*)\)\s+"[^"]*"\s+(?P<name>.+)$', line)
            if not match:
                continue
            raw = match.group("name").strip().strip('"')
            result.append(Folder(raw=raw, name=decode_mailbox(raw), flags=match.group("flags")))
        return result

    def _select(self, folder_raw: str, readonly: bool = True) -> None:
        code, _ = self.imap.select(f'"{folder_raw}"', readonly=readonly)
        if code != "OK":
            raise MailError(f"Не удалось открыть папку {decode_mailbox(folder_raw)}")

    def find_folder(self, wanted: str | None) -> Folder:
        folders = self.folders()
        if not wanted:
            return next((f for f in folders if f.raw.upper() == "INBOX"), folders[0])
        for folder in folders:
            if wanted.casefold() in (folder.name.casefold(), folder.raw.casefold()):
                return folder
        available = ", ".join(f.name for f in folders)
        raise MailError(f"Папка «{wanted}» не найдена. Доступны: {available}")

    def drafts_folder(self) -> Folder:
        folders = self.folders()
        for folder in folders:
            if folder.is_drafts:
                return folder
        raise MailError("Папка черновиков не найдена")

    # --- поиск и чтение ---

    def search(
        self,
        folder: Folder,
        *,
        criteria: list[str] | None = None,
        limit: int = 20,
    ) -> list[str]:
        """UID писем, новые первыми."""
        self._select(folder.raw)
        args = criteria or ["ALL"]
        prepared, has_non_ascii = _prepare_search(args)

        try:
            if has_non_ascii:
                code, data = self.imap.uid("SEARCH", "CHARSET", "UTF-8", *prepared)
            else:
                code, data = self.imap.uid("SEARCH", *prepared)
        except imaplib.IMAP4.error as exc:
            raise MailError(f"Поиск не выполнен: {exc}") from exc
        if code != "OK":
            detail = (data[0] or b"").decode(errors="replace") if data else ""
            # Яндекс отвечает так на временный сбой поиска — особенно после массовых
            # операций. Это НЕ «ничего не найдено»: делать вывод об отсутствии писем нельзя.
            if "UNAVAILABLE" in detail or "Backend error" in detail:
                raise MailError(
                    "Поиск временно недоступен на стороне Яндекса (Backend error). "
                    "Это не значит, что писем нет — повторить через минуту."
                )
            raise MailError(f"Поиск не выполнен: {detail or code}")
        uids = (data[0] or b"").split()
        return [u.decode() for u in reversed(uids)][:limit]

    def headers(self, folder: Folder, uids: list[str]) -> list[Letter]:
        if not uids:
            return []
        self._select(folder.raw)
        letters: list[Letter] = []
        for uid in uids:
            # BODY.PEEK — не проставляет \Seen; FLAGS запрашиваем отдельно, только на чтение
            code, data = self.imap.uid(
                "FETCH", uid, "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)])"
            )
            if code != "OK" or not data or not isinstance(data[0], tuple):
                continue
            flags = str(data[0][0])
            msg = email.message_from_bytes(data[0][1])
            letters.append(
                Letter(
                    uid=uid,
                    subject=decode_header(msg.get("Subject")) or "(без темы)",
                    sender=decode_header(msg.get("From")),
                    to=decode_header(msg.get("To")),
                    date=self._pretty_date(msg.get("Date")),
                    unread="\\Seen" not in flags,
                    message_id=(msg.get("Message-ID") or "").strip(),
                )
            )
        return letters

    def letter(self, folder: Folder, uid: str) -> Letter:
        self._select(folder.raw)
        code, data = self.imap.uid("FETCH", uid, "(FLAGS BODY.PEEK[])")
        if code != "OK" or not data or not isinstance(data[0], tuple):
            raise MailError(f"Письмо {uid} не найдено в папке «{folder.name}»")
        flags = str(data[0][0])
        msg = email.message_from_bytes(data[0][1])
        body, attachments = self._extract(msg)
        return Letter(
            uid=uid,
            subject=decode_header(msg.get("Subject")) or "(без темы)",
            sender=decode_header(msg.get("From")),
            to=decode_header(msg.get("To")),
            date=self._pretty_date(msg.get("Date")),
            unread="\\Seen" not in flags,
            body=body,
            attachments=tuple(attachments),
            message_id=(msg.get("Message-ID") or "").strip(),
        )

    @staticmethod
    def _pretty_date(raw: str | None) -> str:
        if not raw:
            return ""
        try:
            parsed = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return raw
        return parsed.strftime("%Y-%m-%d %H:%M")

    @staticmethod
    def _extract(msg: Message) -> tuple[str, list[str]]:
        """Текст письма и имена вложений. Сами вложения не скачиваем."""
        text_parts: list[str] = []
        html_parts: list[str] = []
        attachments: list[str] = []

        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            filename = part.get_filename()
            if filename or "attachment" in str(part.get("Content-Disposition", "")):
                name = decode_header(filename) if filename else "(без имени)"
                size = len(part.get_payload(decode=True) or b"")
                attachments.append(f"{name} ({size // 1024} КБ)" if size else name)
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            chunk = payload.decode(charset, errors="replace")
            if part.get_content_subtype() == "html":
                html_parts.append(chunk)
            else:
                text_parts.append(chunk)

        body = "\n".join(text_parts).strip() or html_to_text("\n".join(html_parts))
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS] + "\n\n[…письмо обрезано]"
        return body, attachments

    # --- перемещение ---

    def trash_folder(self) -> Folder:
        for folder in self.folders():
            if "\\Trash" in folder.flags or folder.name.casefold() in ("trash", "корзина", "удаленные"):
                return folder
        raise MailError("Папка корзины не найдена")

    def move_to_trash(self, folder: Folder, uids: list[str]) -> int:
        """Перенести письма в Корзину. Обратимо: письма остаются в ящике до её очистки.

        Возвращает число перенесённых. Сервер объявляет MOVE, поэтому используем его —
        иначе пришлось бы копировать, ставить \\Deleted и делать EXPUNGE.
        """
        if not uids:
            return 0
        trash = self.trash_folder()
        # для перемещения папка-источник должна быть открыта на запись
        code, _ = self.imap.select(f'"{folder.raw}"', readonly=False)
        if code != "OK":
            raise MailError(f"Не удалось открыть «{folder.name}» на запись")

        moved = 0
        for start in range(0, len(uids), 100):  # длинную команду сервер может не принять
            chunk = uids[start : start + 100]
            code, _ = self.imap.uid("MOVE", ",".join(chunk), f'"{trash.raw}"')
            if code != "OK":
                raise MailError(f"Сервер отказал в переносе писем (перенесено до сбоя: {moved})")
            moved += len(chunk)
        return moved

    # --- черновики ---

    def append_draft(self, raw_message: bytes) -> Folder:
        """Положить готовое письмо в «Черновики». Отправка не выполняется."""
        drafts = self.drafts_folder()
        # Time2Internaldate требует дату с часовым поясом — иначе ValueError
        stamp = imaplib.Time2Internaldate(datetime.now(timezone.utc).astimezone())
        code, _ = self.imap.append(f'"{drafts.raw}"', "\\Draft", stamp, raw_message)
        if code != "OK":
            raise MailError("Сервер не принял черновик")
        return drafts
