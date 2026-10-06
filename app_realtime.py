from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from groq import Groq
from datetime import datetime, date, timedelta
from urllib.parse import quote
import os
import json
import copy
import re
import requests


# =========================================================
# ENVIRONMENT
# =========================================================

load_dotenv(override=True)

SECRET_KEY = os.getenv("SECRET_KEY", "ai-travel-planner-secret-key").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
MODEL_NAME = os.getenv("MODEL_NAME", "openai/gpt-oss-120b").strip()
POLLINATIONS_API_KEY = os.getenv("POLLINATIONS_API_KEY", "").strip()

from location_service import enrich_itinerary_locations
from railway_service import get_live_trains


# =========================================================
# FLASK
# =========================================================

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///travel_planner.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"


db = SQLAlchemy(app)


groq_client = None
if GROQ_API_KEY:
    try:
        groq_client = Groq(api_key=GROQ_API_KEY, timeout=45)
    except Exception as exc:
        print("Groq initialization warning:", exc)


# =========================================================
# MODELS
# =========================================================

class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    trips = db.relationship(
        "Trip",
        backref="user",
        lazy=True,
        cascade="all, delete-orphan"
    )


class Trip(db.Model):
    __tablename__ = "trips"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    starting_location = db.Column(db.String(150), nullable=False)
    destination = db.Column(db.String(150), nullable=False)
    travel_date = db.Column(db.String(30), nullable=False)
    days = db.Column(db.Integer, nullable=False)
    persons = db.Column(db.Integer, nullable=False)
    budget = db.Column(db.Float, nullable=False)
    travel_preference = db.Column(db.String(50), nullable=False)
    transport_preference = db.Column(db.String(50), nullable=False, default="Any")
    stay_preference = db.Column(db.String(50), nullable=False)
    food_preference = db.Column(db.String(50), nullable=False)
    activity_preference = db.Column(db.String(100), nullable=False)
    ai_result = db.Column(db.Text, nullable=True)
    is_saved = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def activities_preference(self):
        return self.activity_preference


# =========================================================
# DATABASE SETUP + MIGRATION
# =========================================================

def setup_database():
    with app.app_context():
        db.create_all()

        try:
            inspector = db.inspect(db.engine)

            user_columns = {
                column["name"]
                for column in inspector.get_columns("users")
            }

            if "created_at" not in user_columns:
                with db.engine.begin() as connection:
                    connection.exec_driver_sql(
                        "ALTER TABLE users ADD COLUMN created_at DATETIME"
                    )
                    connection.exec_driver_sql(
                        "UPDATE users SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"
                    )

            inspector = db.inspect(db.engine)

            trip_columns = {
                column["name"]
                for column in inspector.get_columns("trips")
            }

            if "is_saved" not in trip_columns:
                with db.engine.begin() as connection:
                    connection.exec_driver_sql(
                        "ALTER TABLE trips ADD COLUMN is_saved BOOLEAN DEFAULT 0"
                    )

            if "transport_preference" not in trip_columns:
                with db.engine.begin() as connection:
                    connection.exec_driver_sql(
                        "ALTER TABLE trips ADD COLUMN transport_preference VARCHAR(50)"
                    )

            print("Database setup completed successfully.")

        except Exception as exc:
            print("Database migration warning:", exc)


# =========================================================
# HELPERS
# =========================================================

def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    try:
        return db.session.get(User, user_id)
    except Exception:
        return None


@app.context_processor
def inject_current_user():
    return {"current_user": current_user()}


def require_login():
    return current_user() is not None


def safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        if isinstance(value, str):
            value = value.replace("₹", "").replace(",", "").strip()
        return float(value)
    except (TypeError, ValueError):
        return default


def clean_text(value, default=""):
    value = str(value or "").strip()
    return value if value else default


def parse_date(value):
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def parse_json(value, default=None):
    if default is None:
        default = {}
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return default

    text = str(value).strip()
    text = re.sub(r"^```json\s*", "", text, flags=re.I)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except Exception:
        pass

    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        try:
            return json.loads(text[first:last + 1])
        except Exception:
            pass

    return default


def destination_key(destination):
    return re.sub(r"[^a-z0-9]+", " ", str(destination or "").lower()).strip()


def slugify(value):
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", str(value or "").strip()).strip("_").lower()
    return slug or "destination"


# =========================================================
# FINAL BUDGET CALCULATION
# =========================================================

def calculate_final_budget(budget_limit, transport, hotel, food, activities, other):
    """Python is the authority for the final amount shown on the website."""

    budget_limit = max(0.0, safe_float(budget_limit))

    costs = {
        "transport": max(0.0, safe_float(transport)),
        "hotel": max(0.0, safe_float(hotel)),
        "food": max(0.0, safe_float(food)),
        "activities": max(0.0, safe_float(activities)),
        "other": max(0.0, safe_float(other)),
    }

    estimate = sum(costs.values())

    # Keep a 5% reserve for normal budgets.
    spending_limit = budget_limit if budget_limit < 2000 else round(budget_limit * 0.95, 2)

    if estimate <= 0:
        # Practical fallback when AI returns missing or zero cost fields.
        fallback = {
            "transport": budget_limit * 0.25,
            "hotel": budget_limit * 0.25,
            "food": budget_limit * 0.20,
            "activities": budget_limit * 0.15,
            "other": budget_limit * 0.10,
        }
        costs = {key: round(value, 2) for key, value in fallback.items()}
        estimate = sum(costs.values())

    if estimate > spending_limit and estimate > 0:
        factor = spending_limit / estimate
        for key in costs:
            costs[key] = round(costs[key] * factor, 2)

    total = round(sum(costs.values()), 2)

    # Rounding correction goes into Other.
    difference = round(spending_limit - total, 2)
    if abs(difference) >= 0.01:
        costs["other"] = round(max(0.0, costs["other"] + difference), 2)

    total = round(sum(costs.values()), 2)

    # Absolute ceiling check.
    if total > budget_limit:
        overflow = round(total - budget_limit, 2)
        costs["other"] = round(max(0.0, costs["other"] - overflow), 2)
        total = round(sum(costs.values()), 2)

    remaining = round(max(0.0, budget_limit - total), 2)

    return {
        "transport": costs["transport"],
        "hotel": costs["hotel"],
        "food": costs["food"],
        "activities": costs["activities"],
        "other": costs["other"],
        "total": total,
        "remaining": remaining,
    }


def distribute_total(total, count):
    count = max(1, safe_int(count, 1))
    total = round(max(0.0, safe_float(total)), 2)

    result = [round(total / count, 2) for _ in range(count - 1)]
    result.append(round(total - sum(result), 2))
    return result


# =========================================================
# AI DESTINATION IMAGE
# =========================================================

def generate_destination_image(destination):
    """Generate and locally cache an AI image for the user's destination."""

    destination = clean_text(destination)
    if not destination:
        return ""

    if not POLLINATIONS_API_KEY:
        print("POLLINATIONS_API_KEY is missing. Destination image skipped.")
        return ""

    output_dir = os.path.join(app.static_folder, "generated")
    os.makedirs(output_dir, exist_ok=True)

    slug = slugify(destination)
    prompt = (
        f"Photorealistic professional tourism photograph of {destination}, India. "
        f"Show authentic local scenery and recognizable visual character associated "
        f"with {destination}. Natural daylight, realistic colors, high detail, "
        f"wide landscape composition, realistic travel photography, no close-up people, "
        f"no text, no letters, no logo, no watermark."
    )

    # Cache by destination to avoid generating the same image repeatedly.
    existing_jpg = os.path.join(output_dir, f"{slug}.jpg")
    existing_png = os.path.join(output_dir, f"{slug}.png")

    if os.path.exists(existing_jpg):
        return url_for("static", filename=f"generated/{slug}.jpg")

    if os.path.exists(existing_png):
        return url_for("static", filename=f"generated/{slug}.png")

    image_url = (
        "https://gen.pollinations.ai/image/"
        + quote(prompt, safe="")
        + "?model=flux&width=1400&height=800"
    )

    try:
        response = requests.get(
            image_url,
            headers={
                "Authorization": f"Bearer {POLLINATIONS_API_KEY}",
                "User-Agent": "AI-Travel-Planner/1.0",
            },
            timeout=120,
        )
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "").lower()

        if "png" in content_type:
            filename = f"{slug}.png"
        elif "webp" in content_type:
            filename = f"{slug}.webp"
        else:
            filename = f"{slug}.jpg"

        file_path = os.path.join(output_dir, filename)

        with open(file_path, "wb") as image_file:
            image_file.write(response.content)

        return url_for(
            "static",
            filename=f"generated/{filename}"
        )

    except requests.HTTPError as exc:
        status = getattr(exc.response, "status_code", "unknown")
        print(f"AI image generation warning: HTTP {status} - {exc}")
    except Exception as exc:
        print("AI image generation warning:", exc)

    return ""


# =========================================================
# GROQ AI PLAN
# =========================================================

def generate_ai_plan(trip_data):

    if not groq_client:
        raise RuntimeError(
            "GROQ_API_KEY is missing or Groq client could not be initialized."
        )

    destination = trip_data["destination"]
    transport_preference = clean_text(
        trip_data.get("transport_preference"),
        "Any suitable transport"
    )

    prompt = f"""
You are a professional travel planner creating a highly practical, chronological travel plan for an Indian traveler.

USER INPUT
Starting location: {trip_data['starting_location']}
Destination: {destination}
Travel date: {trip_data['travel_date']}
Days: {trip_data['days']}
Number of travelers: {trip_data['persons']}
Maximum budget: ₹{trip_data['budget']}
Travel style: {trip_data['travel_preference']}
Transport preference: {transport_preference}
Hotel preference: {trip_data['stay_preference']}
Food preference: {trip_data['food_preference']}
Activity preference: {trip_data['activity_preference']}

CORE ITINERARY RULES
- The destination must remain exactly "{destination}".
- Create exactly {trip_data['days']} itinerary day objects.
- Build every day as a realistic timeline from morning/wake-up until night/sleep.
- Activities must be in strict chronological order with no overlapping times.
- Include breakfast, lunch and dinner at practical times unless the traveler is in transit and a meal is not realistic.
- Include wake-up/morning start and night sleep/rest on every full day.
- Include hotel check-in on arrival day and hotel checkout on the final day when appropriate.
- Include realistic rest periods after long journeys.
- Never pack too many attractions into one day.
- Include practical local transfer time between different places.
- For each movement between places, provide previous_location, next_location, transport_mode, distance and travel_time.
- For sightseeing, shopping and meal stops, give a useful location/area.
- For meals, use meal_type, food_suggestion and recommended_area.
- For bookable activities, use booking_required and booking_note.
- Costs are estimates only. Python will calculate the final amount and remaining budget.
- Do not claim unknown ticket prices, hotel prices, platform numbers, railway seat availability or schedules are verified.

ACTIVITY TYPES
Use only these values for activity_type:
wake_up, breakfast, travel, train, flight, bus, sightseeing, lunch, shopping, activity, hotel_checkin, hotel_checkout, rest, dinner, hotel_return, sleep.

LONG-DISTANCE TRANSPORT RULES
- Respect the user's transport preference: {transport_preference}.
- If the selected transport is Train, include station transfer, train journey and arrival transfer where appropriate.
- If a long train journey crosses a meal time, include realistic onboard meal/rest activities rather than sightseeing.
- If the selected transport is Flight, include airport transfer, check-in/security buffer, flight and arrival transfer.
- If the selected transport is Bus, include terminal transfer, bus journey and arrival transfer.

TRAIN INFORMATION RULES
- If Train is the selected/preferred mode, return up to 5 useful train_options for the requested route/date only when you reasonably know them.
- train_options are suggestions, NOT live railway availability.
- Never invent confirmed seat availability, live running status, platform number or PNR information.
- Set availability_status to "Verify live availability" unless live data is explicitly available (it is not available in this prompt).
- If you are unsure about an exact train number/name/timing, leave the uncertain field blank rather than inventing it.
- The application will show a verification button for IRCTC/official checking.

DESTINATION RULES
- Destination information must be detailed and useful.
- The destination description must contain 3 to 4 complete sentences specific to the destination.
- The travel note must contain 2 to 3 complete sentences with practical visitor advice.
- Return exactly 5 destination highlights specific to the exact destination.
- Do not use generic state-level highlights when the user entered a specific city or town.
- Return JSON only, with no markdown.

JSON FORMAT
{{
  "trip": {{
    "summary": "short practical summary"
  }},
  "budget": {{
    "transport": 0,
    "hotel": 0,
    "food": 0,
    "activities": 0,
    "other": 0
  }},
  "transport_plan": {{
    "outbound": "suggested outbound transport",
    "local_transport": "suggested local transport",
    "return": "suggested return transport"
  }},
  "train_options": [
    {{
      "train_number": "",
      "train_name": "",
      "from_station": "",
      "from_code": "",
      "to_station": "",
      "to_code": "",
      "departure_time": "",
      "arrival_time": "",
      "duration": "",
      "travel_date": "{trip_data['travel_date']}",
      "classes": ["SL", "3A", "2A"],
      "availability_status": "Verify live availability",
      "note": "Suggested train. Verify current schedule and seats before booking."
    }}
  ],
  "itinerary": [
    {{
      "day": 1,
      "date": "{trip_data['travel_date']}",
      "title": "Day title",
      "activities": [
        {{
          "start_time": "06:30 AM",
          "end_time": "07:00 AM",
          "duration": "30 minutes",
          "activity_type": "wake_up",
          "place": "Wake Up & Get Ready",
          "location": "Hotel / Home",
          "previous_location": "",
          "next_location": "",
          "distance_km": null,
          "distance": "",
          "travel_time": "",
          "transport_mode": "",
          "description": "Practical description",
          "estimated_cost": 0,
          "booking_required": false,
          "booking_note": "",
          "meal_type": "",
          "food_suggestion": "",
          "recommended_area": "",
          "train_number": "",
          "train_name": "",
          "departure_station": "",
          "arrival_station": "",
          "departure_time": "",
          "arrival_time": ""
        }}
      ]
    }}
  ],
  "stays": [
    {{
      "name": "stay name",
      "location": "location",
      "rating": null,
      "price_per_night": 0,
      "type": "Hotel / Hostel / Homestay / Resort",
      "description": "short description"
    }}
  ],
  "destination_info": {{
    "description": "Write a useful 3 to 4 sentence description of the exact destination, covering its character, important attractions, local culture or food, and why it is worth visiting.",
    "best_time": "Give the practical best months or season to visit and briefly explain why.",
    "highlights": ["place 1", "place 2", "place 3", "place 4", "place 5"],
    "travel_note": "Write a practical 2 to 3 sentence travel note covering local transport, booking advice, useful precautions, or other helpful visitor information."
  }}
}}
"""

    messages = [
        {
            "role": "system",
            "content": (
                "You are a professional travel planner. "
                "Return exactly one valid JSON object. "
                "Do not write markdown, comments, or explanatory text. "
                "Ensure every array and object is correctly closed. "
                "The itinerary must be chronological from wake-up/morning through sleep/night. "
                "The itinerary array must contain one object for each requested day. "
                "Never claim live railway seat availability without a live data source. "
                "Destination information must be detailed and specific to the exact destination. "
                "Return five destination highlights."
            ),
        },
        {
            "role": "user",
            "content": prompt,
        },
    ]

    try:
        response = groq_client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            temperature=0.15,
            max_completion_tokens=9000,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        data = parse_json(content, None)
        if isinstance(data, dict):
            return data
    except Exception as strict_error:
        print("Groq JSON-mode warning:", strict_error)

    response = groq_client.chat.completions.create(
        model=MODEL_NAME,
        messages=messages,
        temperature=0.15,
        max_completion_tokens=9000,
    )

    content = response.choices[0].message.content
    data = parse_json(content, None)

    if not isinstance(data, dict):
        raise RuntimeError("Groq returned invalid JSON after retry.")

    return data


# =========================================================
# FALLBACK PLAN
# =========================================================

def make_fallback_plan(trip_data):

    budget = trip_data["budget"]

    budget_data = calculate_final_budget(
        budget,
        budget * 0.25,
        budget * 0.25,
        budget * 0.20,
        budget * 0.15,
        budget * 0.10,
    )

    start_date = parse_date(trip_data["travel_date"]) or date.today()
    days = max(1, trip_data["days"])
    daily_costs = distribute_total(budget_data["total"], days)

    itinerary = []

    for index in range(days):
        day_no = index + 1
        day_date = start_date + timedelta(days=index)

        if day_no == 1:
            title = f"Arrival in {trip_data['destination']}"
            activities = [
                {
                    "start_time": "09:00 AM",
                    "end_time": "12:00 PM",
                    "duration": "3 hours",
                    "place": f"Travel to {trip_data['destination']}",
                    "location": trip_data["destination"],
                    "previous_location": trip_data["starting_location"],
                    "next_location": trip_data["destination"],
                    "distance_km": None,
                    "distance": "Approximate",
                    "travel_time": "Approximate",
                    "transport_mode": trip_data["transport_preference"],
                    "description": "Travel to the destination and reach your stay.",
                    "estimated_cost": round(daily_costs[index] * 0.35, 2),
                },
                {
                    "start_time": "12:30 PM",
                    "end_time": "02:00 PM",
                    "duration": "1.5 hours",
                    "place": "Check-in & Lunch",
                    "location": trip_data["destination"],
                    "previous_location": trip_data["destination"],
                    "next_location": trip_data["destination"],
                    "distance_km": None,
                    "distance": "",
                    "travel_time": "",
                    "transport_mode": "",
                    "description": "Check in, have lunch and rest.",
                    "estimated_cost": round(daily_costs[index] * 0.30, 2),
                },
                {
                    "start_time": "04:00 PM",
                    "end_time": "07:00 PM",
                    "duration": "3 hours",
                    "place": f"Explore {trip_data['destination']}",
                    "location": trip_data["destination"],
                    "previous_location": trip_data["destination"],
                    "next_location": trip_data["destination"],
                    "distance_km": None,
                    "distance": "",
                    "travel_time": "",
                    "transport_mode": "",
                    "description": "Visit a popular local area and enjoy the surroundings.",
                    "estimated_cost": round(daily_costs[index] * 0.35, 2),
                },
            ]
        elif day_no == days:
            title = f"Final day and return to {trip_data['starting_location']}"
            activities = [
                {
                    "start_time": "09:00 AM",
                    "end_time": "11:00 AM",
                    "duration": "2 hours",
                    "place": f"Morning sightseeing in {trip_data['destination']}",
                    "location": trip_data["destination"],
                    "previous_location": trip_data["destination"],
                    "next_location": trip_data["destination"],
                    "distance_km": None,
                    "distance": "",
                    "travel_time": "",
                    "transport_mode": "",
                    "description": "Visit one more important place before checkout.",
                    "estimated_cost": round(daily_costs[index] * 0.25, 2),
                },
                {
                    "start_time": "12:00 PM",
                    "end_time": "01:30 PM",
                    "duration": "1.5 hours",
                    "place": "Lunch & Check-out",
                    "location": trip_data["destination"],
                    "previous_location": trip_data["destination"],
                    "next_location": trip_data["destination"],
                    "distance_km": None,
                    "distance": "",
                    "travel_time": "",
                    "transport_mode": "",
                    "description": "Have lunch and prepare for the return journey.",
                    "estimated_cost": round(daily_costs[index] * 0.20, 2),
                },
                {
                    "start_time": "02:00 PM",
                    "end_time": "06:00 PM",
                    "duration": "4 hours",
                    "place": f"Return to {trip_data['starting_location']}",
                    "location": trip_data["starting_location"],
                    "previous_location": trip_data["destination"],
                    "next_location": trip_data["starting_location"],
                    "distance_km": None,
                    "distance": "Approximate",
                    "travel_time": "Approximate",
                    "transport_mode": trip_data["transport_preference"],
                    "description": "Return to your starting location.",
                    "estimated_cost": round(daily_costs[index] * 0.55, 2),
                },
            ]
        else:
            title = f"Explore {trip_data['destination']}"
            activities = [
                {
                    "start_time": "09:00 AM",
                    "end_time": "11:00 AM",
                    "duration": "2 hours",
                    "place": f"Morning sightseeing in {trip_data['destination']}",
                    "location": trip_data["destination"],
                    "previous_location": trip_data["destination"],
                    "next_location": trip_data["destination"],
                    "distance_km": None,
                    "distance": "",
                    "travel_time": "",
                    "transport_mode": "",
                    "description": "Explore an important local attraction.",
                    "estimated_cost": round(daily_costs[index] * 0.30, 2),
                },
                {
                    "start_time": "01:00 PM",
                    "end_time": "02:00 PM",
                    "duration": "1 hour",
                    "place": "Local Lunch",
                    "location": trip_data["destination"],
                    "previous_location": trip_data["destination"],
                    "next_location": trip_data["destination"],
                    "distance_km": None,
                    "distance": "",
                    "travel_time": "",
                    "transport_mode": "",
                    "description": "Enjoy local food and take a short break.",
                    "estimated_cost": round(daily_costs[index] * 0.20, 2),
                },
                {
                    "start_time": "03:00 PM",
                    "end_time": "06:00 PM",
                    "duration": "3 hours",
                    "place": f"{trip_data['activity_preference']} Experience",
                    "location": trip_data["destination"],
                    "previous_location": trip_data["destination"],
                    "next_location": trip_data["destination"],
                    "distance_km": None,
                    "distance": "",
                    "travel_time": "",
                    "transport_mode": "",
                    "description": "Enjoy activities that match your preference.",
                    "estimated_cost": round(daily_costs[index] * 0.35, 2),
                },
                {
                    "start_time": "08:00 PM",
                    "end_time": "09:30 PM",
                    "duration": "1.5 hours",
                    "place": "Dinner",
                    "location": trip_data["destination"],
                    "previous_location": trip_data["destination"],
                    "next_location": trip_data["destination"],
                    "distance_km": None,
                    "distance": "",
                    "travel_time": "",
                    "transport_mode": "",
                    "description": "Have dinner and relax.",
                    "estimated_cost": round(daily_costs[index] * 0.15, 2),
                },
            ]

        last_cost = round(
            daily_costs[index] - sum(
                safe_float(item["estimated_cost"])
                for item in activities[:-1]
            ),
            2
        )
        activities[-1]["estimated_cost"] = max(0.0, last_cost)

        itinerary.append({
            "day": day_no,
            "date": day_date.strftime("%Y-%m-%d"),
            "title": title,
            "activities": activities,
        })

    return {
        "trip": {
            "starting_location": trip_data["starting_location"],
            "destination": trip_data["destination"],
            "travel_date": trip_data["travel_date"],
            "days": days,
            "persons": trip_data["persons"],
            "budget": budget,
            "summary": (
                f"A practical {days}-day trip from "
                f"{trip_data['starting_location']} to {trip_data['destination']}."
            ),
        },
        "budget": budget_data,
        "transport_plan": {
            "outbound": trip_data["transport_preference"],
            "local_transport": "Local auto/cab/public transport as suitable",
            "return": trip_data["transport_preference"],
        },
        "itinerary": itinerary,
        "stays": [
            {
                "name": f"Budget {trip_data['stay_preference']} in {trip_data['destination']}",
                "location": trip_data["destination"],
                "rating": None,
                "price_per_night": round(
                    budget_data["hotel"] / max(1, days),
                    2
                ),
                "type": trip_data["stay_preference"],
                "description": "Suggested accommodation based on your trip budget.",
            }
        ],
        "destination_info": {
            "description": (
                f"{trip_data['destination']} offers a mix of local attractions, food, culture "
                "and experiences that can be explored according to your budget. The destination "
                "can be planned around its main sightseeing areas and local character, while keeping "
                "daily travel practical, comfortable and suitable for the selected travel style."
            ),
            "best_time": "Check the local weather and seasonal conditions before travel.",
            "highlights": [
                f"Popular attractions in {trip_data['destination']}",
                f"Local culture and heritage of {trip_data['destination']}",
                f"Local food and specialties in {trip_data['destination']}",
                f"Main sightseeing areas in {trip_data['destination']}",
                f"Local markets and experiences in {trip_data['destination']}",
            ],
            "travel_note": (
                f"Plan local travel around the main areas of {trip_data['destination']} and confirm "
                "local timings before visiting attractions. Keep some cash for small purchases, "
                "local transport and other on-trip expenses."
            ),
        },
    }


# =========================================================
# NORMALIZE AI PLAN
# =========================================================

def normalize_plan(plan, trip_data):

    if not isinstance(plan, dict):
        plan = {}

    destination = trip_data["destination"]
    days = trip_data["days"]
    start_date = parse_date(trip_data["travel_date"]) or date.today()

    trip = plan.get("trip")
    if not isinstance(trip, dict):
        trip = {}

    trip["starting_location"] = trip_data["starting_location"]
    trip["destination"] = destination
    trip["travel_date"] = trip_data["travel_date"]
    trip["days"] = days
    trip["persons"] = trip_data["persons"]
    trip["budget"] = trip_data["budget"]
    trip["summary"] = clean_text(
        trip.get("summary"),
        f"A personalized {days}-day trip from {trip_data['starting_location']} to {destination}."
    )
    plan["trip"] = trip

    ai_budget = plan.get("budget")
    if not isinstance(ai_budget, dict):
        ai_budget = {}

    final_budget = calculate_final_budget(
        trip_data["budget"],
        ai_budget.get("transport"),
        ai_budget.get("hotel"),
        ai_budget.get("food"),
        ai_budget.get("activities"),
        ai_budget.get("other"),
    )
    plan["budget"] = final_budget

    transport_plan = plan.get("transport_plan")
    if not isinstance(transport_plan, dict):
        transport_plan = {}

    transport_plan["outbound"] = clean_text(
        transport_plan.get("outbound"),
        trip_data["transport_preference"]
    )
    transport_plan["local_transport"] = clean_text(
        transport_plan.get("local_transport"),
        "Local auto/cab/public transport as suitable"
    )
    transport_plan["return"] = clean_text(
        transport_plan.get("return"),
        trip_data["transport_preference"]
    )
    plan["transport_plan"] = transport_plan

    # Suggested train options. These are never treated as live seat availability.
    raw_train_options = plan.get("train_options")
    if not isinstance(raw_train_options, list):
        raw_train_options = []

    clean_train_options = []
    for train in raw_train_options[:5]:
        if not isinstance(train, dict):
            continue

        classes = train.get("classes")
        if not isinstance(classes, list):
            classes = []

        clean_train_options.append({
            "train_number": clean_text(train.get("train_number"), ""),
            "train_name": clean_text(train.get("train_name"), ""),
            "from_station": clean_text(train.get("from_station"), trip_data["starting_location"]),
            "from_code": clean_text(train.get("from_code"), ""),
            "to_station": clean_text(train.get("to_station"), destination),
            "to_code": clean_text(train.get("to_code"), ""),
            "departure_time": clean_text(train.get("departure_time"), ""),
            "arrival_time": clean_text(train.get("arrival_time"), ""),
            "duration": clean_text(train.get("duration"), ""),
            "travel_date": clean_text(train.get("travel_date"), trip_data["travel_date"]),
            "classes": [clean_text(value) for value in classes if clean_text(value)][:6],
            "availability_status": "Verify live availability",
            "note": clean_text(
                train.get("note"),
                "Suggested option only. Verify current schedule, fare and seat availability before booking."
            ),
        })

    plan["train_options"] = clean_train_options

    # Exact number of days.
    raw_itinerary = plan.get("itinerary")
    if not isinstance(raw_itinerary, list):
        raw_itinerary = []

    if not raw_itinerary:
        plan = normalize_plan(make_fallback_plan(trip_data), trip_data)
        plan["destination_image"] = generate_destination_image(destination)
        return plan

    normalized_days = []
    daily_totals = distribute_total(
        final_budget["total"],
        days
    )

    for index in range(days):
        raw_day = raw_itinerary[index] if index < len(raw_itinerary) else {}
        if not isinstance(raw_day, dict):
            raw_day = {}

        activities = raw_day.get("activities")
        if not isinstance(activities, list):
            activities = []

        clean_activities = []

        for activity in activities:
            if not isinstance(activity, dict):
                continue

            item = {
                "start_time": clean_text(activity.get("start_time"), "09:00 AM"),
                "end_time": clean_text(activity.get("end_time"), ""),
                "duration": clean_text(activity.get("duration"), ""),
                "activity_type": clean_text(activity.get("activity_type"), "activity").lower(),
                "place": clean_text(activity.get("place"), f"Explore {destination}"),
                "location": clean_text(activity.get("location"), destination),
                "previous_location": clean_text(activity.get("previous_location"), ""),
                "next_location": clean_text(activity.get("next_location"), ""),
                "distance_km": (
                    safe_float(activity.get("distance_km"), 0)
                    if activity.get("distance_km") not in (None, "", "null")
                    else None
                ),
                "distance": clean_text(activity.get("distance"), ""),
                "travel_time": clean_text(activity.get("travel_time"), ""),
                "transport_mode": clean_text(activity.get("transport_mode"), ""),
                "description": clean_text(
                    activity.get("description"),
                    "Enjoy this part of your trip."
                ),
                "booking_required": bool(activity.get("booking_required", False)),
                "booking_note": clean_text(activity.get("booking_note"), ""),
                "meal_type": clean_text(activity.get("meal_type"), ""),
                "food_suggestion": clean_text(activity.get("food_suggestion"), ""),
                "recommended_area": clean_text(activity.get("recommended_area"), ""),
                "train_number": clean_text(activity.get("train_number"), ""),
                "train_name": clean_text(activity.get("train_name"), ""),
                "departure_station": clean_text(activity.get("departure_station"), ""),
                "arrival_station": clean_text(activity.get("arrival_station"), ""),
                "departure_time": clean_text(activity.get("departure_time"), ""),
                "arrival_time": clean_text(activity.get("arrival_time"), ""),
                "estimated_cost": max(
                    0.0,
                    safe_float(activity.get("estimated_cost"), 0)
                ),
            }

            valid_activity_types = {
                "wake_up", "breakfast", "travel", "train", "flight", "bus",
                "sightseeing", "lunch", "shopping", "activity", "hotel_checkin",
                "hotel_checkout", "rest", "dinner", "hotel_return", "sleep"
            }
            if item["activity_type"] not in valid_activity_types:
                item["activity_type"] = "activity"

            # Meals, sleep and rest do not need route metadata unless explicitly a travel activity.
            if item["activity_type"] in {"breakfast", "lunch", "dinner", "wake_up", "rest", "sleep"}:
                item["previous_location"] = ""
                item["next_location"] = ""
                item["transport_mode"] = ""
                item["distance"] = ""
                item["distance_km"] = None
                item["travel_time"] = ""

            clean_activities.append(item)

        if not clean_activities:
            fallback = make_fallback_plan(trip_data)
            clean_activities = fallback["itinerary"][index]["activities"]

        activity_costs = distribute_total(
            daily_totals[index],
            len(clean_activities)
        )

        for activity_index, activity in enumerate(clean_activities):
            activity["estimated_cost"] = activity_costs[activity_index]

        normalized_days.append({
            "day": index + 1,
            "date": (
                start_date + timedelta(days=index)
            ).strftime("%Y-%m-%d"),
            "title": clean_text(
                raw_day.get("title"),
                f"Explore {destination}"
            ),
            "activities": clean_activities,
        })

    plan["itinerary"] = normalized_days

    stays = plan.get("stays")
    if not isinstance(stays, list):
        stays = []

    clean_stays = []
    for stay in stays[:6]:
        if not isinstance(stay, dict):
            continue
        clean_stays.append({
            "name": clean_text(stay.get("name"), "Recommended Stay"),
            "location": clean_text(stay.get("location"), destination),
            "rating": (
                safe_float(stay.get("rating"), 0)
                if stay.get("rating") not in (None, "", "null")
                else None
            ),
            "price_per_night": max(
                0.0,
                safe_float(stay.get("price_per_night"), 0)
            ),
            "type": clean_text(stay.get("type"), trip_data["stay_preference"]),
            "description": clean_text(
                stay.get("description"),
                "Suitable for this trip."
            ),
        })

    if not clean_stays:
        clean_stays = [
            {
                "name": f"Budget {trip_data['stay_preference']} in {destination}",
                "location": destination,
                "rating": None,
                "price_per_night": round(
                    final_budget["hotel"] / max(1, days),
                    2
                ),
                "type": trip_data["stay_preference"],
                "description": "Suggested accommodation based on your budget.",
            }
        ]

    plan["stays"] = clean_stays

    info = plan.get("destination_info")
    if not isinstance(info, dict):
        info = {}

    highlights = info.get("highlights")
    if not isinstance(highlights, list):
        highlights = []

    clean_highlights = []
    for item in highlights:
        item = clean_text(item)
        if item and item not in clean_highlights:
            clean_highlights.append(item)

    info["description"] = clean_text(
        info.get("description"),
        f"{destination} has local attractions, food and experiences worth exploring."
    )
    info["best_time"] = clean_text(
        info.get("best_time"),
        "Check local weather before travel."
    )
    info["highlights"] = clean_highlights[:5] or [
        f"Popular attractions in {destination}",
        f"Local culture and heritage of {destination}",
        f"Local food and specialties in {destination}",
        f"Main sightseeing areas in {destination}",
        f"Local markets and experiences in {destination}",
    ]
    info["travel_note"] = clean_text(
        info.get("travel_note"),
        "Confirm local timings and travel conditions before visiting."
    )

    plan["destination_info"] = info

    # AI-generated image for the exact user destination.
    plan["destination_image"] = generate_destination_image(destination)

    # Compatibility aliases for older templates.
    trip["image"] = plan["destination_image"]
    trip["destination_image"] = plan["destination_image"]
    trip["total"] = final_budget["total"]
    trip["remaining"] = final_budget["remaining"]

    return plan


# =========================================================
# HOME
# =========================================================

@app.route("/")
def home():
    return render_template("index.html")


# =========================================================
# REGISTER
# =========================================================

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = clean_text(request.form.get("name"))
        email = clean_text(request.form.get("email")).lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not all([name, email, password, confirm_password]):
            flash("Please fill in all fields.", "error")
            return render_template("register.html")

        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return render_template("register.html")

        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return render_template("register.html")

        if User.query.filter_by(email=email).first():
            flash("An account with this email already exists.", "error")
            return render_template("register.html")

        user = User(
            name=name,
            email=email,
            password_hash=generate_password_hash(password)
        )

        db.session.add(user)
        db.session.commit()

        session.clear()
        session["user_id"] = user.id

        flash("Account created successfully.", "success")
        return redirect(url_for("planner"))

    return render_template("register.html")


# =========================================================
# LOGIN
# =========================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = clean_text(request.form.get("email")).lower()
        password = request.form.get("password", "")

        try:
            user = User.query.filter_by(email=email).first()
        except Exception as exc:
            db.session.rollback()
            print("LOGIN DATABASE ERROR:", exc)
            flash(
                "Database structure is outdated. Restart the app once so the database migration can run.",
                "error"
            )
            return render_template("login.html")

        if user and check_password_hash(user.password_hash, password):
            session.clear()
            session["user_id"] = user.id
            flash(f"Welcome back, {user.name}!", "success")
            return redirect(url_for("planner"))

        flash("Invalid email or password.", "error")

    return render_template("login.html")


# =========================================================
# LOGOUT
# =========================================================

@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("home"))


# =========================================================
# PLANNER GET + POST
# =========================================================

@app.route("/planner", methods=["GET", "POST"])
def planner():
    if not require_login():
        flash("Please login or register before planning a trip.", "error")
        return redirect(url_for("login"))

    if request.method == "POST":
        starting_location = clean_text(request.form.get("starting_location"))
        destination = clean_text(request.form.get("destination"))
        travel_date = clean_text(request.form.get("travel_date"))
        days = safe_int(request.form.get("days"), 0)
        persons = safe_int(
            request.form.get("travelers") or request.form.get("persons"),
            0
        )
        budget = safe_float(request.form.get("budget"), 0)

        travel_preference = clean_text(
            request.form.get("travel_preference"),
            "Budget"
        )

        # Current planner.html does not yet have a transport field, so Any is safe.
        transport_preference = clean_text(
            request.form.get("transport_preference"),
            "Any"
        )

        stay_preference = clean_text(
            request.form.get("hotel_preference") or request.form.get("stay_preference"),
            "Hotel"
        )

        food_preference = clean_text(
            request.form.get("food_preference"),
            "Any"
        )

        activity_preference = clean_text(
            request.form.get("activity_preference"),
            "Sightseeing"
        )

        if not starting_location:
            flash("Starting location is required.", "error")
            return render_template("planner.html")

        if not destination:
            flash("Destination is required.", "error")
            return render_template("planner.html")

        if not parse_date(travel_date):
            flash("Please select a valid travel date.", "error")
            return render_template("planner.html")

        if days < 1:
            flash("Number of days must be at least 1.", "error")
            return render_template("planner.html")

        if persons < 1:
            flash("Number of travelers must be at least 1.", "error")
            return render_template("planner.html")

        if budget <= 0:
            flash("Please enter a valid budget.", "error")
            return render_template("planner.html")

        session["planner_input"] = {
            "starting_location": starting_location,
            "destination": destination,
            "travel_date": travel_date,
            "days": days,
            "persons": persons,
            "travelers": persons,
            "budget": budget,
            "travel_preference": travel_preference,
            "transport_preference": transport_preference,
            "stay_preference": stay_preference,
            "food_preference": food_preference,
            "activity_preference": activity_preference,
        }

        return redirect(url_for("processing"))

    return render_template("planner.html")


# Compatibility route if an older planner form posts here.
@app.route("/generate-trip", methods=["POST"])
def generate_trip():
    return planner()


# =========================================================
# PROCESSING / GENERATION
# =========================================================

@app.route("/processing")
def processing():
    if not require_login():
        return redirect(url_for("login"))

    trip_data = session.get("planner_input")

    if not trip_data:
        flash("Please enter your trip details first.", "error")
        return redirect(url_for("planner"))

    try:
        ai_plan = generate_ai_plan(trip_data)
        final_plan = normalize_plan(ai_plan, trip_data)

    except Exception as exc:
        print("Trip generation error:", exc)

        # Do not hide an invalid Groq credential with fake AI output.
        error_text = str(exc).lower()

        if "401" in error_text or "invalid api key" in error_text or "invalid_api_key" in error_text:
            flash(
                "Groq API key is invalid. Update GROQ_API_KEY in .env and restart the app.",
                "error"
            )
            return redirect(url_for("planner"))

        # Other AI failures get a practical non-AI fallback so the website still works.
        try:
            final_plan = normalize_plan(
                make_fallback_plan(trip_data),
                trip_data
            )
            flash(
                "AI was temporarily unavailable, so a practical fallback plan was created.",
                "error"
            )
        except Exception as fallback_error:
            print("Fallback generation error:", fallback_error)
            flash("Unable to generate the trip plan right now.", "error")
            return redirect(url_for("planner"))

    session["trip_plan"] = final_plan
    session.modified = True

    return redirect(url_for("result"))


# =========================================================
# RESULT
# =========================================================

@app.route("/result")
def result():
    if not require_login():
        return redirect(url_for("login"))

    plan = session.get("trip_plan")

    if not plan:
        flash("Please generate a trip plan first.", "error")
        return redirect(url_for("planner"))

    trip = plan.get("trip", {})
    budget = plan.get("budget", {})

    # Important: expose BOTH new and old variable names.
    return render_template(
        "result.html",
        plan=plan,
        trip=trip,
        budget=budget,
        costs=budget,
        ai_plan=plan,
    )


# =========================================================
# ITINERARY
# =========================================================

@app.route("/itinerary")
def itinerary():
    if not require_login():
        return redirect(url_for("login"))

    stored_plan = session.get("trip_plan")

    if not stored_plan:
        flash("Please generate a trip plan first.", "error")
        return redirect(url_for("planner"))

    # Work on a display copy so external verification never corrupts the
    # originally generated/saved AI plan.
    plan = copy.deepcopy(stored_plan)

    # Resolve meaningful attractions/temples/hotels to exact coordinates.
    # Public geocoder calls are locally cached and rate-limited.
    try:
        plan = enrich_itinerary_locations(plan, max_lookups=8)
    except Exception as exc:
        print("Location verification warning:", exc)

    trip_info = plan.get("trip", {})
    transport_plan = plan.get("transport_plan") or {}
    outbound_mode = str(transport_plan.get("outbound") or "").lower()

    live_trains = {
        "enabled": False,
        "error": None,
        "from_station": None,
        "to_station": None,
        "trains": [],
    }

    # Only call railway APIs when the generated/user transport choice is train.
    if "train" in outbound_mode:
        try:
            live_trains = get_live_trains(
                trip_info.get("starting_location", ""),
                trip_info.get("destination", ""),
                trip_info.get("travel_date", ""),
                max_results=8,
            )
        except Exception as exc:
            print("Railway verification warning:", exc)
            live_trains = {
                "enabled": True,
                "error": "Live railway data could not be loaded right now.",
                "from_station": None,
                "to_station": None,
                "trains": [],
            }

    return render_template(
        "itinerary.html",
        plan=plan,
        trip=trip_info,
        budget=plan.get("budget", {}),
        ai_plan=plan,
        live_trains=live_trains,
    )


# =========================================================
# BUDGET
# =========================================================

@app.route("/budget")
def budget():
    if not require_login():
        return redirect(url_for("login"))

    plan = session.get("trip_plan")

    if not plan:
        flash("Please generate a trip plan first.", "error")
        return redirect(url_for("planner"))

    return render_template(
        "budget.html",
        plan=plan,
        trip=plan.get("trip", {}),
        budget=plan.get("budget", {}),
        ai_plan=plan,
    )


# =========================================================
# STAYS
# =========================================================

@app.route("/stays")
def stays():
    if not require_login():
        return redirect(url_for("login"))

    plan = session.get("trip_plan")

    if not plan:
        flash("Please generate a trip plan first.", "error")
        return redirect(url_for("planner"))

    return render_template(
        "stays.html",
        plan=plan,
        trip=plan.get("trip", {}),
        budget=plan.get("budget", {}),
        ai_plan=plan,
    )


# =========================================================
# SAVE TRIP
# =========================================================

@app.route("/save-trip", methods=["POST"])
def save_trip():
    user = current_user()

    if not user:
        flash("Please login first.", "error")
        return redirect(url_for("login"))

    plan = session.get("trip_plan")

    if not plan:
        flash("No generated trip is available to save.", "error")
        return redirect(url_for("planner"))

    trip_info = plan.get("trip", {})
    planner_input = session.get("planner_input", {})

    saved_trip = Trip(
        user_id=user.id,
        starting_location=clean_text(
            trip_info.get("starting_location"),
            planner_input.get("starting_location", "")
        ),
        destination=clean_text(
            trip_info.get("destination"),
            planner_input.get("destination", "")
        ),
        travel_date=clean_text(
            trip_info.get("travel_date"),
            planner_input.get("travel_date", "")
        ),
        days=safe_int(
            trip_info.get("days"),
            planner_input.get("days", 1)
        ),
        persons=safe_int(
            trip_info.get("persons"),
            planner_input.get("persons", 1)
        ),
        budget=safe_float(
            trip_info.get("budget"),
            planner_input.get("budget", 0)
        ),
        travel_preference=clean_text(
            planner_input.get("travel_preference"),
            "Budget"
        ),
        transport_preference=clean_text(
            planner_input.get("transport_preference"),
            "Any"
        ),
        stay_preference=clean_text(
            planner_input.get("stay_preference"),
            "Hotel"
        ),
        food_preference=clean_text(
            planner_input.get("food_preference"),
            "Any"
        ),
        activity_preference=clean_text(
            planner_input.get("activity_preference"),
            "Sightseeing"
        ),
        ai_result=json.dumps(
            plan,
            ensure_ascii=False
        ),
        is_saved=True
    )

    try:
        db.session.add(saved_trip)
        db.session.commit()
        session["saved_trip_id"] = saved_trip.id

        flash("Trip saved successfully.", "success")
        return redirect(url_for("my_trips"))

    except Exception as exc:
        db.session.rollback()
        print("SAVE TRIP ERROR:", exc)
        flash("Trip could not be saved.", "error")
        return redirect(url_for("result"))


# =========================================================
# MY TRIPS
# =========================================================

@app.route("/my-trips")
def my_trips():
    user = current_user()

    if not user:
        flash("Please login to see your saved trips.", "error")
        return redirect(url_for("login"))

    trips = Trip.query.filter_by(
        user_id=user.id,
        is_saved=True
    ).order_by(
        Trip.created_at.desc()
    ).all()

    return render_template(
        "my_trips.html",
        trips=trips
    )


# =========================================================
# VIEW SAVED TRIP
# =========================================================

@app.route("/view-trip/<int:trip_id>")
def view_trip(trip_id):
    user = current_user()

    if not user:
        flash("Please login first.", "error")
        return redirect(url_for("login"))

    saved_trip = Trip.query.filter_by(
        id=trip_id,
        user_id=user.id,
        is_saved=True
    ).first()

    if not saved_trip:
        flash("Trip not found.", "error")
        return redirect(url_for("my_trips"))

    plan = parse_json(saved_trip.ai_result, {})

    if not isinstance(plan, dict) or not plan:
        flash("Saved trip data is not available.", "error")
        return redirect(url_for("my_trips"))

    return render_template(
        "result.html",
        plan=plan,
        trip=plan.get("trip", {}),
        budget=plan.get("budget", {}),
        ai_plan=plan,
        saved_trip=saved_trip,
    )


# =========================================================
# DELETE SAVED TRIP
# =========================================================

@app.route("/delete-trip/<int:trip_id>", methods=["POST"])
def delete_trip(trip_id):
    user = current_user()

    if not user:
        flash("Please login first.", "error")
        return redirect(url_for("login"))

    saved_trip = Trip.query.filter_by(
        id=trip_id,
        user_id=user.id
    ).first()

    if not saved_trip:
        flash("Trip not found.", "error")
        return redirect(url_for("my_trips"))

    try:
        db.session.delete(saved_trip)
        db.session.commit()

        if session.get("saved_trip_id") == trip_id:
            session.pop("saved_trip_id", None)

        flash("Trip deleted successfully.", "success")

    except Exception as exc:
        db.session.rollback()
        print("DELETE TRIP ERROR:", exc)
        flash("Trip could not be deleted.", "error")

    return redirect(url_for("my_trips"))


# =========================================================
# PROFILE
# =========================================================

@app.route("/profile")
def profile():
    user = current_user()

    if not user:
        flash("Please login to view your profile.", "error")
        return redirect(url_for("login"))

    saved_count = Trip.query.filter_by(
        user_id=user.id,
        is_saved=True
    ).count()

    return render_template(
        "profile.html",
        saved_count=saved_count
    )


# =========================================================
# DESTINATIONS
# =========================================================

@app.route("/destinations")
def destinations():
    return render_template("destinations.html")


# =========================================================
# ABOUT
# =========================================================

@app.route("/about")
def about():
    return render_template("about.html")


# =========================================================
# ERROR HANDLERS
# =========================================================

@app.errorhandler(404)
def not_found(error):
    try:
        return render_template("404.html"), 404
    except Exception:
        return "404 - Page not found", 404


@app.errorhandler(500)
def internal_error(error):
    db.session.rollback()
    try:
        return render_template("500.html"), 500
    except Exception:
        return "500 - Internal server error", 500


# =========================================================
# STARTUP
# =========================================================

setup_database()


if __name__ == "__main__":
    print("\n==========================================")
    print(" AI TRAVEL PLANNER")
    print("==========================================")
    print(f"Model: {MODEL_NAME}")
    print("Groq:", "Configured" if GROQ_API_KEY else "Missing")
    print("Pollinations:", "Configured" if POLLINATIONS_API_KEY else "Missing")
    print("==========================================\n")

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True
    )
