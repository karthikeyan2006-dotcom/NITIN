"""
auth.py  —  NITIN User Authentication Module
=============================================
Handles:
  • Persistent MongoDB connection to 'user_list' database
  • Password hashing (SHA-256 + salt)
  • Token-based session management (in-memory store)
  • require_auth() decorator for route protection
  • All /api/auth/* and /api/users/* routes

MongoDB database : user_list
Collections      : users, (messages stored in messaging.py)

User document schema:
  {
    name, username, email, password (hashed), salt,
    role (admin|command_post|viewer),
    dob, position, state (for command_post), active, created_at
  }
"""

from flask import Blueprint, jsonify, request
from pymongo import MongoClient, ASCENDING
from pymongo.errors import ConnectionFailure, DuplicateKeyError
from bson import ObjectId
from datetime import datetime, timedelta
import hashlib, secrets, functools

# ══════════════════════════════════════════════════════════════════════════════
# BLUEPRINT
# ══════════════════════════════════════════════════════════════════════════════
auth_bp = Blueprint("auth", __name__)

# ══════════════════════════════════════════════════════════════════════════════
# PERSISTENT MONGODB CONNECTION
# Using a module-level client so ONE connection is reused across all requests.
# MongoClient is thread-safe by design.
# ══════════════════════════════════════════════════════════════════════════════
MONGO_URI = "mongodb://localhost:27017/"

_mongo_client = None
_user_db      = None

def get_user_db():
    """
    Returns the 'user_list' database using a persistent MongoClient.
    Creates the client on first call; reuses it on all subsequent calls.
    """
    global _mongo_client, _user_db
    if _mongo_client is None:
        try:
            _mongo_client = MongoClient(
                MONGO_URI,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000,
                socketTimeoutMS=10000,
            )
            _mongo_client.admin.command("ping")   # verify connection
            _user_db = _mongo_client["user_list"]
            print("[AUTH] ✓ Connected to MongoDB  →  user_list")
        except Exception as e:
            print(f"[AUTH] ✗ MongoDB connection failed: {e}")
            _mongo_client = None
            _user_db      = None
    return _user_db


def ensure_indexes():
    """
    Call once at startup to create required indexes.
    Safe to call multiple times (idempotent).
    """
    db = get_user_db()
    if db is None:
        print("[AUTH] ⚠ Skipping index creation — DB unavailable")
        return
    db["users"].create_index("email",    unique=True, background=True)
    db["users"].create_index("username", unique=True, background=True)
    db["users"].create_index([("role", ASCENDING)],  background=True)
    print("[AUTH] ✓ Indexes ensured on user_list.users")


# ══════════════════════════════════════════════════════════════════════════════
# INDIA STATES
# ══════════════════════════════════════════════════════════════════════════════
INDIA_STATES = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh",
    "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka",
    "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya",
    "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim",
    "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand",
    "West Bengal", "Delhi", "Jammu & Kashmir", "Ladakh",
    "Andaman & Nicobar Islands", "Chandigarh",
    "Dadra & Nagar Haveli and Daman & Diu", "Lakshadweep", "Puducherry",
]

# ══════════════════════════════════════════════════════════════════════════════
# PASSWORD HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def hash_password(password: str, salt: str = None):
    if salt is None:
        salt = secrets.token_hex(16)
    hashed = hashlib.sha256((salt + password).encode()).hexdigest()
    return hashed, salt


def verify_password(password: str, hashed: str, salt: str) -> bool:
    return hashlib.sha256((salt + password).encode()).hexdigest() == hashed


# ══════════════════════════════════════════════════════════════════════════════
# TOKEN STORE  (in-memory — thread-safe for single-process Flask)
# ══════════════════════════════════════════════════════════════════════════════
_tokens: dict = {}   # token_string -> payload dict


def create_token(user_doc: dict) -> str:
    token = secrets.token_hex(32)
    _tokens[token] = {
        "user_id":  str(user_doc["_id"]),
        "role":     user_doc["role"],
        "username": user_doc["username"],
        "name":     user_doc["name"],
        "state":    user_doc.get("state", ""),
        "email":    user_doc.get("email", ""),
        "position": user_doc.get("position", ""),
        "expires":  datetime.utcnow() + timedelta(hours=12),
    }
    return token


def get_token_data(token: str) -> dict | None:
    if not token:
        return None
    data = _tokens.get(token)
    if not data:
        return None
    if datetime.utcnow() > data["expires"]:
        del _tokens[token]
        return None
    return data


def revoke_token(token: str):
    _tokens.pop(token, None)


# ══════════════════════════════════════════════════════════════════════════════
# AUTH DECORATOR — import this in app.py and messaging.py
# ══════════════════════════════════════════════════════════════════════════════
def require_auth(*allowed_roles):
    """
    Usage:
        @require_auth()                    — any logged-in user
        @require_auth("admin")             — admin only
        @require_auth("admin","command_post") — admin or command_post
    Sets request.user = token payload dict on success.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            token = (
                request.headers.get("X-Auth-Token")
                or request.cookies.get("nitin_token")
            )
            if not token:
                return jsonify({"error": "Unauthorized — no token"}), 401
            data = get_token_data(token)
            if not data:
                return jsonify({"error": "Session expired — please log in again"}), 401
            if allowed_roles and data["role"] not in allowed_roles:
                return jsonify({"error": "Forbidden — insufficient role"}), 403
            request.user = data
            return fn(*args, **kwargs)
        return wrapper
    return decorator


# ══════════════════════════════════════════════════════════════════════════════
# SERIALIZER
# ══════════════════════════════════════════════════════════════════════════════
def serialize_doc(d: dict) -> dict:
    d = dict(d)
    for k, v in d.items():
        if isinstance(v, ObjectId):
            d[k] = str(v)
        elif isinstance(v, datetime):
            d[k] = v.isoformat()
    return d


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES — /api/auth/*
# ══════════════════════════════════════════════════════════════════════════════

@auth_bp.route("/api/auth/signup", methods=["POST"])
def signup():
    data = request.get_json(silent=True) or {}

    # ── Validate required fields ──
    required = ["name", "username", "email", "password", "role", "dob"]
    missing  = [f for f in required if not str(data.get(f, "")).strip()]
    if missing:
        return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400

    role = data["role"].strip()
    if role not in ("admin", "command_post", "viewer"):
        return jsonify({"error": "Invalid role. Must be admin, command_post or viewer."}), 400

    if role == "command_post" and not data.get("state", "").strip():
        return jsonify({"error": "State is required for command post accounts."}), 400

    password = data["password"]
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters."}), 400

    hashed, salt = hash_password(password)

    user_doc = {
        "name":       data["name"].strip(),
        "username":   data["username"].strip().lower(),
        "email":      data["email"].strip().lower(),
        "password":   hashed,
        "salt":       salt,
        "role":       role,
        "dob":        data["dob"].strip(),
        "state":      data.get("state", "").strip() if role == "command_post" else "",
        "position":   data.get("position", "").strip(),
        "active":     True,
        "created_at": datetime.utcnow(),
    }

    db = get_user_db()
    if db is None:
        return jsonify({"error": "Database unavailable. Check MongoDB is running."}), 503

    try:
        result = db["users"].insert_one(user_doc)
        return jsonify({
            "message": "Account created successfully.",
            "user_id": str(result.inserted_id),
        }), 201
    except DuplicateKeyError:
        return jsonify({"error": "Username or email already exists."}), 409
    except Exception as e:
        return jsonify({"error": f"Database error: {str(e)}"}), 500


@auth_bp.route("/api/auth/login", methods=["POST"])
def login():
    data       = request.get_json(silent=True) or {}
    identifier = (data.get("username") or data.get("email") or "").strip().lower()
    password   = data.get("password", "")

    if not identifier or not password:
        return jsonify({"error": "Username/email and password are required."}), 400

    db = get_user_db()
    if db is None:
        return jsonify({"error": "Database unavailable. Check MongoDB is running."}), 503

    user = db["users"].find_one({
        "$or": [{"username": identifier}, {"email": identifier}]
    })
    if not user:
        return jsonify({"error": "Invalid credentials."}), 401
    if not verify_password(password, user["password"], user["salt"]):
        return jsonify({"error": "Invalid credentials."}), 401
    if not user.get("active", True):
        return jsonify({"error": "Account is disabled. Contact admin."}), 403

    token = create_token(user)
    resp  = jsonify({
        "token": token,
        "user": {
            "id":       str(user["_id"]),
            "name":     user["name"],
            "username": user["username"],
            "email":    user["email"],
            "role":     user["role"],
            "state":    user.get("state", ""),
            "position": user.get("position", ""),
        },
    })
    resp.set_cookie(
        "nitin_token", token,
        httponly=True, max_age=43200, samesite="Lax"
    )
    return resp


@auth_bp.route("/api/auth/logout", methods=["POST"])
def logout():
    token = (
        request.headers.get("X-Auth-Token")
        or request.cookies.get("nitin_token")
    )
    if token:
        revoke_token(token)
    resp = jsonify({"message": "Logged out successfully."})
    resp.delete_cookie("nitin_token")
    return resp


@auth_bp.route("/api/auth/me", methods=["GET"])
@require_auth()
def get_me():
    return jsonify({"user": request.user})


@auth_bp.route("/api/auth/states", methods=["GET"])
def get_states():
    """Public endpoint — returns India state list for signup form."""
    return jsonify(INDIA_STATES)


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES — /api/users/*  (admin management)
# ══════════════════════════════════════════════════════════════════════════════

@auth_bp.route("/api/users", methods=["GET"])
@require_auth("admin")
def list_users():
    db = get_user_db()
    if db is None:
        return jsonify({"error": "DB unavailable"}), 503
    users = list(db["users"].find({}, {"password": 0, "salt": 0}))
    return jsonify([serialize_doc(u) for u in users])


@auth_bp.route("/api/users/<user_id>/toggle", methods=["POST"])
@require_auth("admin")
def toggle_user(user_id):
    db = get_user_db()
    if db is None:
        return jsonify({"error": "DB unavailable"}), 503
    try:
        user = db["users"].find_one({"_id": ObjectId(user_id)})
        if not user:
            return jsonify({"error": "User not found"}), 404
        new_status = not user.get("active", True)
        db["users"].update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"active": new_status}}
        )
        return jsonify({"active": new_status})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@auth_bp.route("/api/users/recipients", methods=["GET"])
@require_auth("admin", "command_post")
def get_recipients():
    """Returns list of users the current user can send messages to."""
    db = get_user_db()
    if db is None:
        return jsonify([])
    current_id = request.user["user_id"]
    users = list(db["users"].find(
        {"active": True, "_id": {"$ne": ObjectId(current_id)}},
        {"password": 0, "salt": 0}
    ))
    return jsonify([serialize_doc(u) for u in users])
