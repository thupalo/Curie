"""Session data: state, identity, chat history, transcript, ABAP runs."""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from enum import Enum


class State(str, Enum):
    CREATED = "CREATED"      # opened by SAP, browser not attached yet
    IDLE = "IDLE"            # waiting for a prompt
    THINKING = "THINKING"    # LLM call in progress
    TOOL = "TOOL"            # executing a tool call (RFC to SAP)
    CHECKING = "CHECKING"    # static rules / syntax check / audit of ABAP code
    CONFIRM = "CONFIRM"      # code shown, waiting for Run / Discard
    RUNNING = "RUNNING"      # ABAP program running in SAP
    CLOSED = "CLOSED"


# What the ABAP heartbeat loop sees in E_STATE (CHAR16).
HEARTBEAT_STATE = {
    State.CREATED: "WAITING",
    State.IDLE: "WAITING",
    State.THINKING: "THINKING",
    State.TOOL: "TOOL",
    State.CHECKING: "THINKING",
    State.CONFIRM: "CONFIRM",
    State.RUNNING: "RUNNING",
    State.CLOSED: "CLOSED",
}

BUSY_STATES = frozenset({State.THINKING, State.TOOL, State.CHECKING})


@dataclass
class SapIdentity:
    """Who opened the session, from the RFC connection attributes."""
    sysid: str = ""
    client: str = ""
    user: str = ""
    language: str = ""
    release: str = ""      # partnerRel, e.g. "45B" or "731"
    host: str = ""

    @classmethod
    def from_attributes(cls, attrs: dict) -> "SapIdentity":
        return cls(
            sysid=attrs.get("sysId", ""),
            client=attrs.get("client", ""),
            user=attrs.get("user", ""),
            language=attrs.get("language", "") or attrs.get("isoLanguage", ""),
            release=attrs.get("partnerRel", ""),
            host=attrs.get("partnerHost", "") or attrs.get("host", ""),
        )

    def as_dict(self) -> dict:
        return {"sysid": self.sysid, "client": self.client, "user": self.user,
                "language": self.language, "release": self.release}


@dataclass
class AbapRun:
    """One piece of generated ABAP on its way through check, confirm, execute."""
    run_id: str
    progname: str
    source: str
    created: float = field(default_factory=time.time)
    status: str = "checking"    # checking | ready | blocked | running | done | failed | discarded
    syntax: dict | None = None  # {"ok", "message", "line", "word"}
    audit: dict | None = None   # {"allowed", "risk", "violations", "summary"}
    can_run: bool = False
    mode: str = ""              # "job" | "sync"
    job: str = ""               # "JOBNAME/JOBCOUNT" in job mode
    result: dict | None = None  # {"ok", "rc", "message", "lines"}
    delivery: asyncio.Future | None = None
    # Knowledge: the exemplar this run was kept as, and where the program
    # came from when it was replayed from the report catalogue.
    exemplar_id: str = ""
    # "Correct" pressed before the exemplar was written (capture runs as a
    # task): honoured when it lands.
    wants_catalogue: bool = False
    origin: dict | None = None  # {"catalogue": note id, "values": {old: new}}
    # Fix rounds this program went through before it was accepted:
    # [{"kind": "syntax"|"rules", "message", "word", "line", "source"}]
    fixes: list = field(default_factory=list)
    # Deterministic repairs applied before the check ("line 4: period
    # added before ENDIF"), and the thread the program answers.
    repairs: list = field(default_factory=list)
    thread_id: str = ""


@dataclass
class Session:
    id: str
    sap: SapIdentity
    initial_prompt: str = ""
    state: State = State.CREATED
    state_text: str = ""
    created: float = field(default_factory=time.time)
    sap_last_seen: float = field(default_factory=time.time)
    browser_last_seen: float | None = None
    # Last prompt / answer / run; drives the idle timeout.
    last_activity: float = field(default_factory=time.time)
    close_reason: str | None = None
    # Opened through the development endpoint, not by SAP: no heartbeat
    # expected, so the SAP-gone timeout does not apply.
    dev: bool = False
    # Address of the first browser request (SESSION_BIND_IP).
    client_ip: str | None = None

    # LLM chat history (OpenAI message dicts, without the system prompt).
    messages: list = field(default_factory=list)
    # Events replayed to a (re)connecting browser: (event, data).
    transcript: list = field(default_factory=list)
    # Live SSE subscribers.
    subscribers: list = field(default_factory=list)

    turn_task: asyncio.Task | None = None
    runs: dict = field(default_factory=dict)
    # Knowledge: tables touched by tools in this session, the last visible
    # prompt, and the note ids injected during the current turn.
    tables_used: set = field(default_factory=set)
    last_prompt: str = ""
    injected_notes: list = field(default_factory=list)
    injected_exemplars: list = field(default_factory=list)
    # Failures fixed during the current turn (feed the insights layer).
    fixes: list = field(default_factory=list)
    # The current thread: the business question, then the follow-ups that
    # refined it (memory.threads.is_followup); exemplars are captured for
    # the thread, and a later run in the same thread replaces its exemplar.
    thread: list = field(default_factory=list)
    thread_exemplar_id: str = ""
    # The thread's id (the first user message id) and the tables its tool
    # calls touched: what a /remember hint is tagged with, and what the
    # recorder links the events of an episode to.
    thread_id: str = ""
    thread_tables: set = field(default_factory=set)
    # What the thread was given and produced: the note and exemplar ids
    # injected since the thread started, and the answers the model gave
    # (the recorder credits one outcome per thread, and only the notes the
    # answers or the programs actually used).
    thread_notes: list = field(default_factory=list)
    thread_exemplars: list = field(default_factory=list)
    thread_answers: list = field(default_factory=list)
    # Threads whose outcome has been counted: {thread id: "up" | "down"}.
    thread_outcome: dict = field(default_factory=dict)
    # The thread is a general question (memory.threads.is_general): its
    # turns take the light path until a follow-up names data.
    thread_light: bool = False
    # Feedback already given: {"answer:<id>" | "run:<id>": "up" | "down"}.
    feedback: dict = field(default_factory=dict)
    # Serialises state changes made from RFC threads and the loop thread.
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def closed(self) -> bool:
        return self.state is State.CLOSED

    @property
    def busy(self) -> bool:
        return self.state in BUSY_STATES

    @property
    def attached(self) -> bool:
        return self.browser_last_seen is not None

    def hello(self) -> dict:
        from curie import config

        return {
            "session_id": self.id,
            "sap": self.sap.as_dict(),
            "initial_prompt": self.initial_prompt,
            "state": self.state.value,
            "state_text": self.state_text,
            # "all" | "owners" | "off": whether the page shows Correct / Wrong.
            "feedback": config.KNOWLEDGE_FEEDBACK,
        }
