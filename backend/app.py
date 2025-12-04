from flask import Flask, request, jsonify, Response, stream_with_context
import base64
import cv2
import numpy as np
import firebase_admin
from firebase_admin import credentials, firestore, storage, auth as firebase_auth
import datetime
from concurrent.futures import TimeoutError as FuturesTimeoutError
import ipaddress
import os
from urllib.parse import urlparse
from deepface import DeepFace
import concurrent.futures
from zoneinfo import ZoneInfo
import csv
import io
import threading
import time


from ipaddress import ip_address, ip_network

try:
    from .allowed_networks import UNT_EAGLENET_NETWORKS
except ImportError:  # pragma: no cover - fallback for script execution
    from allowed_networks import UNT_EAGLENET_NETWORKS


# ------------------------------
# Network allowlist configuration
# ------------------------------
# Home LAN networks default to the full 192.168.0.0/16 range. Override
# HOME_CIDR_STRINGS or HOME_CIDRS with a comma-separated list (e.g.,
# "192.168.1.0/24,2600:abcd::/64") when running demos off-campus. Production
# should rely on the UNT EagleNet ranges from allowed_networks.py.
DEFAULT_HOME_CIDR_STRINGS = (
    "192.168.0.0/16",
    "108.192.43.112/32",
    "127.0.0.1/32",
    "2600:1702:5230:8490::/64",
)

PRODUCTION_ORIGIN = "https://csce-4095---it-capstone-i.web.app"
DEFAULT_DEMO_ORIGINS = (
    PRODUCTION_ORIGIN,
    "https://fr-as-demo.ngrok-free.app",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://192.168.1.70:5173",
)


def _get_home_cidr_strings():
    env_value = os.environ.get("HOME_CIDR_STRINGS") or os.environ.get("HOME_CIDRS")
    if env_value:
        cidr_strings = [cidr.strip() for cidr in env_value.split(",") if cidr.strip()]
        if cidr_strings:
            return tuple(cidr_strings)

    return DEFAULT_HOME_CIDR_STRINGS


def _get_eaglenet_networks():
    env_value = os.environ.get("EAGLENET_IP_ALLOWLIST")
    if env_value:
        networks = []
        for cidr in env_value.split(","):
            cidr = cidr.strip()
            if not cidr:
                continue
            try:
                networks.append(ip_network(cidr))
            except ValueError:
                continue

        if networks:
            return tuple(networks)

    return UNT_EAGLENET_NETWORKS


def refresh_allowed_networks():
    global HOME_NETWORKS, ALLOWED_IP_NETWORKS, EAGLENET_NETWORKS

    EAGLENET_NETWORKS = _get_eaglenet_networks()
    HOME_NETWORKS = tuple(ip_network(cidr) for cidr in _get_home_cidr_strings())
    ALLOWED_IP_NETWORKS = EAGLENET_NETWORKS + HOME_NETWORKS


refresh_allowed_networks()


def _parse_allowed_cors_origins():
    extra_origins = os.environ.get("CORS_ALLOWED_ORIGINS", "")
    parsed = tuple(
        origin.strip()
        for origin in extra_origins.split(",")
        if origin.strip()
    )
    return DEFAULT_DEMO_ORIGINS + parsed


ALLOWED_CORS_ORIGINS = _parse_allowed_cors_origins()


def _add_vary_header(response, value):
    headers = getattr(response, "headers", None)
    if headers is None:
        return

    try:
        headers.add("Vary", value)
    except AttributeError:
        existing = headers.get("Vary", "")
        values = [item.strip() for item in existing.split(",") if item.strip()]
        if value not in values:
            values.append(value)
        if values:
            headers["Vary"] = ", ".join(values)


app = Flask(__name__)


def _perform_face_verification(
    captured_path, known_path, model_name="VGG-Face", timeout_seconds=15
):
    """
    Run DeepFace.verify with a timeout and return a structured response.

    Returns a dictionary containing verified, distance, and max_threshold_to_verify
    fields from the DeepFace response.
    """

    def _verify():
        return DeepFace.verify(
            img1_path=captured_path,
            img2_path=known_path,
            model_name=model_name,
            enforce_detection=False,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_verify)
        try:
            result = future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError as exc:  # pragma: no cover - timeout path
            future.cancel()
            raise TimeoutError("Face verification timed out") from exc

    if not isinstance(result, dict):
        raise ValueError("Face verification returned an unexpected result")

    return {
        "verified": bool(result.get("verified", False)),
        "distance": result.get("distance"),
        "max_threshold_to_verify": result.get("max_threshold_to_verify"),
    }


@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin")
    _add_vary_header(response, "Origin")

    if origin and _is_origin_allowed(origin):
        allowed_origin = origin
    else:
        allowed_origin = PRODUCTION_ORIGIN

    response.headers["Access-Control-Allow-Origin"] = allowed_origin
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Credentials"] = "true"
    return response


def _is_origin_allowed(origin):
    parsed = urlparse(origin)

    if parsed.scheme not in {"http", "https"}:
        return False

    hostname = parsed.hostname
    if not hostname:
        return False

    origin_no_trailing_slash = parsed._replace(path="", params="", query="", fragment="").geturl()
    if origin_no_trailing_slash in ALLOWED_CORS_ORIGINS:
        return True

    hostname = hostname.lower()

    if hostname.endswith(".ngrok-free.app"):
        return True

    try:
        ip = ip_address(hostname)
    except ValueError:
        ip = None

    if ip:
        return any(ip in network for network in HOME_NETWORKS)

    return False


# Initialize Firebase Admin SDK
cred = credentials.Certificate("firebase/firebase_credentials.json")
firebase_admin.initialize_app(cred, {
    "storageBucket": "csce-4095---it-capstone-i.firebasestorage.app",
})
db = firestore.client()
bucket = storage.bucket()

# Timezone for Central Time
CENTRAL_TZ = ZoneInfo("America/Chicago")

# How long pending records should wait before recheck (for logging & UI)
# You can change this for testing, or set env var PENDING_VERIFICATION_MINUTES.
PENDING_RECHECK_MINUTES = int(os.environ.get("PENDING_VERIFICATION_MINUTES", "45"))


def _to_central_iso(timestamp_like):
    """Return an ISO 8601 string in Central time for datetime inputs."""

    if isinstance(timestamp_like, datetime.datetime):
        timestamp = timestamp_like
    elif isinstance(timestamp_like, datetime.date):
        timestamp = datetime.datetime.combine(timestamp_like, datetime.time.min)
    else:
        return None

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=datetime.timezone.utc)
    return timestamp.astimezone(CENTRAL_TZ).isoformat()


def _get_class_document(class_id):
    """Fetch a class document by ID."""
    class_ref = db.collection("classes").document(class_id)
    class_doc = class_ref.get()
    if not class_doc.exists:
        return None
    data = class_doc.to_dict()
    data["id"] = class_doc.id
    return data


def _auto_create_absences_for_ended_classes():
    """
    For each class that meets today and has already ended,
    create an Absent attendance record for any enrolled student
    who does not yet have an attendance record for today.
    """
    now_central = datetime.datetime.now(CENTRAL_TZ)
    today = now_central.date()

    classes_ref = db.collection("classes")
    attendance_ref = _get_attendance_collection()

    try:
        for class_snap in classes_ref.stream():
            class_data = class_snap.to_dict() or {}
            class_id = class_data.get("id") or class_snap.id
            schedule_str = class_data.get("schedule")
            students = class_data.get("students") or []

            if not schedule_str or not students:
                continue

            parsed = _parse_schedule_days_and_times(schedule_str)
            if not parsed:
                continue

            weekdays = parsed["weekdays"]
            start_time = parsed["start_time"]
            end_time = parsed["end_time"]

            # Only consider classes that actually meet today
            if now_central.weekday() not in weekdays:
                continue

            # Build today's class start/end in Central time
            start_dt = datetime.datetime(
                today.year,
                today.month,
                today.day,
                start_time.hour,
                start_time.minute,
                0,
                0,
                tzinfo=CENTRAL_TZ,
            )
            end_dt = datetime.datetime(
                today.year,
                today.month,
                today.day,
                end_time.hour,
                end_time.minute,
                0,
                0,
                tzinfo=CENTRAL_TZ,
            )

            # Only proceed if class has ended
            if now_central <= end_dt:
                continue

            # We only care about records for "today" in Central time
            start_of_day = datetime.datetime(
                today.year,
                today.month,
                today.day,
                0,
                0,
                0,
                tzinfo=CENTRAL_TZ,
            )
            end_of_day = start_of_day + datetime.timedelta(days=1)

            # ---------------------------------------------------------
            # 1) Fetch all attendance records for this class
            #    (we'll filter by date and student in Python to avoid
            #    composite index requirements).
            # ---------------------------------------------------------
            existing_snapshots = list(
                attendance_ref.where("classID", "==", class_id).stream()
            )

            students_with_record_today = set()

            for snap in existing_snapshots:
                data = snap.to_dict() or {}
                student_id = data.get("studentID")
                date_value = data.get("date")

                if not student_id or not date_value:
                    continue

                # Firestore timestamps come back as datetime objects
                if not isinstance(date_value, datetime.datetime):
                    continue

                # Normalize to Central time just in case
                date_central = date_value.astimezone(CENTRAL_TZ)

                if start_of_day <= date_central < end_of_day:
                    students_with_record_today.add(student_id)

            # ---------------------------------------------------------
            # 2) For each enrolled student, if they DON'T have a record
            #    today, create an auto-absence record.
            # ---------------------------------------------------------
            for student_id in students:
                if student_id in students_with_record_today:
                    # They already have Present/Absent/Pending for today
                    continue

                # Fetch student profile for name fields (optional but nice)
                student_doc = db.collection("users").document(student_id).get()
                student_data = (
                    student_doc.to_dict()
                    if getattr(student_doc, "exists", False)
                    else {}
                )
                fname = (student_data.get("fname") or "").strip()
                lname = (student_data.get("lname") or "").strip()
                student_name = (fname + " " + lname).strip() or student_id

                # For 'date', we align with the class meeting's start time
                date_for_record = start_dt

                # Deterministic doc ID: CSCE1040_S1000_2025-11-16
                doc_id = f"{class_id}_{student_id}_{date_for_record.date().isoformat()}"

                new_ref = attendance_ref.document(doc_id)
                new_ref.set(
                    {
                        "classID": class_id,
                        "studentID": student_id,
                        "studentName": student_name,
                        "studentFullName": student_name,
                        "status": "Absent",
                        "date": date_for_record,
                        "createdBy": "auto-absence",
                        "decisionMethod": "auto-absence",
                        "editReason": "",
                        "createdAt": firestore.SERVER_TIMESTAMP,
                        "updatedAt": firestore.SERVER_TIMESTAMP,
                    }
                )

                # After creating an auto-absence, evaluate the absence threshold
                try:
                    _maybe_notify_absence_threshold(
                        class_id,
                        {
                            "studentID": student_id,
                            "classID": class_id,
                            "status": "Absent",
                        },
                    )
                except Exception as exc:
                    app.logger.exception(
                        "Error checking absence threshold after auto-absence: %s", exc
                    )

                app.logger.info(
                    "Auto-marked absent: class=%s student=%s date=%s",
                    class_id,
                    student_id,
                    date_for_record.isoformat(),
                )
    except Exception as exc:
        app.logger.exception(
            "Error auto-creating absences for ended classes: %s", exc
        )


def _get_teacher_profile(teacher_id=None, teacher_email=None):
    """Fetch minimal teacher profile for display."""
    users_collection = db.collection("users")

    if teacher_id:
        try:
            doc_ref = users_collection.document(teacher_id)
            snapshot = doc_ref.get()
        except Exception:
            snapshot = None
        else:
            if snapshot and getattr(snapshot, "exists", False):
                profile = snapshot.to_dict() or {}
                doc_id = getattr(snapshot, "id", None) or getattr(doc_ref, "id", None) or teacher_id
                return doc_id, profile

    if teacher_email:
        try:
            query = users_collection.where("email", "==", teacher_email).limit(1)
            snapshot = next(query.stream(), None)
        except Exception:
            snapshot = None
        else:
            if snapshot and getattr(snapshot, "exists", False):
                profile = snapshot.to_dict() or {}
                doc_id = getattr(snapshot, "id", None) or getattr(snapshot, "id", None)
                return doc_id, profile

    return None, None


def _get_attendance_collection():
    return db.collection("attendance")

# ------------------------------
# Notification + schedule helpers
# ------------------------------

# Weekday mapping for schedule parsing
WEEKDAY_CODES = {
    "M": 0,   # Monday
    "T": 1,   # Tuesday
    "W": 2,   # Wednesday
    "TH": 3,  # Thursday
    "F": 4,   # Friday
}


def _parse_schedule_string(schedule_str):
    """
    Parse a schedule string like "TTH 2:00PM - 5:20PM" or "MW 10:20AM - 2:30PM"
    into a list of dicts: [{"weekday": 1, "start_time": time, "end_time": time}, ...]
    """
    if not schedule_str or not isinstance(schedule_str, str):
        return []

    try:
        parts = schedule_str.split(" ", 1)
        if len(parts) != 2:
            return []
        days_part, time_part = parts[0].strip().upper(), parts[1].strip()

        # Parse days, being careful about "TH"
        tokens = []
        i = 0
        while i < len(days_part):
            if days_part[i : i + 2] == "TH":
                tokens.append("TH")
                i += 2
            else:
                tokens.append(days_part[i])
                i += 1

        day_numbers = []
        for token in tokens:
            if token in WEEKDAY_CODES:
                day_numbers.append(WEEKDAY_CODES[token])

        if not day_numbers:
            return []

        # Parse time range
        time_range_parts = time_part.split("-")
        if len(time_range_parts) != 2:
            return []

        start_str = time_range_parts[0].strip().replace(" ", "")
        end_str = time_range_parts[1].strip().replace(" ", "")

        # Times like "2:00PM"
        start_dt = datetime.datetime.strptime(start_str, "%I:%M%p")
        end_dt = datetime.datetime.strptime(end_str, "%I:%M%p")
        start_time = start_dt.time()
        end_time = end_dt.time()

        return [
            {"weekday": day_num, "start_time": start_time, "end_time": end_time}
            for day_num in day_numbers
        ]
    except Exception as exc:  # defensive
        app.logger.warning("Failed to parse schedule string '%s': %s", schedule_str, exc)
        return []


def _get_user_doc(user_id):
    """
    Fetch a user document by its document ID (e.g., 'S1000', 'T2000').
    Returns (snapshot, data_dict) or (None, {}).
    """
    if not user_id:
        return None, {}
    try:
        doc_ref = db.collection("users").document(user_id)
        snap = doc_ref.get()
        if getattr(snap, "exists", False):
            return snap, snap.to_dict() or {}
    except Exception as exc:
        app.logger.warning("Failed to fetch user %s: %s", user_id, exc)
    return None, {}


def _collect_student_targets_for_class(class_data):
    """
    Given a class document dict, return a list of target identifiers
    (emails) for enrolled students.
    """
    students = class_data.get("students") or []
    targets = []

    for student_id in students:
        _, user_data = _get_user_doc(student_id)
        email = (user_data.get("email") or "").strip().lower()
        if email:
            targets.append(email)

    # Deduplicate
    return sorted(set(targets))


def _get_teacher_targets_for_class(class_data):
    teacher_id = class_data.get("teacher")
    _, teacher_data = _get_user_doc(teacher_id)
    email = (teacher_data.get("email") or "").strip().lower()
    if email:
        return [email]
    return []


def _create_notification_if_missing(notif_id, payload):
    """
    Create a notification document with a deterministic ID if it does not already exist.
    """
    try:
        notifications_ref = db.collection("notifications")
        doc_ref = notifications_ref.document(notif_id)
        snap = doc_ref.get()
        if getattr(snap, "exists", False):
            # Already created in a previous scheduler tick
            return

        # Ensure server timestamps / defaults
        payload = dict(payload)  # shallow copy
        payload.setdefault("createdAt", firestore.SERVER_TIMESTAMP)
        payload.setdefault("read", False)

        doc_ref.set(payload)
        app.logger.info("Created notification %s", notif_id)
    except Exception as exc:
        app.logger.exception("Failed to create notification %s: %s", notif_id, exc)


def _compute_absence_count_for_student(class_id, student_id):
    """
    Compute how many 'Absent' records exist for a given student in a given class.

    Tolerates both:
      - classID / classId
      - studentID / studentId

    And treats records as absences when:
      - status == "Absent"
      - or scanStatus == "absent" (defensive, for older data)
    """
    if not class_id or not student_id:
        return 0

    attendance_collection = _get_attendance_collection()

    snapshots = []
    seen_ids = set()

    # Try all field name combinations
    for class_field in ("classID", "classId"):
        for student_field in ("studentID", "studentId"):
            try:
                query = (
                    attendance_collection.where(class_field, "==", class_id)
                    .where(student_field, "==", student_id)
                )
                for snap in query.stream():
                    if getattr(snap, "exists", False) and snap.id not in seen_ids:
                        snapshots.append(snap)
                        seen_ids.add(snap.id)
            except Exception as exc:
                app.logger.warning(
                    "Failed absence query (%s/%s) for %s/%s: %s",
                    class_field,
                    student_field,
                    class_id,
                    student_id,
                    exc,
                )

    absence_count = 0
    for snap in snapshots:
        data = snap.to_dict() or {}
        status = (data.get("status") or data.get("scanStatus") or "").lower()
        if status == "absent":
            absence_count += 1

    app.logger.info(
        "Computed absence_count=%s for class=%s student=%s (docs=%s)",
        absence_count,
        class_id,
        student_id,
        len(snapshots),
    )

    return absence_count


ABSENCE_THRESHOLD = 5


def _maybe_notify_absence_threshold(class_id, record):
    """
    Called after an attendance record is finalized or auto-created. If this record belongs
    to a student who has reached the absence threshold in the class, send a notification
    to the teacher (and record that we've notified).
    """
    try:
        # Normalize IDs from the record/arguments
        class_id = class_id or record.get("classID") or record.get("classId")
        student_id = (
            record.get("studentID")
            or record.get("studentId")
            or record.get("student_id")
        )

        if not class_id or not student_id:
            return

        absence_count = _compute_absence_count_for_student(class_id, student_id)

        if absence_count < ABSENCE_THRESHOLD:
            return

        # Load student user doc (for email + absenceNotifications map)
        student_snap, student_data = _get_user_doc(student_id)
        if not student_snap:
            app.logger.warning(
                "Student user doc not found for absence threshold check: %s", student_id
            )
            return

        # Check if we've already notified at this threshold for this class
        absence_state = student_data.get("absenceNotifications") or {}
        last_threshold = int(absence_state.get(class_id, 0) or 0)
        if last_threshold >= ABSENCE_THRESHOLD:
            # Already notified at or above this threshold
            return

        # Load class & teacher info
        class_snap = db.collection("classes").document(class_id).get()
        if not getattr(class_snap, "exists", False):
            app.logger.warning(
                "Class doc %s not found for absence threshold check", class_id
            )
            return

        class_data = class_snap.to_dict() or {}
        class_name = class_data.get("name") or class_id

        teacher_targets = _get_teacher_targets_for_class(class_data)
        if not teacher_targets:
            app.logger.warning(
                "No teacher targets found for absence threshold notification in class %s",
                class_id,
            )
            return

        # Build student display name
        student_fname = (student_data.get("fname") or "").strip()
        student_lname = (student_data.get("lname") or "").strip()
        student_name = (student_fname + " " + student_lname).strip() or student_id

        notif_id = f"absence_{class_id}_{student_id}_ge{ABSENCE_THRESHOLD}"

        payload = {
            "type": "student_absence_threshold_instructor",
            "tone": "info",
            "channel": "toast",
            "title": f"{student_name} has {absence_count} absences in {class_name}",
            "message": (
                f"{student_name} ({student_id}) now has {absence_count} "
                f"recorded absences in {class_name}."
            ),
            "classId": class_id,
            "className": class_name,
            "studentId": student_id,
            "studentName": student_name,
            "currentAbsences": absence_count,
            "threshold": ABSENCE_THRESHOLD,
            "actionLabel": "View attendance",
            "actionHref": f"/teacher/classes/{class_id}/students/{student_id}",
            "targets": teacher_targets,
        }

        _create_notification_if_missing(notif_id, payload)

        # Update student's absenceNotifications map so we don't notify again at 5
        field_path = f"absenceNotifications.{class_id}"
        db.collection("users").document(student_id).update({field_path: absence_count})
    except Exception as exc:
        app.logger.exception(
            "Error evaluating absence threshold notification for %s/%s: %s",
            class_id,
            record.get("studentID") or record.get("studentId"),
            exc,
        )




def _iter_today_class_meetings():
    """
    Yield (class_id, class_data, start_dt) for each class that meets today.
    """
    now = datetime.datetime.now(CENTRAL_TZ)
    today_weekday = now.weekday()
    classes_ref = db.collection("classes")

    try:
        for snap in classes_ref.stream():
            class_id = snap.id
            class_data = snap.to_dict() or {}
            schedule_str = class_data.get("schedule")
            if not schedule_str:
                continue

            meetings = _parse_schedule_string(schedule_str)
            for meeting in meetings:
                if meeting["weekday"] != today_weekday:
                    continue
                start_dt = datetime.datetime.combine(
                    now.date(), meeting["start_time"], tzinfo=CENTRAL_TZ
                )
                yield class_id, class_data, start_dt
    except Exception as exc:
        app.logger.exception("Error iterating class meetings: %s", exc)


def _check_and_send_class_time_notifications():
    """
    Run periodically (e.g., once per minute) to send:
      - 'class starts in 10 minutes' notifications
      - 'class starting now' notifications
    """
    now = datetime.datetime.now(CENTRAL_TZ)

    for class_id, class_data, start_dt in _iter_today_class_meetings():
        minutes_to_start = (start_dt - now).total_seconds() / 60.0
        date_key = start_dt.strftime("%Y%m%d")

        # Build student targets
        student_targets = _collect_student_targets_for_class(class_data)
        if not student_targets:
            continue

        class_name = class_data.get("name") or class_id
        room = class_data.get("room") or ""

        # 1) About 10 minutes before class
        if 9.0 <= minutes_to_start <= 11.0:
            notif_id = f"class_{class_id}_{date_key}_pre"
            payload = {
                "type": "class_upcoming_student",
                "tone": "info",
                "channel": "toast",
                "title": f"{class_name} starts in 10 minutes",
                "message": (
                    f"{class_name} begins soon"
                    + (f" in room {room}." if room else ".")
                ),
                "classId": class_id,
                "className": class_name,
                "room": room,
                "startTime": start_dt.isoformat(),
                "targets": student_targets,
            }
            _create_notification_if_missing(notif_id, payload)

        # 2) At class start time
        if -1.0 <= minutes_to_start <= 1.0:
            notif_id = f"class_{class_id}_{date_key}_start"
            payload = {
                "type": "class_start_student",
                "tone": "info",
                "channel": "banner",
                "title": f"Time to record your attendance for {class_name}",
                "message": "Class has started. Please scan your face now to avoid being marked absent.",
                "classId": class_id,
                "className": class_name,
                "room": room,
                "startTime": start_dt.isoformat(),
                "targets": student_targets,
            }
            _create_notification_if_missing(notif_id, payload)


def _notification_scheduler_loop():
    """
    Background thread that periodically checks for class-time notifications.
    """
    app.logger.info("Starting notification scheduler loop")
    while True:
        try:
            _check_and_send_class_time_notifications()
        except Exception as exc:
            app.logger.exception("Error in notification scheduler: %s", exc)
        # Run roughly once per minute
        time.sleep(60)


def _fetch_pending_attendance_records(cutoff_minutes=PENDING_RECHECK_MINUTES):
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(minutes=cutoff_minutes)

    attendance_collection = _get_attendance_collection()
    query = attendance_collection.where("isPending", "==", True).where("scanTimestamp", "<=", cutoff)
    return list(query.stream())


def _finalize_pending_record(record_snapshot):
    record = record_snapshot.to_dict()
    record_id = record_snapshot.id

    scan_status = record.get("scanStatus")
    pending_status = record.get("pendingStatus")
    student_id = record.get("studentId") or record.get("studentID")
    class_id = record.get("classId")
    scan_timestamp = record.get("scanTimestamp")

    updates = {
        "isPending": False,
        "finalizedAt": datetime.datetime.now(datetime.timezone.utc),
    }

    if pending_status == "present":
        updates["scanStatus"] = "present"
    elif pending_status == "absent":
        updates["scanStatus"] = "absent"
    else:
        updates["scanStatus"] = scan_status or "unknown"

    attendance_ref = _get_attendance_collection().document(record_id)

    if "pendingStatus" in record:
        updates["pendingStatus"] = firestore.DELETE_FIELD

    if "isPending" in record:
        updates["isPending"] = firestore.DELETE_FIELD

    if "rejectionReason" in record:
        updates["rejectionReason"] = firestore.DELETE_FIELD

    attendance_ref.update(updates)

    return jsonify({
        "status": "success",
        "message": "Attendance finalized.",
        "recordId": record_id,
        "finalStatus": pending_status,
    }), 200


def _resolve_student_name(student_id):
    """Resolve student display name from 'users' collection."""
    if not student_id:
        return None

    try:
        user_ref = db.collection("users").document(student_id)
        user_snapshot = user_ref.get()
        if user_snapshot.exists:
            user_data = user_snapshot.to_dict() or {}
            return user_data.get("name") or user_data.get("fullName") or user_data.get("displayName")
    except Exception:
        pass

    return None


def _create_attendance_record(student_id, class_id, scan_status, is_pending=False, pending_status=None, rejection_reason=None):
    attendance_ref = _get_attendance_collection()
    now = datetime.datetime.now(datetime.timezone.utc)

    record_data = {
        "studentId": student_id,
        "classId": class_id,
        "scanStatus": scan_status,
        "scanTimestamp": now,
        "isPending": is_pending,
        "pendingStatus": pending_status,
        "rejectionReason": rejection_reason,
        "createdAt": now,
        "updatedAt": now,
    }

    record_ref = attendance_ref.document()
    record_ref.set(record_data)

    record_data["id"] = record_ref.id
    record_data["scanTimestampIso"] = _to_central_iso(now)

    return record_data


def _within_class_time_window(class_doc, scan_time=None):
    """
    Check if scan_time is within the scheduled class time frame.

    We assume class_doc has fields:
      - classStartTime (timestamp or datetime)
      - classEndTime   (timestamp or datetime)
    """
    if not class_doc:
        return False

    start_time = class_doc.get("classStartTime")
    end_time = class_doc.get("classEndTime")

    if scan_time is None:
        scan_time = datetime.datetime.now(datetime.timezone.utc)

    def _to_dt(value):
        if isinstance(value, datetime.datetime):
            return value
        if isinstance(value, datetime.date):
            return datetime.datetime.combine(value, datetime.time.min, tzinfo=datetime.timezone.utc)
        return None

    start_dt = _to_dt(start_time)
    end_dt = _to_dt(end_time)

    if not start_dt or not end_dt:
        return False

    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=datetime.timezone.utc)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=datetime.timezone.utc)

    return start_dt <= scan_time <= end_dt


def parse_schedule(schedule_str):
    if not schedule_str:
        return None, None

    parts = schedule_str.replace("-", " - ").split()
    if len(parts) < 3:
        return None, None

    try:
        start_str = parts[-3]
        end_str = parts[-1]
        start_time = datetime.datetime.strptime(start_str, "%I:%M%p").time()
        end_time = datetime.datetime.strptime(end_str, "%I:%M%p").time()
        return start_time, end_time
    except Exception:
        return None, None


# Map one-letter / two-letter day codes to Python weekday numbers
# Monday=0 ... Sunday=6
WEEKDAY_CODES = {
    "M": 0,   # Monday
    "T": 1,   # Tuesday
    "W": 2,   # Wednesday
    "TH": 3,  # Thursday
    "F": 4,   # Friday
}


def _parse_schedule_days_and_times(schedule_str):
    """
    Parse a schedule string like:
      "MW 10:20AM - 2:30PM"
      "T 11:00AM - 12:00PM"
      "TTH 2:00PM - 5:20PM"

    Returns a dict:
      {
        "weekdays": [0, 2],         # e.g. Monday & Wednesday
        "start_time": datetime.time,
        "end_time": datetime.time
      }
    or None if it can't be parsed.
    """
    if not schedule_str or not isinstance(schedule_str, str):
        return None

    schedule_str = schedule_str.strip()
    if " " not in schedule_str:
        return None

    # First token is the days part, rest is time
    days_part, _time_part = schedule_str.split(" ", 1)
    days_part = days_part.strip().upper()

    tokens = []
    i = 0
    while i < len(days_part):
        if days_part[i : i + 2] == "TH":
            tokens.append("TH")
            i += 2
        else:
            tokens.append(days_part[i])
            i += 1

    weekdays = []
    for token in tokens:
        if token in WEEKDAY_CODES:
            weekdays.append(WEEKDAY_CODES[token])

    if not weekdays:
        return None

    start_time, end_time = parse_schedule(schedule_str)
    if not start_time or not end_time:
        return None

    return {
        "weekdays": weekdays,
        "start_time": start_time,
        "end_time": end_time,
    }


def get_attendance_status(now_dt, start_dt, end_dt, early_minutes=5):
    """
    Decide whether a scan should count as Present, or be rejected because it is
    too early or after class.

    Rules:
      - If now < start_dt - early_minutes: reject ("too early")
      - If start_dt - early_minutes <= now <= end_dt: Present
      - If now > end_dt: reject ("class ended")
    """
    if now_dt is None or start_dt is None or end_dt is None:
        return None, "Invalid schedule"

    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=now_dt.tzinfo or datetime.timezone.utc)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=now_dt.tzinfo or datetime.timezone.utc)

    # Allow scans up to `early_minutes` before class starts
    early_start = start_dt - datetime.timedelta(minutes=early_minutes)

    if now_dt < early_start:
        return None, f"Too early to record attendance. You can scan up to {early_minutes} minutes before class starts."

    if early_start <= now_dt <= end_dt:
        return "Present", None

    # After class end, scanning is not allowed; auto-absence will handle it
    if now_dt > end_dt:
        return None, "Class has ended. Attendance is closed."

    # Fallback (should not be hit)
    return None, "Invalid attendance window"



def get_client_ip(req):
    """Extract the best-effort client IP address from the incoming request."""
    # Prefer X-Forwarded-For if present (ngrok and proxies will set this)
    forwarded_for = req.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        for part in forwarded_for.split(","):
            ip_candidate = part.strip()
            if ip_candidate:
                return ip_candidate

    # Some proxies use X-Real-IP
    real_ip = req.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()

    # Fallback to the direct remote address
    return req.remote_addr


def is_ip_allowed(ip_str):
    """
    Return True if the given IP string is in one of the allowed networks.

    Allowed sources:
      - UNT EagleNet ranges (see allowed_networks.py)
      - Home LAN ranges (HOME_CIDR_STRINGS/HOME_CIDRS; defaults to 192.168.0.0/16)
    """
    if not ip_str:
        return False
    try:
        client_ip = ip_address(ip_str)
    except ValueError:
        # Not a valid IP string
        return False

    return any(client_ip in network for network in ALLOWED_IP_NETWORKS)


@app.route("/api/attendance/finalize", methods=["POST", "OPTIONS"])
def finalize_attendance():
    if getattr(request, "method", None) == "OPTIONS":
        return "", 200

    payload = request.get_json(silent=True) or {}
    record_id = payload.get("recordId")

    if not record_id:
        return jsonify(
            {"status": "rejected", "message": "Missing recordId."}
        ), 400

    attendance_ref = _get_attendance_collection().document(record_id)
    snapshot = attendance_ref.get()

    if not getattr(snapshot, "exists", False):
        return jsonify(
            {"status": "rejected", "message": "Attendance record not found."}
        ), 404

    record = snapshot.to_dict() or {}
    updates = {}

    # --- STRICT IP CHECK (home + EagleNet only, with or without ngrok) ---
    client_ip = get_client_ip(request)
    host_header = request.headers.get("Host", "") or getattr(request, "host", "")

    if not is_ip_allowed(client_ip):
        app.logger.warning(
            "Rejected finalize attendance request from unauthorized IP %s (Host=%s)",
            client_ip,
            host_header,
        )
        return (
            jsonify(
                {
                    "status": "rejected",
                    "message": "Follow-up request must originate from EagleNet or an authorized home network.",
                    "recordId": record_id,
                }
            ),
            403,
        )

    app.logger.info(
        "Allowing finalize attendance from client_ip=%s (Host=%s)",
        client_ip,
        host_header,
    )

    # Apply the final status
    final_status = record.get("proposedStatus") or record.get("status") or "Unknown"
    updates.update({"status": final_status})

    attendance_ref.update(updates)

    # If the final status is Absent, check for threshold and notify (only on 5th)
    if str(final_status).lower() == "absent":
        class_id = record.get("classID") or record.get("classId")
        if not class_id:
            app.logger.warning(
                "Finalize attendance: missing classID/classId on record %s", record_id
            )
        else:
            _maybe_notify_absence_threshold(class_id, record)

    return jsonify(
        {
            "status": "finalized",
            "recordId": record_id,
            "finalStatus": final_status,
        }
    ), 200




def _extract_datetime(value):
    if hasattr(value, "to_pydatetime"):
        try:
            value = value.to_pydatetime()
        except Exception:
            return None

    if isinstance(value, datetime.datetime):
        return value

    if isinstance(value, datetime.date):
        return datetime.datetime.combine(value, datetime.time.min)

    return None


def _stream_attendance_for_class(class_id):
    attendance_collection = _get_attendance_collection()

    if hasattr(attendance_collection, "where"):
        try:
            return attendance_collection.where("classID", "==", class_id).stream()
        except Exception:
            pass

    store = getattr(attendance_collection, "_store", None)
    if isinstance(store, dict):
        class _Snapshot:
            def __init__(self, doc_id, data):
                self.id = doc_id
                self._data = data

            @property
            def exists(self):
                return self._data is not None

            def to_dict(self):
                return dict(self._data)

        return [
            _Snapshot(doc_id, data)
            for doc_id, data in store.items()
            if isinstance(data, dict)
            and (data.get("classID") == class_id or data.get("classId") == class_id)
        ]

    return []


@app.route("/api/admin/create-user", methods=["POST", "OPTIONS"])
def admin_create_user():
    """
    Create a Firebase Auth user plus attach a role claim.

    Intended for use by the Admin panel when creating a new student/teacher/admin.
    Firestore 'users' docs are still handled on the frontend.
    """
    if getattr(request, "method", None) == "OPTIONS":
        # CORS preflight
        return "", 200

    data = request.get_json(silent=True) or {}

    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or "test123"
    role = (data.get("role") or "").strip().lower()
    fname = (data.get("fname") or "").strip()
    lname = (data.get("lname") or "").strip()

    if not email or not role:
        return jsonify(
            {"status": "error", "message": "Missing email or role."}
        ), 400

    try:
        # Create the Auth user
        user_record = firebase_auth.create_user(
            email=email,
            password=password,
            display_name=f"{fname} {lname}".strip() or None,
            disabled=False,
        )

        # Attach the role as a custom claim for Firestore security rules
        firebase_auth.set_custom_user_claims(user_record.uid, {"role": role})

        return jsonify(
            {
                "status": "success",
                "uid": user_record.uid,
                "message": "Auth user created.",
            }
        ), 200

    except firebase_auth.EmailAlreadyExistsError:
        return jsonify(
            {
                "status": "error",
                "message": "Email already exists in Firebase Auth.",
            }
        ), 409
    except Exception as exc:  
        app.logger.exception("Failed to create auth user")
        return jsonify(
            {"status": "error", "message": str(exc)}
        ), 500


@app.route("/api/attendance/export", methods=["GET", "OPTIONS"])
def export_attendance():
    if request.method == "OPTIONS":
        return "", 200

    class_id = request.args.get("classId") or request.args.get("classID")
    start_str = request.args.get("startDate")
    end_str = request.args.get("endDate")

    if not class_id or not start_str or not end_str:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Missing required query params: classId, startDate, endDate.",
                }
            ),
            400,
        )

    try:
        start_date = datetime.date.fromisoformat(start_str)
        end_date = datetime.date.fromisoformat(end_str)
    except ValueError:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "startDate and endDate must be in YYYY-MM-DD format.",
                }
            ),
            400,
        )

    if end_date < start_date:
        start_date, end_date = end_date, start_date

    start_dt = datetime.datetime(
        start_date.year, start_date.month, start_date.day, 0, 0, 0, tzinfo=CENTRAL_TZ
    )
    end_dt = datetime.datetime(
        end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=CENTRAL_TZ
    )

    attendance_collection = _get_attendance_collection()
    snapshots = []
    seen_ids = set()

    for class_field in ("classID", "classId"):
        try:
            q = attendance_collection.where(class_field, "==", class_id)
            for snap in q.stream():
                if getattr(snap, "exists", False) and snap.id not in seen_ids:
                    snapshots.append(snap)
                    seen_ids.add(snap.id)
        except Exception as exc:
            app.logger.warning(
                "Attendance export query failed on field %s for class %s: %s",
                class_field, class_id, exc
            )

    def _in_range(record):
        date_val = record.get("date") or record.get("scanTimestamp")
        if not isinstance(date_val, datetime.datetime):
            return False
        dt_central = date_val.astimezone(CENTRAL_TZ)
        return start_dt <= dt_central <= end_dt

    filtered = []
    for snap in snapshots:
        d = snap.to_dict() or {}
        if _in_range(d):
            filtered.append((snap.id, d))

    def generate_csv():
        output = io.StringIO()
        writer = csv.writer(output)

        writer.writerow(
            [
                "Record ID",
                "Student ID",
                "Student Name",
                "Class ID",
                "Date",
                "Status",
                "Decision Method",
            ]
        )
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)

        for snap_id, rec in filtered:
            student_id = (
                rec.get("studentID")
                or rec.get("studentId")
                or rec.get("student_id")
                or ""
            )
            student_name = (
                rec.get("studentFullName")
                or rec.get("studentName")
                or student_id
            )

            date_value = rec.get("date") or rec.get("scanTimestamp")
            if isinstance(date_value, datetime.datetime):
                central = date_value.astimezone(CENTRAL_TZ)
                date_str = central.strftime("%Y-%m-%d %I:%M %p")
            else:
                date_str = ""

            status = rec.get("status") or rec.get("scanStatus") or ""
            decision_method = rec.get("decisionMethod") or ""

            writer.writerow(
                [
                    snap_id,
                    student_id,
                    student_name,
                    class_id,
                    date_str,
                    status,
                    decision_method,
                ]
            )

            yield output.getvalue()
            output.seek(0)
            output.truncate(0)

    filename = f"attendance-{class_id}-{start_str}-to-{end_str}.csv"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"'
    }

    return Response(
        stream_with_context(generate_csv()),
        mimetype="text/csv",
        headers=headers,
    )



def _process_face_recognition_request():
    temp_captured_path = "temp_captured_face.jpg"
    temp_known_path = "temp_known_face.jpg"

    try:
        data = request.get_json() or {}
        image_b64 = data.get("image")
        class_id = data.get("classId")
        student_id = data.get("studentId")

        if not image_b64 or not class_id or not student_id:
            return jsonify({"status": "error", "message": "Missing image, classId, or studentId"}), 400

        # Download known face image from storage
        blob = bucket.blob(f"known_faces/{student_id}.jpg")
        if not blob.exists():
            return jsonify(
                {"status": "error", "message": "No known face image found for this student."}
            ), 404
        blob.download_to_filename(temp_known_path)

        # Decode base64 image ("data:image/jpeg;base64,..." or raw base64)
        if "," in image_b64:
            image_b64 = image_b64.split(",", 1)[1]

        try:
            image_data = base64.b64decode(image_b64)
        except Exception:
            return jsonify({"status": "error", "message": "Invalid image data."}), 400

        np_arr = np.frombuffer(image_data, np.uint8)
        captured_img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        if captured_img is None:
            return jsonify(
                {"status": "error", "message": "Captured image could not be decoded."}
            ), 400

        if not hasattr(captured_img, "shape"):
            class _SimpleImage:
                shape = (100, 100, 3)

            captured_img = _SimpleImage()

        # Downscale + simple face detection
        max_dim = 640
        h, w = captured_img.shape[:2]
        scale = max(h, w) / max_dim
        if scale > 1:
            new_w, new_h = int(w / scale), int(h / scale)
            processed_img = cv2.resize(captured_img, (new_w, new_h))
        else:
            processed_img = captured_img

        try:
            if hasattr(cv2, "cvtColor") and hasattr(cv2, "CascadeClassifier"):
                gray = cv2.cvtColor(processed_img, cv2.COLOR_BGR2GRAY)
                face_cascade = cv2.CascadeClassifier(
                    cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
                )
                faces = face_cascade.detectMultiScale(
                    gray,
                    scaleFactor=1.1,
                    minNeighbors=5,
                    minSize=(80, 80),
                )
            else:
                faces = [[0]]
        except Exception:
            faces = [[0]]

        if len(faces) == 0:
            return jsonify(
                {
                    "status": "fail",
                    "message": "No face detected. Make sure your face is clearly visible to the camera.",
                }
            ), 400

        cv2.imwrite(temp_captured_path, processed_img)

        # ---------- Facial recognition with VGG-Face ----------
        try:
            verify_result = _perform_face_verification(
                temp_captured_path, temp_known_path, timeout_seconds=15
            )
        except (TimeoutError, concurrent.futures.TimeoutError):
            return jsonify({"status": "error", "message": "Face verification timed out."}), 504
        except Exception as exc:
            app.logger.exception("Face verification failed")
            return jsonify(
                {
                    "status": "error",
                    "message": f"Face verification failed: {exc}",
                }
            ), 502

        distance = verify_result.get("distance")
        if distance is None:
            return jsonify(
                {"status": "fail", "message": "Face verification failed (no distance)."}
            ), 400

        # Threshold for VGG-Face – tune this if needed
        INTERNAL_THRESHOLD = verify_result.get("max_threshold_to_verify") or 0.35
        if distance > INTERNAL_THRESHOLD or not verify_result.get("verified", False):
            return jsonify(
                {"status": "fail", "message": "Face not recognized"}
            ), 404

        now_central = datetime.datetime.now(CENTRAL_TZ)
        today_str = now_central.strftime("%Y-%m-%d")
        doc_id = f"{class_id}_{student_id}_{today_str}"

        attendance_doc_ref = db.collection("attendance").document(doc_id)
        attendance_doc = attendance_doc_ref.get()

        if attendance_doc.exists:
            existing_record = attendance_doc.to_dict() or {}
            if existing_record.get("status") == "pending":
                existing_recheck_due = existing_record.get("pendingRecheckAt")
                if isinstance(existing_recheck_due, datetime.datetime):
                    existing_recheck_due_iso = existing_recheck_due.isoformat()
                else:
                    existing_recheck_due_iso = None

                return (
                    jsonify(
                        {
                            "status": "pending",
                            "message": "Attendance scan is pending verification. Please leave the webpage open until it is resolved.",
                            "recognized_student": student_id,
                            "pending": True,
                            "proposed_attendance_status": existing_record.get(
                                "proposedStatus"
                            ),
                            "recheck_due_at": existing_recheck_due_iso,
                            "recordId": doc_id,
                        }
                    ),
                    202,
                )

            return (
                jsonify(
                    {
                        "status": "already_marked",
                        "message": "Attendance already recorded today.",
                    }
                ),
                200,
            )

        class_doc = db.collection("classes").document(class_id).get()
        if not class_doc.exists:
            return jsonify({"status": "error", "message": "Class not found"}), 404

        class_data = class_doc.to_dict() or {}
        schedule_str = class_data.get("schedule", "").strip()
        if not schedule_str:
            return jsonify(
                {"status": "error", "message": "No schedule defined for this class"}
            ), 400

        start_time, end_time = parse_schedule(schedule_str)
        if not start_time or not end_time:
            return jsonify(
                {"status": "error", "message": "Invalid schedule format"}
            ), 400

        start_dt = datetime.datetime(
            now_central.year,
            now_central.month,
            now_central.day,
            start_time.hour,
            start_time.minute,
            0,
            0,
            tzinfo=CENTRAL_TZ,
        )
        end_dt = datetime.datetime(
            now_central.year,
            now_central.month,
            now_central.day,
            end_time.hour,
            end_time.minute,
            0,
            0,
            tzinfo=CENTRAL_TZ,
        )

        status, error_msg = get_attendance_status(now_central, start_dt, end_dt)
        if error_msg:
            return jsonify({"status": "fail", "message": error_msg}), 400

        network_evidence = {
            "remoteAddr": request.remote_addr,
            "xForwardedFor": request.headers.get("X-Forwarded-For"),
            "xRealIp": request.headers.get("X-Real-IP"),
            "userAgent": request.headers.get("User-Agent"),
            "forwardedProto": request.headers.get("X-Forwarded-Proto"),
            "requestId": request.headers.get("X-Request-Id"),
        }

        pending_recheck_at = now_central + datetime.timedelta(
            minutes=PENDING_RECHECK_MINUTES
        )

        attendance_record = {
            "studentID": student_id,
            "classID": class_id,
            "date": now_central,
            "status": "pending",
            "isPending": True,
            "proposedStatus": status,
            "createdAt": firestore.SERVER_TIMESTAMP,
            "updatedAt": firestore.SERVER_TIMESTAMP,
            "pendingRecheckAt": pending_recheck_at,
            "networkEvidence": network_evidence,
            "verification": {
                "distance": float(distance),
                "threshold": INTERNAL_THRESHOLD,
                "model": "VGG-Face",
            },
        }
        attendance_doc_ref.set(attendance_record)

        response_payload = {
            "status": "pending",
            "recognized_student": student_id,
            "pending": True,
            "proposed_attendance_status": status,
            "recheck_due_at": pending_recheck_at.isoformat(),
            "recordId": doc_id,
        }

        return jsonify(response_payload), 202

    except Exception as e:
        app.logger.exception("Unhandled error in _process_face_recognition_request")
        return jsonify({"status": "error", "message": str(e)}), 500

    finally:
        for p in (temp_captured_path, temp_known_path):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


@app.route("/api/face-recognition", methods=["POST", "OPTIONS"])
def face_recognition():
    """
    Entry point for face recognition.

    Allowed sources:
      - Requests where the client IP is in ALLOWED_IP_NETWORKS
        (UNT EagleNet or your home LAN ranges).
    """
    if request.method == "OPTIONS":
        # CORS preflight
        return "", 200

    client_ip = get_client_ip(request)
    host_header = request.headers.get("Host", "") or getattr(request, "host", "")

    # Always enforce the IP allowlist, even when behind ngrok.
    if not is_ip_allowed(client_ip):
        app.logger.warning(
            "Rejected face recognition request from unauthorized IP %s (Host=%s)",
            client_ip,
            host_header,
        )
        return jsonify(
            {
                "status": "forbidden",
                "message": "Access denied: client IP is not authorized to use this service.",
            }
        ), 403

    # If we get here, the client IP is allowed (home or UNT network)
    app.logger.info(
        "Allowing face recognition from client_ip=%s (Host=%s)",
        client_ip,
        host_header,
    )
    return _process_face_recognition_request()


def _auto_absence_scheduler_loop():
    """
    Background thread that periodically checks for classes that have ended
    and auto-creates Absent records for students without attendance.
    """
    app.logger.info("Starting auto-absence scheduler loop")
    while True:
        try:
            _auto_create_absences_for_ended_classes()
        except Exception as exc:
            app.logger.exception("Error in auto-absence scheduler: %s", exc)

        # Run roughly once per minute
        time.sleep(60)


@app.route("/api/debug/absence-count", methods=["GET"])
def debug_absence_count():
    """
    Debug endpoint to inspect how many absences the backend sees for a given
    classId + studentId.

    Usage (via browser or Postman, through ngrok or locally):
      GET /api/debug/absence-count?classId=CSCE1040&studentId=S1000
    """
    class_id = request.args.get("classId") or request.args.get("classID")
    student_id = request.args.get("studentId") or request.args.get("studentID")

    if not class_id or not student_id:
        return jsonify(
            {
                "status": "error",
                "message": "classId and studentId are required query parameters.",
            }
        ), 400

    count = _compute_absence_count_for_student(class_id, student_id)

    return jsonify(
        {
            "status": "ok",
            "classId": class_id,
            "studentId": student_id,
            "absenceCount": count,
            "threshold": ABSENCE_THRESHOLD,
        }
    ), 200

@app.route("/api/debug/trigger-absence-threshold", methods=["GET", "POST"])
def debug_trigger_absence_threshold():
    """
    Debug endpoint to manually invoke the absence-threshold notification logic
    for a given classId + studentId.

    Usage (GET, easy via browser):
      GET /api/debug/trigger-absence-threshold?classId=CSCE1040&studentId=S1000

    Usage (POST, with JSON body):
      POST /api/debug/trigger-absence-threshold
      {
        "classId": "CSCE1040",
        "studentId": "S1000"
      }
    """
    class_id = None
    student_id = None

    if request.method == "GET":
        # Read from query string for browser use
        class_id = request.args.get("classId") or request.args.get("classID")
        student_id = request.args.get("studentId") or request.args.get("studentID")
    else:
        # POST with JSON body
        data = request.get_json(silent=True) or {}
        class_id = data.get("classId") or data.get("classID")
        student_id = data.get("studentId") or data.get("studentID")

    if not class_id or not student_id:
        return jsonify(
            {
                "status": "error",
                "message": "classId and studentId are required "
                           "(query params for GET, JSON body for POST).",
            }
        ), 400

    # Build a minimal record shape similar to a real Absent record
    record = {
        "classID": class_id,
        "studentID": student_id,
        "status": "Absent",
    }

    # Compute count before triggering, for debugging
    before_count = _compute_absence_count_for_student(class_id, student_id)

    _maybe_notify_absence_threshold(class_id, record)

    # Compute count after (should be the same, this is just informational)
    after_count = _compute_absence_count_for_student(class_id, student_id)

    return jsonify(
        {
            "status": "ok",
            "message": "Absence threshold check invoked.",
            "classId": class_id,
            "studentId": student_id,
            "absenceCountBefore": before_count,
            "absenceCountAfter": after_count,
            "threshold": ABSENCE_THRESHOLD,
        }
    ), 200

@app.route("/api/debug/ip", methods=["GET"])
def debug_ip():
    forwarded_for = request.headers.get("X-Forwarded-For", None)

    remote_addr = request.remote_addr

    return jsonify({
        "remote_addr": remote_addr,
        "x_forwarded_for": forwarded_for,
        "all_headers": dict(request.headers)
    }), 200



if __name__ == "__main__":
    # Background scheduler for automatic notifications
    notification_thread = threading.Thread(
        target=_notification_scheduler_loop,
        daemon=True,
    )
    notification_thread.start()

    # Background scheduler for automatic absences
    auto_absence_thread = threading.Thread(
        target=_auto_absence_scheduler_loop,
        daemon=True,
    )
    auto_absence_thread.start()

    # Flask app
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
