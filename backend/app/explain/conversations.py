"""In-memory follow-up conversations, each bound to one method's context."""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict

from pydantic import BaseModel, Field

from app.llm.ollama_client import Message


class Conversation(BaseModel):
    id: str
    method_pk: str
    method_id: str
    repository_id: str
    summary: str  # one-paragraph description of the method being discussed
    context: str  # the rendered context sent with every turn
    valid_labels: list[str] = Field(default_factory=list)  # citation labels that exist
    history: list[Message] = Field(default_factory=list)


class ConversationStore:
    """A small LRU of conversations. Lost on restart, which only costs the chat history."""

    def __init__(self, capacity: int = 100) -> None:
        self._capacity = capacity
        self._items: OrderedDict[str, Conversation] = OrderedDict()
        self._lock = threading.Lock()

    def create(self, **fields: object) -> Conversation:
        conversation = Conversation(id=uuid.uuid4().hex[:12], **fields)  # type: ignore[arg-type]
        with self._lock:
            self._items[conversation.id] = conversation
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)
        return conversation

    def get(self, conversation_id: str) -> Conversation | None:
        with self._lock:
            found = self._items.get(conversation_id)
            if found is not None:
                self._items.move_to_end(conversation_id)
            return found.model_copy(deep=True) if found else None

    def append(self, conversation_id: str, *messages: Message) -> None:
        with self._lock:
            if conversation_id in self._items:
                self._items[conversation_id].history.extend(messages)
