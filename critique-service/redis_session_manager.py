"""Redis-backed SessionRepository for Strands agent session persistence.

Production session store activated by setting the ``REDIS_URL`` env var.
Key scheme::

  strands:session:<session_id>                 → JSON (Session.to_dict)
  strands:session:<session_id>:agent:<agent>   → JSON (SessionAgent.to_dict)
  strands:session:<session_id>:agent:<agent>:messages  → Redis List of JSON strings

Thread-safe: Redis operations are atomic per-key. The caller (``AgentSession._run``)
owns the session's worker thread, so there is no concurrent access to the same
session from multiple threads.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from strands.session.repository_session_manager import RepositorySessionManager
from strands.session.session_repository import SessionRepository
from strands.types.session import Session, SessionAgent, SessionMessage


class RedisSessionManager(RepositorySessionManager, SessionRepository):
    """Redis-backed session manager for Strands agents.

    Wraps a ``RepositorySessionManager`` with a ``SessionRepository`` that
    persists sessions, agents, and messages to Redis. Same pattern as
    ``FileSessionManager`` / ``S3SessionManager``.

    Example::

        mgr = RedisSessionManager(
            session_id="my-session",
            redis_url="redis://:password@host:6379/0",
        )
        agent = Agent(model=..., session_manager=mgr)
    """

    _PREFIX = "strands:session"

    def __init__(
        self,
        session_id: str,
        redis_url: str | None = None,
        **kwargs: Any,
    ):
        """Initialize RedisSessionManager with connection parameters.

        Args:
            session_id: ID for the session.
            redis_url: Redis connection URL (defaults to ``REDIS_URL`` env var,
                       then ``redis://localhost:6379/0``).
            **kwargs: Additional keyword arguments for future extensibility.
        """
        import redis as _redis

        self.redis_url = redis_url or os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        self.redis = _redis.Redis.from_url(self.redis_url, decode_responses=True)
        super().__init__(session_id=session_id, session_repository=self, **kwargs)

    # -- key helpers ---------------------------------------------------------

    def _sk(self, session_id: str) -> str:
        return f"{self._PREFIX}:{session_id}"

    def _ak(self, session_id: str, agent_id: str) -> str:
        return f"{self._PREFIX}:{session_id}:agent:{agent_id}"

    def _mk(self, session_id: str, agent_id: str) -> str:
        return f"{self._PREFIX}:{session_id}:agent:{agent_id}:messages"

    # -- Session CRUD --------------------------------------------------------

    def create_session(self, session: Session, **kwargs: Any) -> Session:
        self.redis.set(self._sk(session.session_id), json.dumps(session.to_dict()))
        return session

    def read_session(self, session_id: str, **kwargs: Any) -> Optional[Session]:
        raw = self.redis.get(self._sk(session_id))
        if raw is None:
            return None
        return Session.from_dict(json.loads(raw))

    # -- Agent CRUD ----------------------------------------------------------

    def create_agent(self, session_id: str, session_agent: SessionAgent, **kwargs: Any) -> None:
        self.redis.set(
            self._ak(session_id, session_agent.agent_id),
            json.dumps(session_agent.to_dict()),
        )

    def read_agent(self, session_id: str, agent_id: str, **kwargs: Any) -> Optional[SessionAgent]:
        raw = self.redis.get(self._ak(session_id, agent_id))
        if raw is None:
            return None
        return SessionAgent.from_dict(json.loads(raw))

    def update_agent(self, session_id: str, session_agent: SessionAgent, **kwargs: Any) -> None:
        self.redis.set(
            self._ak(session_id, session_agent.agent_id),
            json.dumps(session_agent.to_dict()),
        )

    # -- Message CRUD --------------------------------------------------------

    def create_message(
        self, session_id: str, agent_id: str, session_message: SessionMessage, **kwargs: Any
    ) -> None:
        key = self._mk(session_id, agent_id)
        self.redis.rpush(key, json.dumps(session_message.to_dict()))

    def read_message(
        self, session_id: str, agent_id: str, message_id: int, **kwargs: Any
    ) -> Optional[SessionMessage]:
        key = self._mk(session_id, agent_id)
        raw = self.redis.lindex(key, message_id)
        if raw is None:
            return None
        return SessionMessage.from_dict(json.loads(raw))

    def update_message(
        self, session_id: str, agent_id: str, session_message: SessionMessage, **kwargs: Any
    ) -> None:
        key = self._mk(session_id, agent_id)
        self.redis.lset(key, session_message.message_id, json.dumps(session_message.to_dict()))

    def list_messages(
        self,
        session_id: str,
        agent_id: str,
        limit: int | None = None,
        offset: int = 0,
        **kwargs: Any,
    ) -> list[SessionMessage]:
        key = self._mk(session_id, agent_id)
        end = -1 if limit is None else offset + limit - 1
        raw_list = self.redis.lrange(key, offset, end)
        return [SessionMessage.from_dict(json.loads(r)) for r in raw_list]
