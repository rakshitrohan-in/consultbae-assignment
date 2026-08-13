"""
ConsultBae data pipeline — merges three disjoint source CSVs (Naukri applicants,
gig-worker profiles, CBNexus contacts) into one normalized SQLite database.

Run with:  python pipeline/pipeline.py
"""

import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = Path(__file__).resolve().parent / "consultbae.db"

SOURCE1_CSV = DATA_DIR / "source1_naukri_applicants.csv"
SOURCE2_CSV = DATA_DIR / "source2_gig_workers.csv"
SOURCE3_CSV = DATA_DIR / "source3_cbnexus_contacts.csv"

# Collects everything the final summary (step 9) needs to report.
REPORT = {
    "raw_counts": {},          # source -> rows read from disk
    "dropped": [],             # list of (source, reason, row_preview)
    "dedup_dropped": [],       # list of (kept_name, dropped_name) within source1
    "match_stats": defaultdict(int),   # how each s2/s3 row got matched
    "ambiguous": [],           # fallback matches we refused to make
}


# ---------------------------------------------------------------------------
# Normalization helpers
#
# These are used BOTH to validate structural integrity (a field that can't be
# normalized at all is a strong signal the row is corrupt/misaligned) and to
# build the join keys used for entity resolution later.
# ---------------------------------------------------------------------------

def normalize_phone(raw):
    """Collapse any of +91XXXXXXXXXX / 0XXXXXXXXXX / XXX-XXX-XXXX / 91XXXXXXXXXX
    into a plain 10-digit string. Returns None if it can't be coerced to
    exactly 10 digits (a real Indian mobile number length), which we treat as
    'unparseable' rather than guessing.
    """
    if raw is None:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]          # strip country code (no leading +)
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]          # strip domestic trunk prefix
    return digits if len(digits) == 10 else None


def normalize_email(raw):
    """Lowercase + trim. Emails are case-insensitive by spec, and source2 has
    several fully-uppercase addresses that must line up with source1's
    lowercase ones for the join to work."""
    if raw is None:
        return None
    e = str(raw).strip().lower()
    return e if "@" in e else None


# Known aliases for the same city written differently across sources/HR
# systems. Mapped to one canonical spelling each. Gurgaon->Gurugram and
# Bangalore->Bengaluru use the current official city names; "New Delhi" /
# "Delhi NCR" collapse to "Delhi" since all three refer to the same metro
# for our matching purposes.
CITY_ALIASES = {
    "gurgaon": "Gurugram",
    "gurugram": "Gurugram",
    "bangalore": "Bengaluru",
    "bengaluru": "Bengaluru",
    "delhi": "Delhi",
    "new delhi": "Delhi",
    "delhi ncr": "Delhi",
    "noida": "Noida",
    "pune": "Pune",
}


def normalize_city(raw):
    if raw is None:
        return None
    key = re.sub(r"\s+", " ", str(raw).strip().lower())
    if not key:
        return None
    return CITY_ALIASES.get(key, key.title())


def normalize_name(raw):
    """Display-form name normalization: collapse whitespace, Title Case."""
    if raw is None:
        return None
    cleaned = re.sub(r"\s+", " ", str(raw).strip())
    return cleaned.title() if cleaned else None


def name_key(raw):
    """Matching-only key: lowercase, letters+spaces only, whitespace collapsed.
    Used for the name+city fallback join, never stored/displayed."""
    if raw is None:
        return ""
    s = re.sub(r"[^a-z\s]", "", str(raw).strip().lower())
    return re.sub(r"\s+", " ", s).strip()


# Applied Date shows up in 4 different formats in source1. Each is
# distinguished unambiguously by its separator + digit-group layout, so we
# just try each pattern in turn.
_DATE_FORMATS = [
    (re.compile(r"^\d{4}-\d{2}-\d{2}$"), "%Y-%m-%d"),        # 2026-08-08 (ISO)
    (re.compile(r"^\d{2}-\d{2}-\d{4}$"), "%d-%m-%Y"),        # 24-07-2026 (DD-MM-YYYY)
    (re.compile(r"^\d{2}/\d{2}/\d{4}$"), "%m/%d/%Y"),        # 07/13/2026 (MM/DD/YYYY)
    (re.compile(r"^\d{1,2} [A-Za-z]{3} \d{4}$"), "%d %b %Y"),  # 7 Jul 2026
]


def parse_applied_date(raw):
    if raw is None:
        return None
    s = str(raw).strip()
    for pattern, fmt in _DATE_FORMATS:
        if pattern.match(s):
            try:
                return datetime.strptime(s, fmt).date().isoformat()
            except ValueError:
                return None
    return None


# CTC values under this threshold are clearly "lakhs" shorthand (e.g. 4.2,
# meaning 4.2 lakh = 420000) rather than absolute rupees — no one's annual
# CTC is genuinely < 1000 INR. Values at/above it are already absolute.
LAKHS_THRESHOLD = 1000


def normalize_ctc(raw):
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return round(val * 100_000) if val < LAKHS_THRESHOLD else round(val)


# Assumption (documented for the call): an "/hr" gig rate is converted to a
# monthly figure using 8 hrs/day x 22 working days/month = 176 hrs/month,
# a standard full-time-equivalent contractor assumption.
HOURS_PER_MONTH = 176

_RATE_HOURLY = re.compile(r"^(\d+(?:\.\d+)?)/hr$")
_RATE_MONTHLY_K = re.compile(r"^(\d+(?:\.\d+)?)k/month$")


def parse_rate(raw):
    """Returns (rate_value, rate_unit, rate_monthly_inr) or (None, None, None)."""
    if raw is None:
        return None, None, None
    s = str(raw).strip().lower()
    m = _RATE_HOURLY.match(s)
    if m:
        val = float(m.group(1))
        return val, "hourly", round(val * HOURS_PER_MONTH)
    m = _RATE_MONTHLY_K.match(s)
    if m:
        val = float(m.group(1)) * 1000
        return val, "monthly", round(val)
    return None, None, None


# ---------------------------------------------------------------------------
# Step 1+2: load each CSV and drop structurally broken rows
# ---------------------------------------------------------------------------

def load_raw(path):
    # dtype=str + keep_default_na=False: we want the literal text (including
    # empty strings) so we can decide for ourselves what counts as "blank",
    # rather than pandas silently turning "" into NaN.
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def drop_row(source, reason, values):
    REPORT["dropped"].append((source, reason, values))


def clean_structural(df, source, header_cols, row_is_valid):
    """Applies the three structural checks shared by every source, in order:
    1. fully blank row
    2. header row repeated mid-file (a copy/paste artifact from concatenating
       exports)
    3. a source-specific validity check that catches column-shifted /
       misaligned rows (a required field doesn't look like it should, e.g.
       the 'email' column has no '@')
    Returns a cleaned, index-reset DataFrame.
    """
    header_tuple = tuple(header_cols)
    keep_idx = []
    for i, row in df.iterrows():
        values = [str(v).strip() for v in row.tolist()]

        if all(v == "" for v in values):
            drop_row(source, "fully blank row", values)
            continue

        if tuple(values) == header_tuple:
            drop_row(source, "repeated header row mid-file", values)
            continue

        ok, reason = row_is_valid(values)
        if not ok:
            drop_row(source, f"structurally invalid/shifted row - {reason}", values)
            continue

        keep_idx.append(i)

    return df.loc[keep_idx].reset_index(drop=True)


def validate_source1(values):
    full_name, email, phone, city, experience, ctc, applied_date, skills = values
    if normalize_email(email) is None:
        return False, f"'Email' has no '@' ({email!r})"
    if normalize_phone(phone) is None:
        return False, f"'Phone' doesn't reduce to 10 digits ({phone!r})"
    try:
        float(experience)
    except ValueError:
        return False, f"'Experience (Years)' not numeric ({experience!r})"
    if normalize_ctc(ctc) is None:
        return False, f"'Current CTC' not numeric ({ctc!r})"
    if parse_applied_date(applied_date) is None:
        return False, f"'Applied Date' matches none of the 4 known formats ({applied_date!r})"
    return True, ""


def validate_source2(values):
    email_id, worker_name, rate, location, status, skill_tags = values
    if normalize_email(email_id) is None:
        return False, f"'email_id' has no '@' ({email_id!r}) - looks column-shifted"
    if parse_rate(rate)[0] is None:
        return False, f"'rate' matches neither 'X/hr' nor 'Xk/month' ({rate!r})"
    if status.strip().lower() not in {"active", "inactive", "paused"}:
        return False, f"'status' not one of active/inactive/paused ({status!r})"
    return True, ""


def validate_source3(values):
    name, phone_number, city, verified, projects_completed = values
    if normalize_phone(phone_number) is None:
        return False, f"'Phone Number' doesn't reduce to 10 digits ({phone_number!r})"
    try:
        int(projects_completed)
    except ValueError:
        return False, f"'Projects Completed' not an integer ({projects_completed!r})"
    return True, ""


def load_source1():
    df = load_raw(SOURCE1_CSV)
    REPORT["raw_counts"]["source1"] = len(df)
    header = ["Full Name", "Email", "Phone", "City", "Experience (Years)",
              "Current CTC", "Applied Date", "Skills"]
    df = clean_structural(df, "source1", header, validate_source1)

    # Step 3/5: derive normalized columns used for matching + storage.
    df["_email_norm"] = df["Email"].map(normalize_email)
    df["_phone_norm"] = df["Phone"].map(normalize_phone)
    df["_city_norm"] = df["City"].map(normalize_city)
    df["_name_norm"] = df["Full Name"].map(normalize_name)
    df["_name_key"] = df["Full Name"].map(name_key)
    df["_ctc_inr"] = df["Current CTC"].map(normalize_ctc)
    df["_date_iso"] = df["Applied Date"].map(parse_applied_date)
    return df


def load_source2():
    df = load_raw(SOURCE2_CSV)
    REPORT["raw_counts"]["source2"] = len(df)
    header = ["email_id", "worker_name", "rate", "location", "status", "skill_tags"]
    df = clean_structural(df, "source2", header, validate_source2)

    df["_email_norm"] = df["email_id"].map(normalize_email)
    df["_city_norm"] = df["location"].map(normalize_city)
    df["_name_norm"] = df["worker_name"].map(normalize_name)
    df["_name_key"] = df["worker_name"].map(name_key)
    rate_parsed = df["rate"].map(parse_rate)
    df["_rate_value"] = rate_parsed.map(lambda t: t[0])
    df["_rate_unit"] = rate_parsed.map(lambda t: t[1])
    df["_rate_monthly_inr"] = rate_parsed.map(lambda t: t[2])
    return df


def load_source3():
    df = load_raw(SOURCE3_CSV)
    REPORT["raw_counts"]["source3"] = len(df)
    header = ["Name", "Phone Number", "City", "Verified", "Projects Completed"]
    df = clean_structural(df, "source3", header, validate_source3)

    df["_phone_norm"] = df["Phone Number"].map(normalize_phone)
    df["_city_norm"] = df["City"].map(normalize_city)
    df["_name_norm"] = df["Name"].map(normalize_name)
    df["_name_key"] = df["Name"].map(name_key)
    return df


# ---------------------------------------------------------------------------
# Step 6: dedupe exact-duplicate applications within source1
#
# Two rows count as "the same application submitted twice" when they share
# the same normalized phone AND the same applied date/CTC/experience/skills
# - i.e. every fact about the application matches - even though the name is
# abbreviated differently (e.g. "R. Verma" vs "Rohit Verma") or the email has
# an "alt." prefix on the second submission. Name/email themselves are
# deliberately excluded from the fingerprint since those are exactly the
# fields expected to vary between the two submissions.
# ---------------------------------------------------------------------------

def _looks_abbreviated(name):
    """True for names like 'R. Verma' that use an initial instead of a full
    first name - used to prefer the more complete record as canonical."""
    return "." in name


def dedupe_source1(df):
    groups = defaultdict(list)
    for i, row in df.iterrows():
        fingerprint = (
            row["_phone_norm"],
            row["_date_iso"],
            row["_ctc_inr"],
            row["Experience (Years)"].strip(),
            row["Skills"].strip().lower(),
        )
        groups[fingerprint].append(i)

    keep_idx = []
    for idxs in groups.values():
        if len(idxs) == 1:
            keep_idx.append(idxs[0])
            continue
        # Prefer the row with the non-abbreviated name as canonical; break
        # remaining ties by keeping the first-seen row.
        idxs_sorted = sorted(idxs, key=lambda i: (_looks_abbreviated(df.loc[i, "Full Name"]), i))
        kept = idxs_sorted[0]
        keep_idx.append(kept)
        for dropped in idxs_sorted[1:]:
            REPORT["dedup_dropped"].append(
                (df.loc[kept, "Full Name"], df.loc[kept, "Email"],
                 df.loc[dropped, "Full Name"], df.loc[dropped, "Email"])
            )

    keep_idx.sort()
    return df.loc[keep_idx].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Step 4: cross-source entity resolution via Union-Find
#
# source1 is the bridge: it's the only source with both an email and a
# phone, so we anchor source2 rows to it by email and source3 rows to it by
# phone. Anything left unmatched gets one more chance via normalized
# name+city, but ONLY when that key resolves to exactly one source1 row AND
# exactly one leftover source2/3 row — an ambiguous key (0 or 2+ candidates
# on either side) is left unmerged rather than guessed at.
# ---------------------------------------------------------------------------

class UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def resolve_entities(s1, s2, s3):
    uf = UnionFind()
    for i in s1.index:
        uf.find(("s1", i))
    for i in s2.index:
        uf.find(("s2", i))
    for i in s3.index:
        uf.find(("s3", i))

    # --- bridge match 1: source2 -> source1 via normalized email ---
    s1_by_email = defaultdict(list)
    for i, email in s1["_email_norm"].items():
        s1_by_email[email].append(i)

    s2_matched, s3_matched = set(), set()
    for i, email in s2["_email_norm"].items():
        candidates = s1_by_email.get(email, [])
        if candidates:
            for c in candidates:
                uf.union(("s2", i), ("s1", c))
            s2_matched.add(i)
            REPORT["match_stats"]["source2_matched_via_email"] += 1

    # --- bridge match 2: source3 -> source1 via normalized phone ---
    s1_by_phone = defaultdict(list)
    for i, phone in s1["_phone_norm"].items():
        s1_by_phone[phone].append(i)

    for i, phone in s3["_phone_norm"].items():
        candidates = s1_by_phone.get(phone, [])
        if candidates:
            for c in candidates:
                uf.union(("s3", i), ("s1", c))
            s3_matched.add(i)
            REPORT["match_stats"]["source3_matched_via_phone"] += 1

    # --- fallback: name + city, only when unambiguous on both sides ---
    s1_by_namecity = defaultdict(list)
    for i, row in s1.iterrows():
        s1_by_namecity[(row["_name_key"], row["_city_norm"])].append(i)

    leftover_by_namecity = defaultdict(list)
    for i, row in s2.iterrows():
        if i not in s2_matched:
            leftover_by_namecity[(row["_name_key"], row["_city_norm"])].append(("s2", i))
    for i, row in s3.iterrows():
        if i not in s3_matched:
            leftover_by_namecity[(row["_name_key"], row["_city_norm"])].append(("s3", i))

    for key, group in leftover_by_namecity.items():
        s1_candidates = s1_by_namecity.get(key, [])
        if len(s1_candidates) == 1 and len(group) == 1:
            src, i = group[0]
            uf.union((src, i), ("s1", s1_candidates[0]))
            if src == "s2":
                s2_matched.add(i)
                REPORT["match_stats"]["source2_matched_via_name_city_fallback"] += 1
            else:
                s3_matched.add(i)
                REPORT["match_stats"]["source3_matched_via_name_city_fallback"] += 1
        elif s1_candidates:
            # A source1 person with this name+city genuinely exists, but we
            # can't safely say which leftover row (if any) is really them -
            # either source1 has 2+ people with this name+city, or 2+
            # leftover rows are competing for the same one. That's a true
            # ambiguity, worth a warning. (When s1_candidates is empty there's
            # nothing to be ambiguous about - the row(s) just have no source1
            # anchor and become their own standalone person(s), silently.)
            name, city = key
            for src, i in group:
                df = s2 if src == "s2" else s3
                REPORT["ambiguous"].append({
                    "source": src,
                    "row_name": df.loc[i, "_name_norm"],
                    "city": city or "(unknown)",
                    "source1_candidates": len(s1_candidates),
                    "leftover_rows_sharing_key": len(group),
                })

    return uf


# ---------------------------------------------------------------------------
# Build the `persons` table from the resolved Union-Find components.
# Preference order for identity fields when a component spans sources:
# source1 (has both email+phone) > source3 (has phone) > source2 (has email).
# ---------------------------------------------------------------------------

def build_persons(uf, s1, s2, s3):
    groups = defaultdict(list)
    for i in s1.index:
        groups[uf.find(("s1", i))].append(("s1", i))
    for i in s2.index:
        groups[uf.find(("s2", i))].append(("s2", i))
    for i in s3.index:
        groups[uf.find(("s3", i))].append(("s3", i))

    persons = []
    node_to_person = {}
    for person_id, (_, members) in enumerate(sorted(groups.items()), start=1):
        s1_rows = [i for src, i in members if src == "s1"]
        s2_rows = [i for src, i in members if src == "s2"]
        s3_rows = [i for src, i in members if src == "s3"]

        if s1_rows:
            r = s1.loc[s1_rows[0]]
            full_name, email, phone, city = r["_name_norm"], r["_email_norm"], r["_phone_norm"], r["_city_norm"]
        elif s3_rows:
            r = s3.loc[s3_rows[0]]
            full_name, city = r["_name_norm"], r["_city_norm"]
            phone = r["_phone_norm"]
            email = s2.loc[s2_rows[0], "_email_norm"] if s2_rows else None
        else:
            r = s2.loc[s2_rows[0]]
            full_name, email, city = r["_name_norm"], r["_email_norm"], r["_city_norm"]
            phone = None

        sources = ",".join(
            s for s, present in
            (("source1", bool(s1_rows)), ("source2", bool(s2_rows)), ("source3", bool(s3_rows)))
            if present
        )
        persons.append({
            "person_id": person_id, "full_name": full_name, "email": email,
            "phone": phone, "city": city, "sources": sources,
        })
        for src, i in members:
            node_to_person[(src, i)] = person_id

    return persons, node_to_person


# ---------------------------------------------------------------------------
# Step 7+8: write everything to SQLite
# ---------------------------------------------------------------------------

def write_database(persons, node_to_person, s1, s2, s3):
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE persons (
            person_id INTEGER PRIMARY KEY,
            full_name TEXT,
            email TEXT,
            phone TEXT,
            city TEXT,
            sources TEXT NOT NULL
        );

        CREATE TABLE naukri_applications (
            application_id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL REFERENCES persons(person_id),
            full_name TEXT,
            email TEXT,
            phone TEXT,
            city TEXT,
            experience_years REAL,
            current_ctc_raw TEXT,
            current_ctc_inr INTEGER,
            applied_date_raw TEXT,
            applied_date_iso TEXT,
            skills TEXT
        );

        CREATE TABLE gig_profiles (
            gig_id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL REFERENCES persons(person_id),
            email_id TEXT,
            worker_name TEXT,
            rate_raw TEXT,
            rate_value REAL,
            rate_unit TEXT,
            rate_monthly_inr INTEGER,
            location TEXT,
            status TEXT,
            skill_tags TEXT
        );

        CREATE TABLE cbnexus_contacts (
            contact_id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER NOT NULL REFERENCES persons(person_id),
            name TEXT,
            phone_number TEXT,
            city TEXT,
            verified TEXT,
            projects_completed INTEGER
        );

        CREATE TABLE audio_submissions (
            submission_id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id INTEGER REFERENCES persons(person_id),
            filename TEXT,
            duration_sec REAL,
            sample_rate_khz REAL,
            bitrate_kbps REAL,
            loudness_db REAL,
            noise_estimate REAL,
            submitted_at TEXT
        );
    """)

    cur.executemany(
        "INSERT INTO persons (person_id, full_name, email, phone, city, sources) "
        "VALUES (:person_id, :full_name, :email, :phone, :city, :sources)",
        persons,
    )

    for i, row in s1.iterrows():
        cur.execute(
            """INSERT INTO naukri_applications
               (person_id, full_name, email, phone, city, experience_years,
                current_ctc_raw, current_ctc_inr, applied_date_raw, applied_date_iso, skills)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (node_to_person[("s1", i)], row["Full Name"], row["Email"], row["Phone"], row["City"],
             float(row["Experience (Years)"]), row["Current CTC"], row["_ctc_inr"],
             row["Applied Date"], row["_date_iso"], row["Skills"]),
        )

    for i, row in s2.iterrows():
        cur.execute(
            """INSERT INTO gig_profiles
               (person_id, email_id, worker_name, rate_raw, rate_value, rate_unit,
                rate_monthly_inr, location, status, skill_tags)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (node_to_person[("s2", i)], row["email_id"], row["worker_name"], row["rate"],
             row["_rate_value"], row["_rate_unit"], row["_rate_monthly_inr"],
             row["location"], row["status"], row["skill_tags"]),
        )

    for i, row in s3.iterrows():
        cur.execute(
            """INSERT INTO cbnexus_contacts
               (person_id, name, phone_number, city, verified, projects_completed)
               VALUES (?,?,?,?,?,?)""",
            (node_to_person[("s3", i)], row["Name"], row["Phone Number"], row["City"],
             row["Verified"], int(row["Projects Completed"])),
        )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Step 9: summary
# ---------------------------------------------------------------------------

def print_summary(persons):
    print("\n" + "=" * 70)
    print("PIPELINE SUMMARY")
    print("=" * 70)

    print("\nRows loaded per source (raw, before cleaning):")
    for src, n in REPORT["raw_counts"].items():
        print(f"  {src}: {n}")

    print(f"\nStructurally broken rows dropped: {len(REPORT['dropped'])}")
    for src, reason, values in REPORT["dropped"]:
        print(f"  [{src}] {reason} -> {values}")

    print(f"\nExact-duplicate source1 applications deduped: {len(REPORT['dedup_dropped'])}")
    for kept_name, kept_email, dropped_name, dropped_email in REPORT["dedup_dropped"]:
        print(f"  kept '{kept_name}' <{kept_email}>; dropped duplicate "
              f"'{dropped_name}' <{dropped_email}> (same phone/date/CTC/experience/skills)")

    print("\nCross-source match breakdown:")
    for k, v in REPORT["match_stats"].items():
        print(f"  {k}: {v}")

    print(f"\nUnique persons resolved: {len(persons)}")

    print(f"\nAmbiguous / unmerged name+city fallback candidates: {len(REPORT['ambiguous'])}")
    for a in REPORT["ambiguous"]:
        print(f"  [{a['source']}] '{a['row_name']}' in '{a['city']}' -> "
              f"{a['source1_candidates']} source1 candidate(s), "
              f"{a['leftover_rows_sharing_key']} leftover row(s) sharing that key "
              f"-- left unmerged")

    print("\nDone. Database written to:", DB_PATH)


def main():
    s1 = load_source1()
    s1 = dedupe_source1(s1)
    s2 = load_source2()
    s3 = load_source3()

    uf = resolve_entities(s1, s2, s3)
    persons, node_to_person = build_persons(uf, s1, s2, s3)
    write_database(persons, node_to_person, s1, s2, s3)
    print_summary(persons)


if __name__ == "__main__":
    main()
