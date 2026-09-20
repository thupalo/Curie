"""Direct SAP -> model chat over RFC (the optional plugin, CHAT_PLUGIN=1).

    protocol.py   words / line breaks / done; message row conversion
    service.py    handles and streams: open, read, close, complete
    selftest.py   the ABAP round for `curie --selftest`

Served by handlers/zbla_chat.py as Z_BLA_RFC_CHAT_OPEN / READ / CLOSE and
Z_AI_CHAT_COMPLETION. See CHAT_PLUGIN_PLAN.md and docs/15-chat-plugin.md.
"""
from curie.chat.protocol import (Chunk, LineAssembler, WordChunker, messages_from_rows,
                                 messages_from_zchatmsg, to_sap)
from curie.chat.service import ChatError, ChatService, current

__all__ = ["Chunk", "LineAssembler", "WordChunker", "messages_from_rows",
           "messages_from_zchatmsg", "to_sap", "ChatError", "ChatService", "current"]
