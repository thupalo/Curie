"""The wire protocol of the direct chat: words, line breaks, done.

RFC in release 4.5B carries flat CHAR fields that lose trailing blanks, so
a model delta ("Hel", "lo wor", "ld.\\n\\n1.") cannot travel as it is. The
interface transports words and line breaks instead (CHAT_INTERFACE.md,
section 5):

    Chunk(text="Turn")                      one word, no blanks inside
    Chunk(text="wrote:", newline=True)      the word, then a line break
    Chunk(text="", newline=True)            an empty line
    Chunk(done=True)                        end of the answer (sentinel)

WordChunker turns deltas into chunks; LineAssembler is its inverse and
renders chunks into lines the way the ABAP client does, so the
non-streamed call returns exactly what the streamed client would have
shown. Message rows (ZBLA_RFC_MSG, ZCHATMSG) are converted here as well.
"""
from __future__ import annotations

from dataclasses import dataclass

from curie import config
from curie.sap_tools.abap_source import TRANSLITERATE

MAX_WORD = 200        # client wrap position; the field is 255
FIELD_TEXT = 255
ROLES = {"user", "assistant", "system"}
# ZCHATMSG-MSGROL is one character.
MSGROL = {"U": "user", "A": "assistant", "S": "system"}


@dataclass(slots=True)
class Chunk:
    text: str = ""
    newline: bool = False
    done: bool = False

    def row(self, codepage: str | None = None) -> dict:
        """The ZBLA_RFC_CHUNK row."""
        return {"TEXT": to_sap(self.text, codepage),
                "NEWLINE": "X" if self.newline else " ",
                "DONE": "X" if self.done else " "}


class WordChunker:
    """Deltas in, word/newline chunks out. Holds back the unfinished word."""

    def __init__(self, max_word: int = MAX_WORD):
        self.word = ""
        self.max_word = max_word

    def feed(self, delta: str) -> list[Chunk]:
        out: list[Chunk] = []
        for ch in delta:
            if ch == "\r":
                continue
            if ch == "\n":
                out.append(Chunk(self.word, newline=True))
                self.word = ""
            elif ch.isspace():
                if self.word:
                    out.append(Chunk(self.word))
                    self.word = ""
            elif ord(ch) < 0x20:
                continue
            else:
                self.word += ch
                if len(self.word) >= self.max_word:
                    out.append(Chunk(self.word))
                    self.word = ""
        return out

    def flush(self) -> list[Chunk]:
        out = [Chunk(self.word)] if self.word else []
        self.word = ""
        return out


@dataclass(slots=True)
class Line:
    text: str
    continuation: bool = False   # wrapped from the previous line, not a new paragraph

    def tline(self) -> dict:
        """SAPscript ITF row: '*' starts a paragraph, blank continues, '/' is an empty line."""
        if not self.text and not self.continuation:
            return {"TDFORMAT": "/", "TDLINE": ""}
        return {"TDFORMAT": " " if self.continuation else "*", "TDLINE": self.text}


class LineAssembler:
    """Chunks in, lines out, with the client's rendering rules.

    Words on a line are joined with one blank; a NEWLINE chunk ends the
    line; a word that would overflow `width` starts a continuation line.
    """

    def __init__(self, width: int = FIELD_TEXT):
        self.width = width
        self.lines: list[Line] = []
        self.current = ""
        self.continuation = False

    def _emit(self, next_is_continuation: bool) -> None:
        self.lines.append(Line(self.current, self.continuation))
        self.current = ""
        self.continuation = next_is_continuation

    def feed(self, chunk: Chunk) -> None:
        word = chunk.text
        if word:
            joined = f"{self.current} {word}" if self.current else word
            if len(joined) > self.width and self.current:
                self._emit(next_is_continuation=True)
                joined = word
            self.current = joined[:self.width]
        if chunk.newline:
            self._emit(next_is_continuation=False)

    def finish(self) -> list[Line]:
        if self.current:
            self._emit(next_is_continuation=False)
        return self.lines

    def text(self) -> str:
        """The lines as one string (continuations rejoined with a blank)."""
        out: list[str] = []
        for line in self.lines:
            if line.continuation and out:
                out[-1] = f"{out[-1]} {line.text}"
            else:
                out.append(line.text)
        return "\n".join(out)


def to_sap(text: str, codepage: str | None = None) -> str:
    """Make a word representable in the backend codepage and fit the field."""
    codepage = codepage or config.profile()["codepage"]
    fixed = text.translate(TRANSLITERATE)
    try:
        fixed.encode(codepage)
    except UnicodeEncodeError:
        fixed = fixed.encode(codepage, errors="replace").decode(codepage)
    return fixed[:FIELD_TEXT]


def _field(row: dict, name: str, index: int) -> str:
    """A row field by name, or by position when the structure names differ."""
    if name in row:
        value = row[name]
    else:
        lower = {k.upper(): v for k, v in row.items()}
        if name in lower:
            value = lower[name]
        else:
            values = list(row.values())
            value = values[index] if index < len(values) else ""
    return "" if value is None else str(value)


def messages_from_rows(rows: list[dict] | None) -> list[dict]:
    """ZBLA_RFC_MSG rows (MSGNO, ROLE, LINENO, TEXT) -> chat messages.

    Rows with the same MSGNO form one message, lines joined with newlines.
    """
    messages: list[dict] = []
    current_no = None
    lines: list[str] = []
    role = "user"

    def close() -> None:
        if current_no is not None:
            messages.append({"role": role, "content": "\n".join(lines)})

    for row in rows or []:
        msgno = _field(row, "MSGNO", 0).strip() or "0"
        if msgno != current_no:
            close()
            current_no = msgno
            lines = []
            role = _field(row, "ROLE", 1).strip().lower() or "user"
            if role not in ROLES:
                role = "user"
        lines.append(_field(row, "TEXT", 3).rstrip())
    close()
    return messages


def messages_from_zchatmsg(rows: list[dict] | None, prompt: str) -> list[dict]:
    """ZCHATMSG history rows plus the new prompt -> chat messages.

    One row is one message; MSGROL U/A/S is the role (anything else counts
    as user). Rows are taken in the order the caller sent them.
    """
    messages: list[dict] = []
    for row in rows or []:
        text = _field(row, "MSGTXT", 9).rstrip()
        if not text:
            continue
        role = MSGROL.get(_field(row, "MSGROL", 3).strip().upper(), "user")
        messages.append({"role": role, "content": text})
    messages.append({"role": "user", "content": prompt.rstrip()})
    return messages
