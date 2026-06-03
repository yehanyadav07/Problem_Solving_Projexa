import os
import hashlib
import mimetypes
import random
import re
import json
import base64
from math import asin, cos, radians, sin, sqrt
from datetime import datetime

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
    jsonify,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

try:
    import google.generativeai as genai
except ImportError:
    genai = None

try:
    import requests as http_requests
except ImportError:
    http_requests = None


BASE_DIR = os.path.abspath(os.path.dirname(__file__))
if load_dotenv:
    load_dotenv(os.path.join(BASE_DIR, ".env"))

UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change-this-in-production")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(BASE_DIR, "smart_ewaste.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["ADMIN_EMAIL"] = os.getenv("ADMIN_EMAIL", "admin@smartewaste.local")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GOOGLE_VISION_API_KEY = os.getenv("GOOGLE_VISION_API_KEY", "")

if GEMINI_API_KEY and genai:
    genai.configure(api_key=GEMINI_API_KEY)

PREDICTION_CACHE = {}
IP_LOCATION_CACHE = {}

db = SQLAlchemy(app)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    eco_credits = db.Column(db.Integer, default=0, nullable=False)
    requests = db.relationship("RecyclingRequest", backref="user", lazy=True)


class Center(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    location = db.Column(db.String(200), nullable=False)
    accepted_items = db.Column(db.String(300), nullable=False)
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    osm_id = db.Column(db.String(50), nullable=True, unique=True)
    requests = db.relationship("RecyclingRequest", backref="center", lazy=True)


class RecyclingRequest(db.Model):
    __tablename__ = "request"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    center_id = db.Column(db.Integer, db.ForeignKey("center.id"), nullable=True)
    item_type = db.Column(db.String(80), nullable=False)
    status = db.Column(db.String(50), default="Pending", nullable=False)
    credits_awarded = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return db.session.get(User, user_id)


def is_admin(user) -> bool:
    return bool(user and user.email.lower() == app.config["ADMIN_EMAIL"].lower())


def login_required():
    return get_current_user() is not None


def validate_password_strength(password: str):
    if len(password) < 8:
        return False, "Password must be at least 8 characters long."
    if not re.search(r"[A-Z]", password):
        return False, "Password must include at least one uppercase letter."
    if not re.search(r"[a-z]", password):
        return False, "Password must include at least one lowercase letter."
    if not re.search(r"\d", password):
        return False, "Password must include at least one number."
    if not re.search(r"[!@#$%^&*()_\-+=\[\]{};:,.?/\\|`~]", password):
        return False, "Password must include at least one special character."
    return True, ""


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    c = 2 * asin(sqrt(a))
    return r * c


EWASTE_CATEGORIES = {
    "smartphone": {
        "credits": 25,
        "description": "Smartphones contain valuable materials like gold, silver, palladium, and rare earth elements in their circuit boards. Proper recycling can recover these materials.",
        "environmental_impact": "Recycling one smartphone saves enough energy to charge a laptop for 44 hours. It also prevents hazardous materials like lead and mercury from entering landfills.",
    },
    "battery": {
        "credits": 15,
        "description": "Batteries contain lithium, cobalt, nickel, and manganese. These materials are finite and their extraction has significant environmental costs.",
        "environmental_impact": "Recycling batteries prevents heavy metals from contaminating soil and groundwater. Recovered lithium can be used in new batteries, reducing mining needs.",
    },
    "laptop": {
        "credits": 45,
        "description": "Laptops contain copper wiring, aluminum casing, gold-plated connectors, and rare earth magnets. They are one of the most valuable e-waste items to recycle.",
        "environmental_impact": "Recycling one laptop can save up to 800 kg of CO2 emissions compared to manufacturing from raw materials. It also recovers up to 99% of plastic components.",
    },
    "tablet": {
        "credits": 30,
        "description": "Tablets contain similar materials to smartphones but in larger quantities. Their lithium batteries and circuit boards are particularly valuable for recovery.",
        "environmental_impact": "Proper tablet recycling prevents the release of brominated flame retardants and other toxic substances into the environment.",
    },
    "headphones": {
        "credits": 10,
        "description": "Headphones contain copper wiring, neodymium magnets, and various plastics that can be separated and recycled.",
        "environmental_impact": "While small individually, the collective impact of recycling headphones is significant given billions are produced annually worldwide.",
    },
    "charger": {
        "credits": 8,
        "description": "Chargers contain copper wiring, circuit boards with tin and lead solder, and plastic casings.",
        "environmental_impact": "Billions of chargers end up in landfills each year. Recycling helps recover copper and reduces the 11,000 tonnes of charger e-waste generated annually in Europe alone.",
    },
    "desktop": {
        "credits": 50,
        "description": "Desktop computers are among the most valuable e-waste items, containing significant amounts of copper, gold, aluminum, and various rare metals.",
        "environmental_impact": "Recycling a single desktop PC can recover over 1 kg of copper and prevent the release of mercury from switches and fluorescent backlights.",
    },
    "monitor": {
        "credits": 35,
        "description": "Monitors contain glass, LED components, circuit boards, and plastic. Older CRT monitors also contain lead, making proper recycling critical.",
        "environmental_impact": "A single CRT monitor can contain up to 3.6 kg of lead. Proper recycling prevents this from leaching into groundwater.",
    },
    "printer": {
        "credits": 30,
        "description": "Printers contain motors, circuit boards, plastic, and sometimes residual ink containing volatile organic compounds.",
        "environmental_impact": "Recycling printers and their cartridges saves significant amounts of oil and prevents plastic waste.",
    },
    "keyboard": {
        "credits": 8,
        "description": "Keyboards contain circuit boards, plastic keycaps, rubber membranes, and copper contacts that can all be separated and recycled.",
        "environmental_impact": "While small individually, keyboards contribute to the growing pile of peripheral e-waste. Recycling helps recover plastics and metals.",
    },
    "mouse": {
        "credits": 5,
        "description": "Computer mice contain small circuit boards, optical sensors, and plastic components.",
        "environmental_impact": "Recycling mice prevents small electronics from entering landfills where their components can release harmful substances.",
    },
    "camera": {
        "credits": 20,
        "description": "Cameras contain precision optics, image sensors, lithium batteries, and circuit boards with valuable metals.",
        "environmental_impact": "Camera sensors contain rare earth elements whose mining has significant environmental costs. Recycling helps reduce demand for new extraction.",
    },
    "television": {
        "credits": 40,
        "description": "TVs contain large amounts of glass, plastic, copper wiring, and LED/LCD panels. Smart TVs also have complex circuit boards.",
        "environmental_impact": "Recycling a TV prevents up to 4 kg of lead (in older models) and recovers valuable indium from LCD panels.",
    },
    "gaming_console": {
        "credits": 35,
        "description": "Gaming consoles contain high-performance processors, memory chips, fans, and custom circuit boards with valuable metals.",
        "environmental_impact": "Console recycling recovers copper, gold, and rare earth elements while preventing harmful plastics from entering the waste stream.",
    },
    "speaker": {
        "credits": 12,
        "description": "Speakers contain neodymium magnets, copper voice coils, and various plastics and wood materials.",
        "environmental_impact": "Neodymium magnets in speakers are made from rare earth elements. Recycling helps reduce the environmental damage from rare earth mining.",
    },
    "router": {
        "credits": 10,
        "description": "Routers contain circuit boards, antennas with copper elements, and plastic casings.",
        "environmental_impact": "With the rapid evolution of WiFi standards, routers become obsolete quickly. Recycling helps recover valuable circuit board materials.",
    },
    "cable": {
        "credits": 5,
        "description": "Cables primarily contain copper or aluminum conductors wrapped in plastic insulation.",
        "environmental_impact": "Cable recycling is one of the most efficient forms of e-waste recovery, as copper can be recycled indefinitely without quality loss.",
    },
    "other_electronic": {
        "credits": 15,
        "description": "This electronic device contains circuit boards, wiring, and various metals that can be recovered through proper recycling processes.",
        "environmental_impact": "Every electronic device recycled helps reduce the 50+ million tonnes of global e-waste generated annually.",
    },
}


def fetch_nearby_recyclers(lat, lon, radius_km=25):
    if not http_requests:
        return []

    radius_m = int(radius_km * 1000)
    overpass_url = "https://overpass-api.de/api/interpreter"
    query = f"""
    [out:json][timeout:15];
    (
      node["amenity"="recycling"](around:{radius_m},{lat},{lon});
      way["amenity"="recycling"](around:{radius_m},{lat},{lon});
      node["shop"="electronics"]["second_hand"="yes"](around:{radius_m},{lat},{lon});
      node["amenity"="waste_disposal"](around:{radius_m},{lat},{lon});
      node["recycling_type"="centre"](around:{radius_m},{lat},{lon});
      node["industrial"="scrap_yard"](around:{radius_m},{lat},{lon});
    );
    out center body;
    """

    try:
        resp = http_requests.post(overpass_url, data={"data": query}, timeout=15)
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception:
        return []

    results = []
    seen_ids = set()
    for element in data.get("elements", []):
        osm_id = str(element.get("id", ""))
        if osm_id in seen_ids:
            continue
        seen_ids.add(osm_id)

        tags = element.get("tags", {})
        name = tags.get("name", "")
        if not name:
            amenity = tags.get("amenity", tags.get("shop", "recycling"))
            name = f"Recycling Point ({amenity.replace('_', ' ').title()})"

        c_lat = element.get("lat") or element.get("center", {}).get("lat")
        c_lon = element.get("lon") or element.get("center", {}).get("lon")
        if c_lat is None or c_lon is None:
            continue

        address_parts = []
        for key in ("addr:street", "addr:housenumber", "addr:city", "addr:suburb", "addr:postcode"):
            val = tags.get(key)
            if val:
                address_parts.append(val)
        location = ", ".join(address_parts) if address_parts else tags.get("description", f"{c_lat:.4f}, {c_lon:.4f}")

        dist = haversine_km(lat, lon, c_lat, c_lon)

        accepted = []
        recycling_keys = [k for k in tags if k.startswith("recycling:")]
        for rk in recycling_keys:
            if tags[rk] == "yes":
                item_name = rk.replace("recycling:", "").replace("_", " ").title()
                accepted.append(item_name)
        if not accepted:
            accepted = ["General E-Waste"]

        results.append({
            "osm_id": osm_id,
            "name": name,
            "location": location,
            "latitude": c_lat,
            "longitude": c_lon,
            "distance_km": round(dist, 2),
            "accepted_items": ", ".join(accepted),
            "tags": tags,
        })

    results.sort(key=lambda x: x["distance_km"])
    return results[:15]


def get_or_create_center(center_data):
    osm_id = center_data.get("osm_id", "")
    if osm_id:
        existing = Center.query.filter_by(osm_id=osm_id).first()
        if existing:
            return existing

    center = Center(
        name=center_data["name"],
        location=center_data["location"],
        accepted_items=center_data.get("accepted_items", "General E-Waste"),
        latitude=center_data.get("latitude"),
        longitude=center_data.get("longitude"),
        osm_id=osm_id if osm_id else None,
    )
    db.session.add(center)
    db.session.commit()
    return center


def image_hash(image_path: str) -> str:
    h = hashlib.md5()
    with open(image_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()



def analyze_with_vision_api(image_path: str, image_bytes: bytes = None) -> dict:
    """Call Google Cloud Vision API to extract labels, objects, logos and web context.
    Pass pre-read image_bytes to avoid a redundant disk read when the caller already
    has the bytes in memory.
    Returns a dict with keys: labels, objects, logos, web_entities (all lists of strings).
    Returns empty dict if API key not configured or on any error.
    """
    if not GOOGLE_VISION_API_KEY or not http_requests:
        return {}

    try:
        if image_bytes is None:
            with open(image_path, "rb") as f:
                image_bytes = f.read()

        image_b64 = base64.b64encode(image_bytes).decode("utf-8")
        url = f"https://vision.googleapis.com/v1/images:annotate?key={GOOGLE_VISION_API_KEY}"
        payload = {
            "requests": [{
                "image": {"content": image_b64},
                "features": [
                    {"type": "LABEL_DETECTION", "maxResults": 10},
                    {"type": "OBJECT_LOCALIZATION", "maxResults": 5},
                    {"type": "LOGO_DETECTION", "maxResults": 3},
                    {"type": "WEB_DETECTION", "maxResults": 5},
                ],
            }]
        }

        resp = http_requests.post(url, json=payload, timeout=15)
        if resp.status_code != 200:
            app.logger.warning(f"Vision API HTTP {resp.status_code}: {resp.text[:200]}")
            return {}

        data = resp.json()
        response = data.get("responses", [{}])[0]

        labels = [a.get("description", "") for a in response.get("labelAnnotations", [])]
        objects = [o.get("name", "") for o in response.get("localizedObjectAnnotations", [])]
        logos = [l.get("description", "") for l in response.get("logoAnnotations", [])]
        web_entities = [
            e.get("description", "")
            for e in response.get("webDetection", {}).get("webEntities", [])
        ]

        return {
            "labels": [x for x in labels if x],
            "objects": [x for x in objects if x],
            "logos": [x for x in logos if x],
            "web_entities": [x for x in web_entities if x],
        }
    except Exception as e:
        app.logger.warning(f"Vision API error: {e}")
        return {}


def predict_ewaste_with_gemini(image_path: str) -> dict:
    img_hash = image_hash(image_path)
    if img_hash in PREDICTION_CACHE:
        return PREDICTION_CACHE[img_hash]

    result = _run_prediction(image_path, img_hash)
    PREDICTION_CACHE[img_hash] = result
    return result


def _run_prediction(image_path: str, img_hash: str) -> dict:
    if GEMINI_API_KEY and genai:
        try:
            # Read image bytes once; reused by both Vision API and Gemini
            with open(image_path, "rb") as f:
                image_bytes = f.read()

            vision_data = analyze_with_vision_api(image_path, image_bytes=image_bytes)
            hints = []
            if vision_data.get("labels"):
                hints.append(f"Detected image labels: {', '.join(vision_data['labels'])}")
            if vision_data.get("objects"):
                hints.append(f"Localised objects in image: {', '.join(vision_data['objects'])}")
            if vision_data.get("logos"):
                hints.append(f"Brand/logo recognised: {', '.join(vision_data['logos'])}")
            if vision_data.get("web_entities"):
                hints.append(f"Web context matches: {', '.join(vision_data['web_entities'])}")
            vision_hints = (
                "\n\nPre-analysis from Google Cloud Vision API (use this to improve accuracy):\n"
                + "\n".join(hints)
            ) if hints else ""

            generation_config = genai.types.GenerationConfig(
                temperature=0.0,
                candidate_count=1,
            )
            model = genai.GenerativeModel(
                "gemini-2.0-flash",
                generation_config=generation_config,
            )

            mime_type, _ = mimetypes.guess_type(image_path)
            if not mime_type:
                mime_type = "image/jpeg"

            image_part = {"mime_type": mime_type, "data": image_bytes}

            prompt = (
                "You are an expert e-waste classifier and environmental consultant.\n"
                "Analyse this image carefully and identify the electronic or electrical device shown.\n\n"
                "RULES:\n"
                "- Identify ANY electronic/electrical device — there is NO restricted list.\n"
                "  Be specific: e.g. 'Smartwatch', 'VR Headset', 'Electric Toothbrush',\n"
                "  'CRT Monitor', 'Cordless Drill', 'Smart Thermostat', 'Drone', 'E-Reader',\n"
                "  'Solar Inverter', 'Medical Glucometer', 'Hair Straightener', etc.\n"
                "- A laptop has a screen hinged to a keyboard. A desktop is a tower/box only.\n"
                "- A tablet is a flat touchscreen with no physical keyboard.\n"
                "- Estimate eco-credits (5–100) based on size, material value (gold/copper/\n"
                "  rare-earths = higher), and recycling complexity.\n"
                "  Guidance: small cable ~5, mouse ~5, headphones ~10, battery ~15,\n"
                "  smartphone ~25, tablet ~30, laptop ~45, desktop ~50.\n"
                "- Write description and environmental_impact specific to this exact device type.\n"
                + vision_hints
                + "\n\nRespond with ONLY valid JSON, no markdown, no explanation:\n"
                "{\n"
                '    "is_ewaste": true or false,\n'
                '    "item_type": "<specific device type, title case, e.g. Smartwatch>",\n'
                '    "confidence": <integer 1-100>,\n'
                '    "specific_name": "<brand/model if visible, else empty string>",\n'
                '    "condition": "working" or "damaged" or "broken" or "unknown",\n'
                '    "credits": <integer 5-100>,\n'
                '    "description": "<materials it contains and why it should be recycled>",\n'
                '    "environmental_impact": "<concrete benefit of recycling this specific item>"\n'
                "}"
            )

            response = model.generate_content([prompt, image_part])
            raw = response.text.strip()
            if raw.startswith("```"):
                raw = "\n".join(l for l in raw.splitlines() if not l.strip().startswith("```"))
            result = json.loads(raw.strip())

            if not isinstance(result, dict) or "is_ewaste" not in result:
                raise ValueError("Invalid response structure")

            if not result.get("is_ewaste", False):
                specific = result.get("specific_name", "a non-electronic item")
                return {
                    "item_type": "Not E-Waste",
                    "credits": 0,
                    "confidence": result.get("confidence", 80),
                    "description": f"This appears to be: {specific}. Only electronic devices can be recycled here.",
                    "environmental_impact": "Please upload an image of an electronic device.",
                    "is_ewaste": False,
                    "condition": "unknown",
                }

            item_type = result.get("item_type", "Electronic Device").strip() or "Electronic Device"
            condition = result.get("condition", "unknown")
            confidence = int(result.get("confidence", 85))

            credits = max(5, min(100, int(result.get("credits", 15))))
            if condition == "working":
                credits = int(credits * 1.2)
            elif condition == "broken":
                credits = int(credits * 0.7)
            credits = max(5, min(120, credits))

            specific = result.get("specific_name", "").strip()
            description = result.get("description", "")
            if specific:
                description = f"Identified as: {specific}. " + description
            environmental_impact = result.get("environmental_impact", "")

            return {
                "item_type": item_type,
                "credits": credits,
                "confidence": confidence,
                "description": description,
                "environmental_impact": environmental_impact,
                "is_ewaste": True,
                "condition": condition,
            }

        except Exception as e:
            app.logger.warning(f"Gemini API error: {e}, falling back to deterministic prediction")

    return _deterministic_fallback(image_path, img_hash)


def _deterministic_fallback(image_path: str, img_hash: str) -> dict:
    filename = os.path.basename(image_path).lower()

    filename_hints = {
        "phone": "smartphone", "mobile": "smartphone", "iphone": "smartphone",
        "android": "smartphone", "samsung": "smartphone", "pixel": "smartphone",
        "batter": "battery", "laptop": "laptop", "notebook": "laptop",
        "macbook": "laptop", "thinkpad": "laptop", "tablet": "tablet",
        "ipad": "tablet", "headphone": "headphones", "earphone": "headphones",
        "earbud": "headphones", "airpod": "headphones", "charger": "charger",
        "cable": "cable", "usb": "cable", "desktop": "desktop",
        "tower": "desktop", "pc": "desktop", "monitor": "monitor",
        "screen": "monitor", "display": "monitor", "printer": "printer",
        "keyboard": "keyboard", "mouse": "mouse", "camera": "camera",
        "tv": "television", "television": "television", "console": "gaming_console",
        "xbox": "gaming_console", "playstation": "gaming_console",
        "nintendo": "gaming_console", "speaker": "speaker",
        "router": "router", "modem": "router",
    }

    detected_type = None
    for hint, ewaste_type in filename_hints.items():
        if hint in filename:
            detected_type = ewaste_type
            break

    if not detected_type:
        order = [
            "smartphone", "laptop", "battery", "tablet", "headphones",
            "charger", "desktop", "monitor", "keyboard", "mouse",
            "camera", "speaker",
        ]
        hash_int = int(img_hash, 16)
        detected_type = order[hash_int % len(order)]
        confidence = 55 + (hash_int % 20)
    else:
        hash_int = int(img_hash, 16)
        confidence = 72 + (hash_int % 20)

    category = EWASTE_CATEGORIES.get(detected_type, EWASTE_CATEGORIES["other_electronic"])
    return {
        "item_type": detected_type.replace("_", " ").title(),
        "credits": category["credits"],
        "confidence": confidence,
        "description": category["description"],
        "environmental_impact": category["environmental_impact"],
        "is_ewaste": True,
        "condition": "unknown",
    }


@app.route("/")
def index():
    if login_required():
        return redirect(url_for("dashboard"))
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not username or not email or not password:
            flash("All fields are required.", "danger")
            return redirect(url_for("register"))
        is_valid, password_message = validate_password_strength(password)
        if not is_valid:
            flash(password_message, "warning")
            return redirect(url_for("register"))

        existing_user = User.query.filter((User.username == username) | (User.email == email)).first()
        if existing_user:
            flash("Username or email already exists.", "warning")
            return redirect(url_for("register"))

        user = User(
            username=username,
            email=email,
            password_hash=generate_password_hash(password),
            eco_credits=0,
        )
        db.session.add(user)
        db.session.commit()

        flash("Registration successful. Please login.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        identifier = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        user = User.query.filter(
            (User.email == identifier.lower()) | (User.username == identifier)
        ).first()
        if user and check_password_hash(user.password_hash, password):
            session["user_id"] = user.id
            flash(f"Welcome back, {user.username}!", "success")
            return redirect(url_for("dashboard"))

        flash("Invalid email or password.", "danger")
        return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("user_id", None)
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


@app.route("/dashboard")
def dashboard():
    user = get_current_user()
    if not user:
        flash("Please login to continue.", "warning")
        return redirect(url_for("login"))

    user_requests = (
        RecyclingRequest.query.filter_by(user_id=user.id)
        .order_by(RecyclingRequest.created_at.desc())
        .all()
    )
    pending_credits = sum(
        r.credits_awarded for r in user_requests if r.status == "Pending"
    )
    return render_template(
        "dashboard.html",
        user=user,
        requests=user_requests,
        is_admin_user=is_admin(user),
        pending_credits=pending_credits,
    )


@app.route("/leaderboard")
def leaderboard():
    user = get_current_user()
    if not user:
        flash("Please login to continue.", "warning")
        return redirect(url_for("login"))

    top_users = User.query.order_by(User.eco_credits.desc(), User.username.asc()).all()
    return render_template("leaderboard.html", users=top_users, user=user)


@app.route("/scanner", methods=["GET", "POST"])
def scanner():
    user = get_current_user()
    if not user:
        flash("Please login to continue.", "warning")
        return redirect(url_for("login"))

    result = None
    centers = []
    uploaded_image = None
    user_coords = None

    if request.method == "POST":
        if "ewaste_image" not in request.files:
            flash("No file uploaded.", "danger")
            return redirect(url_for("scanner"))

        file = request.files["ewaste_image"]
        if file.filename == "":
            flash("Please choose an image file.", "warning")
            return redirect(url_for("scanner"))

        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            saved_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{filename}"
            save_path = os.path.join(app.config["UPLOAD_FOLDER"], saved_name)
            file.save(save_path)

            result = predict_ewaste_with_gemini(save_path)
            uploaded_image = saved_name

            lat = request.form.get("latitude", "").strip()
            lon = request.form.get("longitude", "").strip()
            try:
                user_lat = float(lat)
                user_lon = float(lon)
                user_coords = {"lat": user_lat, "lon": user_lon}
            except ValueError:
                user_lat = None
                user_lon = None

            if result.get("is_ewaste", True) and user_lat is not None and user_lon is not None:
                nearby = fetch_nearby_recyclers(user_lat, user_lon)
                centers = nearby
        else:
            flash("Invalid file type. Upload png/jpg/jpeg/webp only.", "danger")
            return redirect(url_for("scanner"))

    return render_template(
        "scanner.html",
        result=result,
        centers=centers,
        uploaded_image=uploaded_image,
        user_coords=user_coords,
        user=user,
    )


@app.route("/api/nearby-recyclers")
def api_nearby_recyclers():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Login required"}), 401

    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    radius = request.args.get("radius", default=25, type=int)

    if lat is None or lon is None:
        return jsonify({"error": "lat and lon parameters required"}), 400

    results = fetch_nearby_recyclers(lat, lon, radius_km=radius)
    return jsonify({"centers": results, "count": len(results)})


@app.route("/api/geolocate")
def api_geolocate():
    """Return approximate lat/lon for the requesting IP via ipapi.co.
    Used as a fallback when the browser denies GPS access.
    """
    user = get_current_user()
    if not user:
        return jsonify({"error": "Login required"}), 401

    if not http_requests:
        return jsonify({"error": "requests library not available"}), 500

    # Resolve real client IP (handles reverse-proxy X-Forwarded-For header)
    forwarded = request.headers.get("X-Forwarded-For", "")
    client_ip = forwarded.split(",")[0].strip() if forwarded else request.remote_addr

    # ipapi.co cannot geolocate private/loopback addresses
    def _is_private_ip(ip):
        if any(ip.startswith(p) for p in ("127.", "10.", "192.168.", "::1", "localhost")):
            return True
        # RFC 1918: 172.16.0.0/12 covers 172.16–172.31
        if ip.startswith("172."):
            try:
                return 16 <= int(ip.split(".")[1]) <= 31
            except (IndexError, ValueError):
                pass
        return False

    if _is_private_ip(client_ip):
        return jsonify({"error": "Cannot geolocate a local/private IP address. "
                                  "Grant browser GPS permission instead."}), 400

    if client_ip in IP_LOCATION_CACHE:
        return jsonify(IP_LOCATION_CACHE[client_ip])

    try:
        resp = http_requests.get(
            f"https://ipapi.co/{client_ip}/json/",
            headers={"User-Agent": "SmartEWaste/1.0"},
            timeout=6,
        )
        if resp.status_code != 200:
            return jsonify({"error": "IP geolocation service unavailable"}), 502

        data = resp.json()
        lat = data.get("latitude")
        lon = data.get("longitude")
        if not lat or not lon:
            return jsonify({"error": "Location not found for this IP"}), 404

        result = {
            "lat": lat,
            "lon": lon,
            "city": data.get("city", ""),
            "region": data.get("region", ""),
            "country": data.get("country_name", ""),
            "method": "ip",
        }
        IP_LOCATION_CACHE[client_ip] = result
        return jsonify(result)
    except Exception as e:
        app.logger.warning(f"IP geolocation error: {e}")
        return jsonify({"error": "Geolocation request failed"}), 500


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


@app.route("/schedule_pickup/<int:center_id>", methods=["POST"])
def schedule_pickup(center_id):
    user = get_current_user()
    if not user:
        flash("Please login to continue.", "warning")
        return redirect(url_for("login"))

    center = db.session.get(Center, center_id)
    if not center:
        flash("Recycling center not found.", "danger")
        return redirect(url_for("scanner"))

    item_type = request.form.get("item_type", "").strip()
    credits = int(request.form.get("credits", 0))

    if not item_type:
        flash("Missing item type for pickup.", "danger")
        return redirect(url_for("scanner"))

    new_request = RecyclingRequest(
        user_id=user.id,
        center_id=center.id,
        item_type=item_type,
        status="Pending",
        credits_awarded=credits,
    )

    db.session.add(new_request)
    db.session.commit()

    flash(f"Pickup scheduled with {center.name}. {credits} Eco-Credits will be awarded once collected!", "success")
    return redirect(url_for("dashboard"))


@app.route("/schedule_pickup_dynamic", methods=["POST"])
def schedule_pickup_dynamic():
    user = get_current_user()
    if not user:
        flash("Please login to continue.", "warning")
        return redirect(url_for("login"))

    item_type = request.form.get("item_type", "").strip()
    credits = int(request.form.get("credits", 0))
    center_name = request.form.get("center_name", "").strip()
    center_location = request.form.get("center_location", "").strip()
    center_lat = request.form.get("center_lat", type=float)
    center_lon = request.form.get("center_lon", type=float)
    osm_id = request.form.get("osm_id", "").strip()

    if not item_type or not center_name:
        flash("Missing required information.", "danger")
        return redirect(url_for("scanner"))

    center_data = {
        "osm_id": osm_id,
        "name": center_name,
        "location": center_location,
        "latitude": center_lat,
        "longitude": center_lon,
        "accepted_items": "General E-Waste",
    }
    center = get_or_create_center(center_data)

    new_request = RecyclingRequest(
        user_id=user.id,
        center_id=center.id,
        item_type=item_type,
        status="Pending",
        credits_awarded=credits,
    )

    db.session.add(new_request)
    db.session.commit()

    flash(f"Pickup scheduled with {center.name}. {credits} Eco-Credits will be awarded once collected!", "success")
    return redirect(url_for("dashboard"))


@app.route("/cancel_pickup/<int:request_id>", methods=["POST"])
def cancel_pickup(request_id):
    user = get_current_user()
    if not user:
        flash("Please login to continue.", "warning")
        return redirect(url_for("login"))

    recycle_request = db.session.get(RecyclingRequest, request_id)
    if not recycle_request or recycle_request.user_id != user.id:
        flash("Pickup request not found.", "danger")
        return redirect(url_for("dashboard"))

    if recycle_request.status != "Pending":
        flash("Only pending pickups can be cancelled.", "warning")
        return redirect(url_for("dashboard"))

    recycle_request.status = "Cancelled"
    db.session.commit()
    flash("Pickup cancelled.", "info")
    return redirect(url_for("dashboard"))


@app.route("/admin/requests")
def admin_requests():
    user = get_current_user()
    if not user:
        flash("Please login to continue.", "warning")
        return redirect(url_for("login"))
    if not is_admin(user):
        flash("Admin access required.", "danger")
        return redirect(url_for("dashboard"))

    requests_list = RecyclingRequest.query.order_by(RecyclingRequest.created_at.desc()).all()
    return render_template("admin_requests.html", requests=requests_list, user=user)


@app.route("/admin/requests/<int:request_id>/collect", methods=["POST"])
def mark_request_collected(request_id):
    user = get_current_user()
    if not user:
        flash("Please login to continue.", "warning")
        return redirect(url_for("login"))
    if not is_admin(user):
        flash("Admin access required.", "danger")
        return redirect(url_for("dashboard"))

    recycle_request = db.session.get(RecyclingRequest, request_id)
    if not recycle_request:
        flash("Request not found.", "danger")
        return redirect(url_for("admin_requests"))

    recycle_request.status = "Collected"
    requester = db.session.get(User, recycle_request.user_id)
    if requester:
        requester.eco_credits += recycle_request.credits_awarded
    db.session.commit()
    flash(f"Request #{request_id} marked as Collected. {recycle_request.credits_awarded} Eco-Credits awarded to user!", "success")
    return redirect(url_for("admin_requests"))


def ensure_center_coordinates():
    columns = {col["name"] for col in inspect(db.engine).get_columns("center")}
    with db.engine.begin() as connection:
        if "latitude" not in columns:
            connection.execute(text("ALTER TABLE center ADD COLUMN latitude FLOAT"))
        if "longitude" not in columns:
            connection.execute(text("ALTER TABLE center ADD COLUMN longitude FLOAT"))
        if "osm_id" not in columns:
            connection.execute(text("ALTER TABLE center ADD COLUMN osm_id VARCHAR(50)"))


def ensure_request_center_column():
    columns = {col["name"] for col in inspect(db.engine).get_columns("request")}
    if "center_id" in columns:
        return
    with db.engine.begin() as connection:
        connection.execute(text("ALTER TABLE request ADD COLUMN center_id INTEGER"))


if __name__ == "__main__":
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    with app.app_context():
        db.create_all()
        ensure_center_coordinates()
        ensure_request_center_column()
    app.run(debug=True)
