"""
messaging.py  —  NITIN Messaging Module
=========================================
Handles all inter-user messaging stored in MongoDB user_list.messages

Message document schema:
  {
    sender_id   : str (user _id),
    sender_name : str,
    sender_role : str,
    subject     : str,
    body        : str,
    recipients  : [str, ...],   list of user _id strings
    is_broadcast: bool,
    created_at  : datetime,
    read_by     : [str, ...],   list of user _id strings who have read it
  }

Role permissions:
  admin        → send to anyone, broadcast to all
  command_post → send to anyone (admin, other CPs, viewers)
  viewer       → READ ONLY (inbox only, no send)
"""

from flask import Blueprint, jsonify, request
from pymongo import DESCENDING
from bson import ObjectId
from datetime import datetime

# Import shared DB connection and auth decorator from auth.py
from auth import get_user_db, require_auth, serialize_doc, ensure_indexes as _auth_ensure_indexes

# ══════════════════════════════════════════════════════════════════════════════
# BLUEPRINT
# ══════════════════════════════════════════════════════════════════════════════
messaging_bp = Blueprint("messaging", __name__)


def ensure_msg_indexes():
    """
    Create indexes on messages collection.
    Called once at app startup.
    """
    db = get_user_db()
    if db is None:
        print("[MESSAGING] ⚠ Skipping index creation — DB unavailable")
        return
    db["messages"].create_index([("created_at", DESCENDING)], background=True)
    db["messages"].create_index("recipients",                  background=True)
    db["messages"].create_index("sender_id",                   background=True)
    print("[MESSAGING] ✓ Indexes ensured on user_list.messages")


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@messaging_bp.route("/api/messages/send", methods=["POST"])
@require_auth("admin", "command_post")   # viewers cannot send
def send_message():
    """
    Send a message to specific recipients OR broadcast to all active users.

    Request JSON:
      {
        "subject":    "(optional)",
        "body":       "Message text (required)",
        "recipients": ["user_id_1", "user_id_2"]   ← omit or [] for broadcast
      }
    """
    data = request.get_json(silent=True) or {}
    body    = data.get("body", "").strip()
    subject = data.get("subject", "").strip()
    recipient_ids = data.get("recipients", [])

    if not body:
        return jsonify({"error": "Message body is required."}), 400

    db = get_user_db()
    if db is None:
        return jsonify({"error": "Database unavailable."}), 503

    sender_id = request.user["user_id"]

    # ── Resolve recipients ──────────────────────────────────────────────────
    if recipient_ids:
        # Validate every supplied ID actually exists and is active
        try:
            oid_list = [ObjectId(r) for r in recipient_ids]
        except Exception:
            return jsonify({"error": "One or more recipient IDs are invalid."}), 400

        valid_users = list(db["users"].find(
            {"_id": {"$in": oid_list}, "active": True},
            {"_id": 1}
        ))
        resolved_ids = [str(u["_id"]) for u in valid_users]
        is_broadcast = False
    else:
        # Broadcast — every active user except the sender
        all_active = list(db["users"].find(
            {"active": True, "_id": {"$ne": ObjectId(sender_id)}},
            {"_id": 1}
        ))
        resolved_ids = [str(u["_id"]) for u in all_active]
        is_broadcast = True

    if not resolved_ids:
        return jsonify({"error": "No valid recipients found."}), 400

    msg_doc = {
        "sender_id":    sender_id,
        "sender_name":  request.user["name"],
        "sender_role":  request.user["role"],
        "subject":      subject,
        "body":         body,
        "recipients":   resolved_ids,
        "is_broadcast": is_broadcast,
        "created_at":   datetime.utcnow(),
        "read_by":      [],
    }

    try:
        result = db["messages"].insert_one(msg_doc)
        return jsonify({
            "message_id": str(result.inserted_id),
            "sent_to":    len(resolved_ids),
            "broadcast":  is_broadcast,
        }), 201
    except Exception as e:
        return jsonify({"error": f"Failed to save message: {str(e)}"}), 500


@messaging_bp.route("/api/messages/inbox", methods=["GET"])
@require_auth()   # all roles can read inbox
def get_inbox():
    """Returns all messages where current user is a recipient. Newest first."""
    db = get_user_db()
    if db is None:
        return jsonify([])

    uid  = request.user["user_id"]
    msgs = list(db["messages"].find(
        {"recipients": uid},
        sort=[("created_at", DESCENDING)],
        limit=200,
    ))
    return jsonify([serialize_doc(m) for m in msgs])


@messaging_bp.route("/api/messages/sent", methods=["GET"])
@require_auth("admin", "command_post")
def get_sent():
    """Returns all messages sent by the current user. Newest first."""
    db = get_user_db()
    if db is None:
        return jsonify([])

    uid  = request.user["user_id"]
    msgs = list(db["messages"].find(
        {"sender_id": uid},
        sort=[("created_at", DESCENDING)],
        limit=200,
    ))
    return jsonify([serialize_doc(m) for m in msgs])


@messaging_bp.route("/api/messages/<msg_id>", methods=["GET"])
@require_auth()
def get_message(msg_id):
    """Fetch a single message by ID (must be sender or recipient)."""
    db  = get_user_db()
    if db is None:
        return jsonify({"error": "DB unavailable"}), 503

    uid = request.user["user_id"]
    try:
        msg = db["messages"].find_one({"_id": ObjectId(msg_id)})
    except Exception:
        return jsonify({"error": "Invalid message ID"}), 400

    if not msg:
        return jsonify({"error": "Message not found"}), 404

    # Access control: only sender or recipients can view
    if uid != msg["sender_id"] and uid not in msg.get("recipients", []):
        return jsonify({"error": "Forbidden"}), 403

    return jsonify(serialize_doc(msg))


@messaging_bp.route("/api/messages/<msg_id>/read", methods=["POST"])
@require_auth()
def mark_read(msg_id):
    """Mark a message as read by the current user."""
    db  = get_user_db()
    if db is None:
        return jsonify({"ok": False, "error": "DB unavailable"}), 503

    uid = request.user["user_id"]
    try:
        db["messages"].update_one(
            {"_id": ObjectId(msg_id), "recipients": uid},
            {"$addToSet": {"read_by": uid}}
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@messaging_bp.route("/api/messages/unread_count", methods=["GET"])
@require_auth()
def unread_count():
    """Returns count of messages in inbox that have not been read yet."""
    db  = get_user_db()
    if db is None:
        return jsonify({"count": 0})

    uid   = request.user["user_id"]
    count = db["messages"].count_documents(
        {"recipients": uid, "read_by": {"$nin": [uid]}}
    )
    return jsonify({"count": count})


@messaging_bp.route("/api/messages/<msg_id>/delete", methods=["DELETE"])
@require_auth("admin")
def delete_message(msg_id):
    """Admin only — permanently delete a message."""
    db = get_user_db()
    if db is None:
        return jsonify({"error": "DB unavailable"}), 503
    try:
        result = db["messages"].delete_one({"_id": ObjectId(msg_id)})
        if result.deleted_count == 0:
            return jsonify({"error": "Message not found"}), 404
        return jsonify({"ok": True, "deleted": msg_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
