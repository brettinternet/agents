from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Any

from .db import canonical_json, mutation, utc_now
from .policy import DomainError, validate_page, validate_text

MENTION = re.compile(r"(?<![A-Za-z0-9_.-])@([a-z0-9-]+)")
CHANNELS = ("#all-hands", "#findings", "#publishing", "#coordination", "#incidents")
WORK_MEMBERS = ("human", "elder", "explorer", "yapper")


def seed_conversations(connection: sqlite3.Connection) -> None:
    now = utc_now()
    for address in CHANNELS:
        connection.execute(
            "INSERT OR IGNORE INTO conversations(address,kind,created_at,updated_at)VALUES(?,'channel',?,?)",
            (address, now, now),
        )
    actors = {str(row[0]) for row in connection.execute("SELECT slug FROM actors")}
    channel_members = {
        "#all-hands": {"human", "elder", "explorer", "yapper"},
        "#findings": {"human", "elder", "explorer"},
        "#publishing": {"human", "elder", "yapper"},
        "#coordination": {"human", "elder"},
        "#incidents": {"human", "elder"},
    }
    channel_notifications = {
        "#all-hands": {"elder", "explorer", "yapper"},
        "#findings": {"elder", "explorer"},
        "#publishing": {"elder", "yapper"},
        "#coordination": {"elder"},
        "#incidents": {"elder"},
    }
    for address in CHANNELS:
        cid = connection.execute("SELECT id FROM conversations WHERE address=?", (address,)).fetchone()[0]
        for actor in channel_members[address] & actors:
            connection.execute(
                "INSERT OR IGNORE INTO conversation_members(conversation_id,actor_slug,notify)VALUES(?,?,?)",
                (cid, actor, int(actor in channel_notifications[address])),
            )


def sync_work_conversation_membership(connection: sqlite3.Connection, item_id: str) -> None:
    """Keep a work thread readable by its current active participants."""
    conversation = connection.execute(
        "SELECT id FROM conversations WHERE address=? AND kind='work'", (f"work:{item_id}",)
    ).fetchone()
    if conversation is None:
        return
    members: set[str] = set(WORK_MEMBERS)
    members.update(
        str(row[0])
        for row in connection.execute("SELECT actor_slug FROM assignments WHERE work_id=? AND state='open'", (item_id,))
    )
    members.update(
        str(row[0])
        for row in connection.execute(
            "SELECT responder FROM consultations WHERE work_id=? AND state='assigned' AND responder IS NOT NULL",
            (item_id,),
        )
    )
    members.update(
        str(row[0])
        for row in connection.execute(
            "SELECT r.actor_slug FROM reviews r "
            "JOIN submissions s ON s.id=r.submission_id "
            "JOIN executions e ON e.id=s.execution_id "
            "WHERE e.work_id=? AND r.verdict='pending'",
            (item_id,),
        )
    )
    member_list = sorted(members)
    actors = (
        {
            str(row[0])
            for row in connection.execute(
                "SELECT slug FROM actors WHERE slug IN ({})".format(",".join("?" for _ in member_list)),
                tuple(member_list),
            )
        }
        if member_list
        else set()
    )
    connection.executemany(
        "INSERT OR IGNORE INTO conversation_members(conversation_id,actor_slug,notify)VALUES(?,?,1)",
        ((conversation["id"], actor) for actor in actors),
    )
    if actors:
        marks = ",".join("?" for _ in actors)
        connection.execute(
            f"DELETE FROM conversation_members WHERE conversation_id=? AND actor_slug NOT IN ({marks})",
            (conversation["id"], *actors),
        )


def create_work_conversation(connection: sqlite3.Connection, item_id: str) -> int:
    now = utc_now()
    cursor = connection.execute(
        "INSERT INTO conversations(address,kind,work_id,created_at,updated_at)VALUES(?,'work',?,?,?)",
        (f"work:{item_id}", item_id, now, now),
    )
    if cursor.lastrowid is None:
        raise RuntimeError("SQLite did not allocate a conversation ID")
    cid = cursor.lastrowid
    for actor in WORK_MEMBERS:
        if connection.execute("SELECT 1 FROM actors WHERE slug=?", (actor,)).fetchone():
            connection.execute(
                "INSERT INTO conversation_members(conversation_id,actor_slug,notify)VALUES(?,?,1)", (cid, actor)
            )
    return cid


class Messages:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def post(
        self, sender: str, to: str, body: str, reply_to: int | None = None, urgency: str = "normal"
    ) -> dict[str, Any]:
        validate_text(body, "message")
        if urgency not in {"normal", "urgent"}:
            raise DomainError("validation_failed", "invalid urgency")
        conversation = self._resolve(sender, to)
        if (
            reply_to is not None
            and self.connection.execute(
                "SELECT 1 FROM messages WHERE id=? AND conversation_id=?", (reply_to, conversation["id"])
            ).fetchone()
            is None
        ):
            raise DomainError("validation_failed", "reply target is outside conversation")
        now = utc_now()
        cursor = self.connection.execute(
            "INSERT INTO messages(conversation_id,sender_slug,reply_to_id,body,urgency,created_at)VALUES(?,?,?,?,?,?)",
            (conversation["id"], sender, reply_to, body, urgency, now),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not allocate a message ID")
        message_id = cursor.lastrowid
        recipients = self._recipients(int(conversation["id"]), str(conversation["address"]), sender, body)
        if to == "!human":
            recipients = {"human"}
            self.connection.execute(
                "INSERT INTO incidents(kind,entity_kind,entity_id,severity,state,summary,details_json,created_at,updated_at)VALUES('human_escalation','message',?,'high','open',?,'{}',?,?)",
                (str(message_id), body[:200], now, now),
            )
        targets = self._delivery_targets(conversation, recipients)
        self.connection.executemany(
            "INSERT INTO deliveries(message_id,actor_slug,terminal_run_id,state,attempts,next_attempt_at)"
            "VALUES (?, ?, ?, 'pending', 0, ?)",
            ((message_id, actor, targets[actor], now) for actor in sorted(recipients)),
        )
        return {
            "id": message_id,
            "conversation_id": int(conversation["id"]),
            "address": str(conversation["address"]),
            "deliveries": len(recipients),
        }

    def inbox(self, actor: str, terminal_run_id: int, persistent: bool, limit: int = 50) -> list[dict[str, Any]]:
        validate_page(limit, 50)
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT m.id,m.created_at,m.sender_slug,c.address,m.reply_to_id,m.body,m.urgency "
                "FROM deliveries d JOIN messages m ON m.id=d.message_id "
                "JOIN conversations c ON c.id=m.conversation_id "
                "WHERE d.actor_slug=? AND d.state='pending' "
                "AND (d.terminal_run_id=? OR (d.terminal_run_id IS NULL AND ?=1)) "
                "ORDER BY m.id LIMIT ?",
                (actor, terminal_run_id, int(persistent), limit),
            )
        ]

    def ack(self, actor: str, terminal_run_id: int, persistent: bool, message_ids: list[int]) -> dict[str, Any]:
        if not message_ids or len(message_ids) > 50 or len(message_ids) != len(set(message_ids)):
            raise DomainError("validation_failed", "ack requires 1-50 unique message IDs")
        marks = ",".join("?" for _ in message_ids)
        visibility = "actor_slug=? AND (terminal_run_id=? OR (terminal_run_id IS NULL AND ?=1))"
        context = (actor, terminal_run_id, int(persistent))
        found = {
            int(row[0])
            for row in self.connection.execute(
                f"SELECT message_id FROM deliveries WHERE {visibility} AND message_id IN ({marks})",
                (*context, *message_ids),
            )
        }
        if found != set(message_ids):
            raise DomainError("unauthorized", "cannot acknowledge another actor's delivery")
        self.connection.execute(
            f"UPDATE deliveries SET state='acknowledged',acknowledged_at=? "
            f"WHERE {visibility} AND message_id IN ({marks})",
            (utc_now(), *context, *message_ids),
        )
        return {"acknowledged": sorted(found)}

    def history(self, actor: str, address: str, before_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        validate_page(limit)
        conversation = self._conversation(address)
        if conversation["kind"] == "work" and conversation["work_id"] is not None:
            sync_work_conversation_membership(self.connection, str(conversation["work_id"]))
        if (
            self.connection.execute(
                "SELECT 1 FROM conversation_members WHERE conversation_id=? AND actor_slug=?",
                (conversation["id"], actor),
            ).fetchone()
            is None
        ):
            raise DomainError("unauthorized", "actor is not a conversation member")
        sql = "SELECT m.* FROM messages m WHERE conversation_id=?"
        args: list[Any] = [conversation["id"]]
        if before_id is not None:
            sql += " AND id<?"
            args.append(before_id)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [dict(row) for row in self.connection.execute(sql, args)]

    def _conversation(self, address: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM conversations WHERE address=?", (address,)).fetchone()
        if row is None:
            raise DomainError("not_found", f"conversation {address} does not exist")
        return row

    def _resolve(self, sender: str, to: str) -> sqlite3.Row:
        if to == "!human":
            now = utc_now()
            self.connection.execute(
                "INSERT OR IGNORE INTO conversations(address,kind,created_at,updated_at)VALUES('!human','escalation',?,?)",
                (now, now),
            )
            cid = self.connection.execute("SELECT id FROM conversations WHERE address='!human'").fetchone()[0]
            for actor in (sender, "human"):
                self.connection.execute(
                    "INSERT OR IGNORE INTO conversation_members(conversation_id,actor_slug,notify)VALUES(?,?,1)",
                    (cid, actor),
                )
            return self._conversation("!human")
        if to.startswith("@"):
            target = to[1:]
            actor = self.connection.execute("SELECT persistent FROM actors WHERE slug=?", (target,)).fetchone()
            if actor is None:
                raise DomainError("not_found", "DM target does not exist")
            if not actor["persistent"]:
                self._single_live_terminal(target)
            address = "dm:" + ":".join(sorted((sender, target)))
            now = utc_now()
            self.connection.execute(
                "INSERT OR IGNORE INTO conversations(address,kind,created_at,updated_at)VALUES(?,'dm',?,?)",
                (address, now, now),
            )
            cid = self.connection.execute("SELECT id FROM conversations WHERE address=?", (address,)).fetchone()[0]
            for member in (sender, target):
                self.connection.execute(
                    "INSERT OR IGNORE INTO conversation_members(conversation_id,actor_slug,notify)VALUES(?,?,1)",
                    (cid, member),
                )
            return self._conversation(address)
        conversation = self._conversation(to)
        if conversation["kind"] == "work" and conversation["work_id"] is not None:
            sync_work_conversation_membership(self.connection, str(conversation["work_id"]))
        if (
            self.connection.execute(
                "SELECT 1 FROM conversation_members WHERE conversation_id=? AND actor_slug=?",
                (conversation["id"], sender),
            ).fetchone()
            is None
        ):
            raise DomainError("unauthorized", "sender is not conversation member")
        return conversation

    def _single_live_terminal(self, actor: str) -> int:
        rows = list(
            self.connection.execute(
                "SELECT id FROM terminal_runs WHERE actor_slug=? AND state='live' AND token_revoked_at IS NULL "
                "ORDER BY id",
                (actor,),
            )
        )
        if not rows:
            raise DomainError("unavailable_actor", "on-demand actor has no active terminal")
        if len(rows) > 1:
            raise DomainError("unavailable_actor", "on-demand actor has multiple active terminals")
        return int(rows[0]["id"])

    def _recipients(self, cid: int, address: str, sender: str, body: str) -> set[str]:
        if address == "#all-hands":
            mentioned = set(MENTION.findall(body))
            if mentioned:
                marks = ",".join("?" for _ in mentioned)
                known = {
                    str(row[0])
                    for row in self.connection.execute(
                        f"SELECT slug FROM actors WHERE slug IN ({marks})",
                        tuple(sorted(mentioned)),
                    )
                }
                missing = mentioned - known
                if missing:
                    raise DomainError(
                        "not_found",
                        f"mentioned actor does not exist: {sorted(missing)[0]}",
                    )
        recipients = {
            str(row[0])
            for row in self.connection.execute(
                "SELECT actor_slug FROM conversation_members WHERE conversation_id=? AND notify=1",
                (cid,),
            )
        }
        recipients.discard(sender)
        return recipients

    def _delivery_targets(self, conversation: sqlite3.Row, recipients: set[str]) -> dict[str, int | None]:
        targets: dict[str, int | None] = {actor: None for actor in recipients}
        if conversation["kind"] == "dm" and recipients:
            for actor in recipients:
                persistent = self.connection.execute("SELECT persistent FROM actors WHERE slug=?", (actor,)).fetchone()
                if persistent is not None and not persistent["persistent"]:
                    targets[actor] = self._single_live_terminal(actor)
            return targets
        if conversation["kind"] != "work" or conversation["work_id"] is None or not recipients:
            return targets

        marks = ",".join("?" for _ in recipients)
        actors = tuple(sorted(recipients))
        work_id = str(conversation["work_id"])
        rows = self.connection.execute(
            "WITH purpose_targets(actor_slug,terminal_run_id,priority,purpose_id) AS ("
            "SELECT actor_slug,terminal_run_id,1,id FROM assignments "
            f"WHERE work_id=? AND state='open' AND actor_slug IN ({marks}) "
            "UNION ALL "
            "SELECT r.actor_slug,r.terminal_run_id,2,r.id FROM reviews r "
            "JOIN submissions s ON s.id=r.submission_id "
            "JOIN executions e ON e.id=s.execution_id "
            f"WHERE e.work_id=? AND r.verdict='pending' AND r.actor_slug IN ({marks}) "
            "UNION ALL "
            "SELECT responder,terminal_run_id,3,id FROM consultations "
            f"WHERE work_id=? AND state='assigned' AND responder IN ({marks})"
            ") "
            "SELECT actor_slug,terminal_run_id FROM purpose_targets "
            "ORDER BY actor_slug,priority,purpose_id",
            (work_id, *actors, work_id, *actors, work_id, *actors),
        )
        resolved: set[str] = set()
        for row in rows:
            actor = str(row["actor_slug"])
            if actor not in targets or actor in resolved:
                continue
            resolved.add(actor)
            terminal_run_id = row["terminal_run_id"]
            targets[actor] = int(terminal_run_id) if terminal_run_id is not None else None
        return targets


class Messaging:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    @staticmethod
    def _identity(actor: str) -> str:
        return "human" if actor == "human" else f"agent:{actor}"

    def post(
        self,
        request_id: str,
        actor: str,
        to: str,
        body: str,
        reply_to: int | None = None,
        urgency: str = "normal",
    ) -> dict[str, Any]:
        request = {"to": to, "body": body, "reply_to": reply_to, "urgency": urgency}
        body_hash = hashlib.sha256(canonical_json(request).encode()).hexdigest()
        return mutation(
            self.connection,
            self._identity(actor),
            request_id,
            "message.posted",
            f"message:{to}",
            body_hash,
            lambda connection: Messages(connection).post(actor, **request),
        )

    def ack(
        self,
        request_id: str,
        actor: str,
        terminal_run_id: int,
        persistent: bool,
        message_ids: list[int],
    ) -> dict[str, Any]:
        request = {
            "message_ids": message_ids,
            "terminal_run_id": terminal_run_id,
            "persistent": persistent,
        }
        body_hash = hashlib.sha256(canonical_json(request).encode()).hexdigest()
        return mutation(
            self.connection,
            self._identity(actor),
            request_id,
            "inbox.acknowledged",
            f"inbox:{actor}",
            body_hash,
            lambda connection: Messages(connection).ack(actor, terminal_run_id, persistent, message_ids),
        )
