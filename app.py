"""
NITIN — Ship Detection & Classification System
Full integrated Flask backend:
  • User Authentication (admin / command_post / viewer)
  • Role-based access control
  • Messaging system
  • AI Detection Pipeline (admin only)
  • MongoDB: ship_detection_db (pipeline) + user_list (auth/messages)
"""

from flask import Flask, jsonify, request, send_from_directory, session
from flask_cors import CORS
from pymongo import MongoClient, DESCENDING
from pymongo.errors import ConnectionFailure
from bson import ObjectId
from pathlib import Path
from datetime import datetime, timedelta
import os, threading, hashlib, secrets, functools
import importlib, sys

# ── Pipeline import ───────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
try:
    pipeline = importlib.import_module("arena_test")
    PIPELINE_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] Could not import arena_test: {e}")
    PIPELINE_AVAILABLE = False

# ── Flask setup ───────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = secrets.token_hex(32)   # change to fixed string in production
CORS(app,
     supports_credentials=True,
     origins="*",                   # allow any device on the network
     allow_headers=["Content-Type", "X-Auth-Token"],
     methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])

MONGO_URI = "mongodb://localhost:27017/"

# ── Global pipeline state ─────────────────────────────────────────────────────
pipeline_status = {"running":False,"progress":0,"total":0,"current_file":"","log":[],"finished":False,"error":None}
models = {"yolo":None,"cls":None,"device":None,"loaded":False}

# ══════════════════════════════════════════════════════════════════════════════
# DB HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_user_db():
    """Returns (db, client) for user_list database."""
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
        return client["user_list"], client
    except Exception as e:
        print(f"[DB] user_list connection error: {e}")
        return None, None

def get_pipeline_db():
    """Returns (db, client) for ship_detection_db."""
    if not PIPELINE_AVAILABLE:
        return None, None
    return pipeline.connect_to_mongodb()

def ensure_indexes():
    """Create indexes on first run."""
    db, client = get_user_db()
    if db is None:
        return
    try:
        db["users"].create_index("email", unique=True)
        db["users"].create_index("username", unique=True)
        db["messages"].create_index([("created_at", DESCENDING)])
        db["messages"].create_index("recipients")
        db["messages"].create_index("sender_id")
        print("✓ MongoDB indexes ensured")
    finally:
        if client: client.close()

# ── Password hashing ──────────────────────────────────────────────────────────
def hash_password(password: str, salt: str = None):
    if salt is None:
        salt = secrets.token_hex(16)
    hashed = hashlib.sha256((salt + password).encode()).hexdigest()
    return hashed, salt

def verify_password(password: str, hashed: str, salt: str):
    return hashlib.sha256((salt + password).encode()).hexdigest() == hashed

# ── Token store (in-memory; swap for Redis in production) ────────────────────
_tokens = {}   # token -> {user_id, role, username, expires}

def create_token(user_doc):
    token = secrets.token_hex(32)
    _tokens[token] = {
        "user_id":  str(user_doc["_id"]),
        "role":     user_doc["role"],
        "username": user_doc["username"],
        "name":     user_doc["name"],
        "state":    user_doc.get("state",""),
        "expires":  datetime.utcnow() + timedelta(hours=12),
    }
    return token

def get_token_data(token):
    data = _tokens.get(token)
    if not data:
        return None
    if datetime.utcnow() > data["expires"]:
        del _tokens[token]
        return None
    return data

def revoke_token(token):
    _tokens.pop(token, None)

# ── Auth decorator ────────────────────────────────────────────────────────────
def require_auth(*allowed_roles):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            token = request.headers.get("X-Auth-Token") or request.cookies.get("nitin_token")
            if not token:
                return jsonify({"error":"Unauthorized"}), 401
            data = get_token_data(token)
            if not data:
                return jsonify({"error":"Session expired"}), 401
            if allowed_roles and data["role"] not in allowed_roles:
                return jsonify({"error":"Forbidden"}), 403
            request.user = data
            return fn(*args, **kwargs)
        return wrapper
    return decorator

# ── Indian states list ────────────────────────────────────────────────────────
INDIA_STATES = [
    "Andhra Pradesh","Arunachal Pradesh","Assam","Bihar","Chhattisgarh",
    "Goa","Gujarat","Haryana","Himachal Pradesh","Jharkhand","Karnataka",
    "Kerala","Madhya Pradesh","Maharashtra","Manipur","Meghalaya","Mizoram",
    "Nagaland","Odisha","Punjab","Rajasthan","Sikkim","Tamil Nadu","Telangana",
    "Tripura","Uttar Pradesh","Uttarakhand","West Bengal","Delhi",
    "Jammu & Kashmir","Ladakh","Andaman & Nicobar Islands","Chandigarh",
    "Dadra & Nagar Haveli and Daman & Diu","Lakshadweep","Puducherry",
]

def serialize_doc(d):
    """Convert MongoDB document for JSON serialization."""
    d = dict(d)
    for k, v in d.items():
        if isinstance(v, ObjectId):
            d[k] = str(v)
        elif isinstance(v, datetime):
            d[k] = v.isoformat()
    return d

# ══════════════════════════════════════════════════════════════════════════════
# AUTH ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/auth/signup", methods=["POST"])
def signup():
    data = request.json or {}
    required = ["name","username","email","password","role","dob"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    role = data["role"]
    if role not in ("admin","command_post","viewer"):
        return jsonify({"error":"Invalid role"}), 400

    if role == "command_post" and not data.get("state"):
        return jsonify({"error":"State is required for command post"}), 400

    hashed, salt = hash_password(data["password"])

    user = {
        "name":       data["name"].strip(),
        "username":   data["username"].strip().lower(),
        "email":      data["email"].strip().lower(),
        "password":   hashed,
        "salt":       salt,
        "role":       role,
        "dob":        data["dob"],
        "state":      data.get("state","").strip() if role == "command_post" else "",
        "position":   data.get("position","").strip(),
        "created_at": datetime.utcnow(),
        "active":     True,
    }

    db, client = get_user_db()
    if db is None:
        return jsonify({"error":"Database unavailable"}), 503
    try:
        result = db["users"].insert_one(user)
        return jsonify({"message":"Account created successfully","user_id":str(result.inserted_id)}), 201
    except Exception as e:
        if "duplicate key" in str(e).lower() or "E11000" in str(e):
            return jsonify({"error":"Username or email already exists"}), 409
        return jsonify({"error":str(e)}), 500
    finally:
        if client: client.close()


@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.json or {}
    identifier = (data.get("username") or data.get("email","")).strip().lower()
    password   = data.get("password","")

    if not identifier or not password:
        return jsonify({"error":"Username/email and password required"}), 400

    db, client = get_user_db()
    if db is None:
        return jsonify({"error":"Database unavailable"}), 503
    try:
        user = db["users"].find_one({"$or":[{"username":identifier},{"email":identifier}]})
        if not user:
            return jsonify({"error":"Invalid credentials"}), 401
        if not verify_password(password, user["password"], user["salt"]):
            return jsonify({"error":"Invalid credentials"}), 401
        if not user.get("active", True):
            return jsonify({"error":"Account disabled"}), 403

        token = create_token(user)
        resp = jsonify({
            "token":    token,
            "user": {
                "id":       str(user["_id"]),
                "name":     user["name"],
                "username": user["username"],
                "email":    user["email"],
                "role":     user["role"],
                "state":    user.get("state",""),
                "position": user.get("position",""),
            }
        })
        resp.set_cookie("nitin_token", token, httponly=True, max_age=43200, samesite="Lax")
        return resp
    finally:
        if client: client.close()


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    token = request.headers.get("X-Auth-Token") or request.cookies.get("nitin_token")
    if token:
        revoke_token(token)
    resp = jsonify({"message":"Logged out"})
    resp.delete_cookie("nitin_token")
    return resp


@app.route("/api/auth/me", methods=["GET"])
@require_auth()
def get_me():
    return jsonify({"user": request.user})


@app.route("/api/auth/states", methods=["GET"])
def get_states():
    return jsonify(INDIA_STATES)


# ══════════════════════════════════════════════════════════════════════════════
# USER MANAGEMENT (admin only)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/users", methods=["GET"])
@require_auth("admin")
def list_users():
    db, client = get_user_db()
    if db is None:
        return jsonify([])
    try:
        users = list(db["users"].find({}, {"password":0,"salt":0}))
        return jsonify([serialize_doc(u) for u in users])
    finally:
        if client: client.close()


@app.route("/api/users/<user_id>/toggle", methods=["POST"])
@require_auth("admin")
def toggle_user(user_id):
    db, client = get_user_db()
    if db is None:
        return jsonify({"error":"DB unavailable"}), 503
    try:
        user = db["users"].find_one({"_id":ObjectId(user_id)})
        if not user:
            return jsonify({"error":"User not found"}), 404
        new_status = not user.get("active", True)
        db["users"].update_one({"_id":ObjectId(user_id)},{"$set":{"active":new_status}})
        return jsonify({"active":new_status})
    finally:
        if client: client.close()


@app.route("/api/users/recipients", methods=["GET"])
@require_auth("admin","command_post")
def get_recipients():
    """List users that the current user can message."""
    db, client = get_user_db()
    if db is None:
        return jsonify([])
    try:
        role = request.user["role"]
        current_id = request.user["user_id"]
        # admin can message everyone; command_post can message admin + other command_posts + viewers
        users = list(db["users"].find(
            {"active":True, "_id":{"$ne":ObjectId(current_id)}},
            {"password":0,"salt":0}
        ))
        return jsonify([serialize_doc(u) for u in users])
    finally:
        if client: client.close()


# ══════════════════════════════════════════════════════════════════════════════
# MESSAGING ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/messages/send", methods=["POST"])
@require_auth("admin","command_post")
def send_message():
    data = request.json or {}
    body = data.get("body","").strip()
    recipients = data.get("recipients",[])  # list of user_id strings; empty = broadcast

    if not body:
        return jsonify({"error":"Message body required"}), 400

    role = request.user["role"]
    # Viewers cannot send messages (enforced by require_auth)
    # command_post cannot send to specific users outside their allowed scope
    # (they can send to anyone — validated by recipient filtering in UI)

    db, client = get_user_db()
    if db is None:
        return jsonify({"error":"DB unavailable"}), 503
    try:
        # Resolve recipient docs to validate they exist
        if recipients:
            rec_ids = [ObjectId(r) for r in recipients]
        else:
            # broadcast: all active users except sender
            all_users = db["users"].find(
                {"active":True,"_id":{"$ne":ObjectId(request.user["user_id"])}},
                {"_id":1}
            )
            rec_ids = [u["_id"] for u in all_users]

        msg = {
            "sender_id":    request.user["user_id"],
            "sender_name":  request.user["name"],
            "sender_role":  request.user["role"],
            "body":         body,
            "subject":      data.get("subject",""),
            "recipients":   [str(r) for r in rec_ids],
            "is_broadcast": not bool(data.get("recipients",[])),
            "created_at":   datetime.utcnow(),
            "read_by":      [],
        }
        result = db["messages"].insert_one(msg)
        return jsonify({"message_id":str(result.inserted_id),"sent_to":len(rec_ids)}), 201
    finally:
        if client: client.close()


@app.route("/api/messages/inbox", methods=["GET"])
@require_auth()
def get_inbox():
    db, client = get_user_db()
    if db is None:
        return jsonify([])
    try:
        uid = request.user["user_id"]
        # User sees messages where they are a recipient
        msgs = list(db["messages"].find(
            {"recipients": uid},
            sort=[("created_at", DESCENDING)],
            limit=100
        ))
        return jsonify([serialize_doc(m) for m in msgs])
    finally:
        if client: client.close()


@app.route("/api/messages/sent", methods=["GET"])
@require_auth("admin","command_post")
def get_sent():
    db, client = get_user_db()
    if db is None:
        return jsonify([])
    try:
        uid = request.user["user_id"]
        msgs = list(db["messages"].find(
            {"sender_id": uid},
            sort=[("created_at", DESCENDING)],
            limit=100
        ))
        return jsonify([serialize_doc(m) for m in msgs])
    finally:
        if client: client.close()


@app.route("/api/messages/<msg_id>/read", methods=["POST"])
@require_auth()
def mark_read(msg_id):
    db, client = get_user_db()
    if db is None:
        return jsonify({"ok":False})
    try:
        uid = request.user["user_id"]
        db["messages"].update_one(
            {"_id":ObjectId(msg_id)},
            {"$addToSet":{"read_by": uid}}
        )
        return jsonify({"ok":True})
    finally:
        if client: client.close()


@app.route("/api/messages/unread_count", methods=["GET"])
@require_auth()
def unread_count():
    db, client = get_user_db()
    if db is None:
        return jsonify({"count":0})
    try:
        uid = request.user["user_id"]
        count = db["messages"].count_documents(
            {"recipients": uid, "read_by": {"$nin":[uid]}}
        )
        return jsonify({"count":count})
    finally:
        if client: client.close()


# ══════════════════════════════════════════════════════════════════════════════
# EXISTING PIPELINE ROUTES (admin only for write, all roles for read)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/status", methods=["GET"])
@require_auth()
def api_status():
    db, client = get_pipeline_db()
    mongo_ok = db is not None
    if client: client.close()
    return jsonify({
        "pipeline_available": PIPELINE_AVAILABLE,
        "mongodb_connected":  mongo_ok,
        "models_loaded":      models["loaded"],
        "pipeline_running":   pipeline_status["running"],
        "user_role":          request.user["role"],
    })


@app.route("/api/models/load", methods=["POST"])
@require_auth("admin")
def load_models():
    if not PIPELINE_AVAILABLE:
        return jsonify({"error":"Pipeline module not available"}), 503
    if models["loaded"]:
        return jsonify({"message":"Models already loaded"})
    try:
        models["yolo"], models["cls"], models["device"] = pipeline.initialize_models()
        models["loaded"] = True
        return jsonify({"message":"Models loaded successfully","device":str(models["device"])})
    except Exception as e:
        return jsonify({"error":str(e)}), 500


def _run_pipeline_thread():
    global pipeline_status
    pipeline_status["log"] = []
    pipeline_status["error"] = None
    pipeline_status["finished"] = False
    db, mongo_client = get_pipeline_db()
    try:
        input_folder   = Path(pipeline.CONFIG["input_folder"])
        image_extensions = [".jpg",".jpeg",".png",".bmp",".tiff"]
        image_files    = [f for f in input_folder.iterdir() if f.suffix.lower() in image_extensions]
        pipeline_status["total"]    = len(image_files)
        pipeline_status["progress"] = 0
        if not image_files:
            pipeline_status["log"].append("⚠ No images found in input folder.")
            return
        for idx, image_file in enumerate(image_files, 1):
            pipeline_status["current_file"] = image_file.name
            pipeline_status["log"].append(f"[{idx}/{len(image_files)}] Processing: {image_file.name}")
            try:
                pipeline.process_image(str(image_file), models["yolo"], models["cls"], models["device"], db)
                pipeline_status["log"].append(f"  ✓ Done: {image_file.name}")
            except Exception as e:
                pipeline_status["log"].append(f"  ✗ Error: {image_file.name} — {str(e)}")
            pipeline_status["progress"] = idx
    except Exception as e:
        pipeline_status["error"] = str(e)
        pipeline_status["log"].append(f"✗ Fatal error: {str(e)}")
    finally:
        if mongo_client: mongo_client.close()
        pipeline_status["running"]  = False
        pipeline_status["finished"] = True
        pipeline_status["log"].append("✓ Pipeline complete.")


@app.route("/api/pipeline/run", methods=["POST"])
@require_auth("admin")
def run_pipeline():
    if pipeline_status["running"]:
        return jsonify({"error":"Pipeline already running"}), 409
    if not models["loaded"]:
        return jsonify({"error":"Models not loaded. Call /api/models/load first."}), 400
    pipeline_status["running"] = True
    t = threading.Thread(target=_run_pipeline_thread, daemon=True)
    t.start()
    return jsonify({"message":"Pipeline started"})


@app.route("/api/pipeline/status", methods=["GET"])
@require_auth()
def pipeline_progress():
    return jsonify({
        "running":      pipeline_status["running"],
        "progress":     pipeline_status["progress"],
        "total":        pipeline_status["total"],
        "current_file": pipeline_status["current_file"],
        "log":          pipeline_status["log"][-30:],
        "finished":     pipeline_status["finished"],
        "error":        pipeline_status["error"],
    })


@app.route("/api/input/images", methods=["GET"])
@require_auth("admin")
def list_input_images():
    if not PIPELINE_AVAILABLE: return jsonify([])
    input_folder = Path(pipeline.CONFIG["input_folder"])
    if not input_folder.exists(): return jsonify([])
    exts = {".jpg",".jpeg",".png",".bmp",".tiff"}
    return jsonify([f.name for f in input_folder.iterdir() if f.suffix.lower() in exts])


@app.route("/api/images/ship/<filename>")
@require_auth()
def serve_ship_image(filename):
    """
    Serve ship image from MongoDB base64 field.
    Pipeline stores annotated_image (or original_image) as base64 in the DB.
    This decodes it and returns as a real image response — works on all devices.
    """
    db, client = get_pipeline_db()
    if db is None:
        return jsonify({"error": "Database not connected"}), 503
    try:
        # Search all date collections for this image_name
        doc = None
        for col_name in db.list_collection_names():
            doc = db[col_name].find_one(
                {"image_name": filename},
                {"annotated_image": 1, "original_image": 1,
                 "annotated_image_format": 1, "original_image_format": 1}
            )
            if doc:
                break

        if not doc:
            # Fallback: try serving from disk (if file exists locally)
            if PIPELINE_AVAILABLE:
                folder = pipeline.CONFIG.get("output_object_folder", "")
                disk_path = os.path.join(folder, filename)
                if os.path.isfile(disk_path):
                    return send_from_directory(folder, filename)
            return jsonify({"error": "Image not found"}), 404

        # Prefer annotated image, fall back to original
        b64_data = doc.get("annotated_image") or doc.get("original_image")
        if not b64_data:
            return jsonify({"error": "No image data stored in DB"}), 404

        import base64
        img_bytes = base64.b64decode(b64_data)

        # Detect image format from first bytes
        if img_bytes[:3] == b'\xff\xd8\xff':
            mime = "image/jpeg"
        elif img_bytes[:8] == b'\x89PNG\r\n\x1a\n':
            mime = "image/png"
        else:
            mime = "image/jpeg"  # default

        from flask import Response
        return Response(
            img_bytes,
            mimetype=mime,
            headers={
                "Cache-Control": "public, max-age=3600",
                "Content-Length": str(len(img_bytes)),
            }
        )
    except Exception as e:
        return jsonify({"error": f"Image serve error: {str(e)}"}), 500
    finally:
        if client:
            client.close()


@app.route("/api/db/collections", methods=["GET"])
@require_auth()
def list_collections():
    db, client = get_pipeline_db()
    if db is None:
        return jsonify({"error":"MongoDB not connected"}), 503
    try:
        cols = sorted(db.list_collection_names(), reverse=True)
        return jsonify(cols)
    finally:
        if client: client.close()


# Fields to ALWAYS strip — these base64 blobs are huge and break JSON responses
_EXCLUDE_BLOB_FIELDS = {
    "original_image": 0,
    "annotated_image": 0,
    "original_image_format": 0,
    "annotated_image_format": 0,
}


@app.route("/api/db/<collection_name>", methods=["GET"])
@require_auth()
def get_records(collection_name):
    """
    Returns all records for a date collection.
    Base64 image blobs are ALWAYS excluded — they are served via /api/images/ship/<filename>.
    Supports ?skip=N&limit=N for pagination (default: all records, no limit).
    """
    db, client = get_pipeline_db()
    if db is None:
        return jsonify({"error":"MongoDB not connected"}), 503
    try:
        col   = db[collection_name]
        skip  = int(request.args.get("skip",  0))
        limit = int(request.args.get("limit", 0))   # 0 = no limit

        cursor = col.find({}, _EXCLUDE_BLOB_FIELDS).skip(skip)
        if limit > 0:
            cursor = cursor.limit(limit)

        docs = list(cursor)
        for d in docs:
            d["_id"] = str(d["_id"])
            if isinstance(d.get("date_time"),    datetime): d["date_time"]    = d["date_time"].isoformat()
            if isinstance(d.get("processed_at"), datetime): d["processed_at"] = d["processed_at"].isoformat()
        return jsonify(docs)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if client: client.close()


@app.route("/api/db/all/map", methods=["GET"])
@require_auth()
def get_all_for_map():
    db, client = get_pipeline_db()
    if db is None: return jsonify([])
    try:
        all_records = []
        for col_name in db.list_collection_names():
            col  = db[col_name]
            # Only fetch fields needed for map display — never fetch base64 blobs
            docs = list(col.find({},{
                "_id":1,"image_name":1,"ship_class":1,
                "classification_confidence":1,"date_time":1,
                "number_of_objects":1,"latitude":1,"longitude":1,
            }))
            for d in docs:
                d["_id"] = str(d["_id"])
                if isinstance(d.get("date_time"), datetime): d["date_time"] = d["date_time"].isoformat()
                d["collection"] = col_name
            all_records.extend(docs)
        return jsonify(all_records)
    finally:
        if client: client.close()


@app.route("/api/stats", methods=["GET"])
@require_auth()
def get_stats():
    db, client = get_pipeline_db()
    if db is None:
        return jsonify({"total_ships":0,"collections":0,"class_distribution":{}})
    try:
        total = 0
        class_dist = {}
        for col_name in db.list_collection_names():
            col = db[col_name]
            for doc in col.find({},{"ship_class":1,"number_of_objects":1}):
                total += doc.get("number_of_objects",1)
                cls = doc.get("ship_class","unknown")
                class_dist[cls] = class_dist.get(cls,0) + 1
        return jsonify({"total_ships":total,"collections":len(db.list_collection_names()),"class_distribution":class_dist})
    finally:
        if client: client.close()


@app.route("/api/config", methods=["GET"])
@require_auth()
def get_config():
    if not PIPELINE_AVAILABLE: return jsonify({})
    return jsonify({
        "input_folder":       pipeline.CONFIG["input_folder"],
        "output_ship_folder": pipeline.CONFIG["output_object_folder"],
        "output_no_ship_folder": pipeline.CONFIG["output_no_object_folder"],
        "confidence_threshold":  pipeline.CONFIG["confidence_threshold"],
        "class_names":           pipeline.class_names,
    })


# ══════════════════════════════════════════════════════════════════════════════
# SERVE FRONTEND
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def root():
    return send_from_directory("static","login.html")

@app.route("/dashboard")
def dashboard():
    return send_from_directory("static","index.html")

@app.route("/login")
def login_page():
    return send_from_directory("static","login.html")


# ══════════════════════════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    ensure_indexes()

    # Print all network interfaces so user knows what URL to share with other devices
    import socket
    hostname = socket.gethostname()
    try:
        lan_ip = socket.gethostbyname(hostname)
    except Exception:
        lan_ip = "unknown"

    print("\n" + "="*60)
    print("  NITIN — Ship Detection System")
    print(f"  Local  : http://localhost:5000")
    print(f"  Network: http://{lan_ip}:5000   ← share this with other devices")
    print("="*60 + "\n")
    app.run(debug=True, host="0.0.0.0", port=5000)
