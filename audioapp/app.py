"""
Mini audio submission tool.

Serves a form that records/uploads a short audio clip, probes it with ffmpeg,
estimates loudness/noise with pydub, matches the submitter against the
existing `persons` table (or creates one), and stores the result in
`audio_submissions`. See ../pipeline/pipeline.py for the Task 1 pipeline this
app's person-matching logic mirrors.

Run with:  python audioapp/app.py
"""

import json
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, send_from_directory, url_for
from pydub import AudioSegment
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR.parent / "pipeline" / "consultbae.db"
UPLOAD_FOLDER = BASE_DIR / "uploads"

ALLOWED_EXTENSIONS = {"mp3", "wav", "m4a", "webm", "ogg"}

app = Flask(__name__)
app.secret_key = "audioapp-dev-secret"  # only used to sign flash-message cookies


# ---------------------------------------------------------------------------
# Normalization helpers — copied from pipeline.py so this app matches
# submitters against `persons` using exactly the same rules Task 1 used to
# build that table (10-digit phone, Title-Case display name, letters-only
# matching key).
# ---------------------------------------------------------------------------

def normalize_phone(raw):
    if raw is None:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def normalize_name(raw):
    if raw is None:
        return None
    cleaned = re.sub(r"\s+", " ", str(raw).strip())
    return cleaned.title() if cleaned else None


def name_key(raw):
    if raw is None:
        return ""
    s = re.sub(r"[^a-z\s]", "", str(raw).strip().lower())
    return re.sub(r"\s+", " ", s).strip()


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Person matching — mirrors the pipeline's "never guess on an ambiguous
# signal" principle. We only have name + phone here (no email/city), so a
# "clear match" requires both: the normalized phone matches a persons row
# AND that row's normalized name matches too. Phone-only matches with a
# different name are treated as a different person rather than silently
# attaching a new submission to the wrong record.
# ---------------------------------------------------------------------------

def match_or_create_person(conn, raw_name, raw_phone):
    phone = normalize_phone(raw_phone)
    display_name = normalize_name(raw_name)
    key = name_key(raw_name)

    person_id = None
    if phone:
        rows = conn.execute("SELECT person_id, full_name FROM persons WHERE phone = ?", (phone,)).fetchall()
        exact = [r for r in rows if name_key(r["full_name"] or "") == key]
        if len(exact) == 1:
            person_id = exact[0]["person_id"]

    if person_id is None:
        cur = conn.execute(
            "INSERT INTO persons (full_name, email, phone, city, sources) VALUES (?, ?, ?, ?, ?)",
            (display_name, None, phone, None, "audioapp"),
        )
        person_id = cur.lastrowid

    return person_id


# ---------------------------------------------------------------------------
# Audio analysis
# ---------------------------------------------------------------------------

def probe_audio(path):
    """Runs ffprobe to pull duration/sample-rate/bitrate from the audio stream."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout)
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError) as e:
        print(f"  [ffprobe] could not analyze {path.name}: {e}")
        return None, None, None

    fmt = data.get("format", {})
    audio_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})

    duration_sec = None
    for source in (audio_stream, fmt):
        if source.get("duration"):
            duration_sec = round(float(source["duration"]), 3)
            break

    sample_rate_khz = None
    if audio_stream.get("sample_rate"):
        sample_rate_khz = round(int(audio_stream["sample_rate"]) / 1000, 3)

    bit_rate = audio_stream.get("bit_rate") or fmt.get("bit_rate")
    bitrate_kbps = round(int(bit_rate) / 1000, 2) if bit_rate else None

    return duration_sec, sample_rate_khz, bitrate_kbps


# A frame this short (16-bit PCM, full scale) is inaudibly quiet; used as a
# floor so digital-silence frames (dBFS == -inf) don't wreck the percentile
# calculation below.
SILENCE_FLOOR_DB = -96.0
NOISE_FRAME_MS = 50
NOISE_PERCENTILE = 0.10


def analyze_audio(path):
    """
    loudness_db: pydub's AudioSegment.dBFS - an RMS-based loudness measure
    (20*log10(rms_amplitude / max_possible_amplitude)), i.e. how loud the
    whole clip is relative to full scale. This is the standard, simple
    "how loud is this file" number; it is not perceptual (LUFS) loudness.

    noise_estimate (bonus/exploratory): the clip is chopped into 50ms frames
    and each frame's dBFS is computed. We report the 10th-percentile
    (quietest 10%) frame level as a stand-in for the ambient noise floor,
    on the assumption most recordings have at least a few non-speech gaps
    where only background noise is present. This is a rough heuristic, not
    a real noise-floor/SNR estimator, but it's simple and explainable.
    """
    try:
        audio = AudioSegment.from_file(path)
    except Exception as e:
        print(f"  [pydub] could not analyze {path.name}: {e}")
        return None, None

    loudness_db = round(audio.dBFS, 2) if audio.dBFS != float("-inf") else SILENCE_FLOOR_DB

    frame_levels = []
    for start_ms in range(0, len(audio), NOISE_FRAME_MS):
        level = audio[start_ms:start_ms + NOISE_FRAME_MS].dBFS
        frame_levels.append(level if level != float("-inf") else SILENCE_FLOOR_DB)

    if not frame_levels:
        return loudness_db, None

    frame_levels.sort()
    idx = max(0, int(len(frame_levels) * NOISE_PERCENTILE) - 1)
    noise_estimate = round(frame_levels[idx], 2)

    return loudness_db, noise_estimate


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        audio_file = request.files.get("audio_file")

        if not name or not phone:
            flash("Name and phone are both required.", "error")
            return redirect(url_for("index"))
        if audio_file is None or audio_file.filename == "":
            flash("Please record audio or choose a file to upload.", "error")
            return redirect(url_for("index"))
        if not allowed_file(audio_file.filename):
            flash("Unsupported audio format. Use mp3, wav, m4a, webm, or ogg.", "error")
            return redirect(url_for("index"))

        UPLOAD_FOLDER.mkdir(exist_ok=True)
        original_name = secure_filename(audio_file.filename) or "recording.webm"
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        stored_filename = f"{timestamp}_{original_name}"
        stored_path = UPLOAD_FOLDER / stored_filename
        audio_file.save(stored_path)

        duration_sec, sample_rate_khz, bitrate_kbps = probe_audio(stored_path)
        loudness_db, noise_estimate = analyze_audio(stored_path)

        conn = get_db()
        person_id = match_or_create_person(conn, name, phone)
        conn.execute(
            """INSERT INTO audio_submissions
               (person_id, filename, duration_sec, sample_rate_khz, bitrate_kbps,
                loudness_db, noise_estimate, submitted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (person_id, stored_filename, duration_sec, sample_rate_khz, bitrate_kbps,
             loudness_db, noise_estimate, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        conn.commit()
        conn.close()

        flash("Submission received. Thank you!", "success")
        return redirect(url_for("index"))

    return render_template("index.html")


@app.route("/submissions")
def submissions():
    conn = get_db()
    rows = conn.execute(
        """
        SELECT s.submission_id, p.full_name, p.phone, s.filename, s.duration_sec,
               s.sample_rate_khz, s.bitrate_kbps, s.loudness_db, s.noise_estimate,
               s.submitted_at
        FROM audio_submissions s
        LEFT JOIN persons p ON p.person_id = s.person_id
        ORDER BY s.submitted_at DESC, s.submission_id DESC
        """
    ).fetchall()
    conn.close()
    return render_template("submissions.html", rows=rows)


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


if __name__ == "__main__":
    UPLOAD_FOLDER.mkdir(exist_ok=True)
    port = 5000
    print("=" * 60)
    print("Audio submission tool starting")
    print(f"  Database:  {DB_PATH}")
    print(f"  Uploads:   {UPLOAD_FOLDER} (created if missing)")
    print(f"  Form:      http://127.0.0.1:{port}/")
    print(f"  Listing:   http://127.0.0.1:{port}/submissions")
    print("=" * 60)
    app.run(debug=True, port=port)
