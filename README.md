# ConsultBae Assignment: Rakshit Rohan

## Setup Steps

**Requirements:** Python 3.10+, ffmpeg installed and on PATH, pip.

1. Clone the repo:
   ```
   git clone https://github.com/rakshitrohan-in/consultbae-assignment.git
   cd consultbae-assignment
   ```

2. **Generate the database** (must be done first. The DB is not committed to the repo; it's generated fresh from the source CSVs):
   ```
   cd pipeline
   pip install -r requirements.txt
   python pipeline.py
   ```
   This reads the 3 raw CSVs in `data/`, cleans and matches them, and writes `pipeline/consultbae.db`. Console output prints a full summary (row counts, dropped rows, match stats); this is the same output backing the Data Issues Report below.

3. **Run the audio submission app** (Task 3):
   ```
   cd ../audioapp
   pip install -r requirements.txt
   python app.py
   ```
   Open `http://127.0.0.1:5000` in your browser. Submit a name + phone with either a browser recording (mic access required) or a file upload. View all submissions with playback at `http://127.0.0.1:5000/submissions`.

4. **n8n automation** (Task 2): the workflow JSON files are in `n8n/` (main workflow + error handler). To run them, import both into an n8n instance (cloud or self-hosted), connect an OpenAI credential and a Google Sheets credential, and publish the main workflow. A live demo of this is shown in the screen recording linked below.

---

## Data Issues Report

Every data quality problem found across the 3 source files, and exactly what was done about each one. Verified against a live run of `pipeline.py` on August 13, 2026.

---

### Raw Input Volumes (before any cleaning)

| Source | Raw Rows |
|---|---|
| Source 1 (`source1_naukri_applicants.csv`) | 42 |
| Source 2 (`source2_gig_workers.csv`) | 32 |
| Source 3 (`source3_cbnexus_contacts.csv`) | 31 |

### 1. Structurally Broken Rows (3 dropped)

These rows were unusable at the row level. No amount of value-cleaning could fix them, so they were dropped before any matching logic ran.

| Issue | Location | What Was Found | Action Taken |
|---|---|---|---|
| Fully blank row | Source 2 | A row where every single field was empty | Dropped |
| Column-shifted row | Source 2 | Skill tags data (`react, javascript, mysql`) had landed in the `email_id` field, with the real email in the name field and other columns shifted one position over. Detected because the `email_id` field contained no `@` character | Dropped, logged with reason |
| Repeated header row | Source 3 | The literal header row (`Name, Phone Number, City, Verified, Projects Completed`) appeared again mid-file | Dropped |

### 2. Duplicate Applications Within Source 1 (2 deduped)

| Duplicate Pair | What Was Found | Resolution |
|---|---|---|
| Rohit Verma / "R. Verma" | Same phone, date, CTC, experience, and skills, but one entry used an abbreviated name | Kept the full name as canonical, dropped the abbreviated duplicate |
| Nikhil Chopra (alt-email) | Same phone, date, CTC, experience, and skills, but submitted under two different emails | Kept one canonical record, dropped the duplicate |

**Method:** duplicates identified by a fingerprint of **phone + date + CTC + experience + skills**, deliberately excluding name/email, since those are expected to vary between duplicate submissions from the same real person.

### 3. Cross-Source Person Matching

No single ID field exists across all 3 files, so people were matched using whatever contact fields overlapped.

| Match Type | Count |
|---|---|
| Source 2 rows matched to Source 1 via email | 15 |
| Source 3 rows matched to Source 1 via phone | 25 |
| Matched via name+city fallback (1-to-1 only) | 4 (Manish Bhatia, Divya Chopra, Karan Chopra, Vikram Mehta) |
| **Unique persons resolved overall** | **60** |

**Method:** Source 1 (has both email and phone) used as the bridge. A Union-Find structure chains matches transitively across all three files, which is necessary since Source 2 and Source 3 share no key with each other, only individually with Source 1.

### 4. Genuine Ambiguity: Correctly Left Unmerged

Two "Arjun Mehta" records, both in Noida: one exists only in Source 2, one only in Source 3. No shared email or phone links them to each other or to a third, already-merged Arjun Mehta (a different real person, same name, same city). Matching on name+city alone would risk merging two different real people into one record. **Left unmerged.** The pipeline logs this as ambiguous rather than guessing.

This was re-confirmed at the application layer: a test submission through the Task 3 audio app using "Arjun Mehta" (phone matching neither existing record) correctly created a **new** person instead of silently attaching to either ambiguous existing one. The same "don't guess" logic held consistently across the pipeline and the live app.

### 5. Value-Level Formatting Issues (cleaned, not dropped)

| Issue | Example | Fix Applied |
|---|---|---|
| Mixed CTC formats | `417964` (absolute) vs. `4.2` (lakhs) | Values under 1000 assumed lakhs, converted to absolute rupees |
| Mixed rate formats | `"250/hr"` vs. `"45k/month"` | Converted to common monthly-INR figure (hourly × 176 hrs; "k/month" × 1000) |
| Inconsistent date formats | `YYYY-MM-DD`, `DD-MM-YYYY`, `"7 Jul 2026"`, `MM/DD/YYYY` | Format detected per value, then parsed. Ambiguous slash-dates resolved as MM/DD |
| City aliasing | Gurgaon/Gurugram, Bangalore/Bengaluru, Delhi/New Delhi/Delhi NCR | Collapsed to one canonical spelling via lookup table |
| Trailing whitespace / casing | `" priya@example.com "`, `"NOIDA"` | Trimmed, normalized before comparison |
| Invalid phone numbers | Values not resolving to a clean 10-digit number | Set to null rather than forced into a match |

### Summary

- **105 raw rows** across 3 files → **60 unique real people**
- **3 structurally broken rows** dropped, each with a specific cause
- **2 duplicate applications** deduped
- **2 genuinely ambiguous matches** correctly left unmerged
- **0 orphaned foreign keys** in the final database

---

## Stuck Log

### 1. Source 2 and Source 3 don't share anything with each other

Source 1 has both email and phone, so it links fine to Source 2 (via email) and Source 3 (via phone) separately. But Source 2 and Source 3 have nothing in common with each other directly; no shared field at all. So a normal JOIN wasn't going to cut it.

**What I tried first:** I thought about just doing two separate JOINs, Source1 to Source2, then Source1 to Source3, and figured that'd link everything through Source1 fine. It does, technically. But it doesn't actually merge someone who shows up in Source2 *and* Source3 under the same Source1 record into one clean person. I'd have needed extra dedup logic on top, and that felt like it could get messy fast.

**What I asked AI:** I asked Claude Code to build the matching using Union-Find instead, basically a structure that groups things together across multiple links, so Source2 connects to Source1 via email, Source3 connects to Source1 via phone, and it all chains together properly in one pass instead of me stitching it together after the fact.

**What I rejected:** I considered just running the two JOINs and cleaning up duplicates afterward with pandas. Dropped that idea; too easy to mess up and accidentally create duplicate people if the cleanup step wasn't perfect. Union-Find is just the right tool for this kind of problem, so I went with that instead of trying to reinvent it badly.

### 2. Two different people, both named Arjun Mehta, both in Noida

One shows up only in Source 2, the other only in Source 3. Nothing in the data actually proves they're the same person or two different ones; same name, same city, that's it.

**Where I got stuck:** my gut reaction was to just merge them, since name+city was the only thing I had to go on. But then I realized, what if they're actually two different guys? Merging them would mean mixing up two real people's data, which felt way worse than just... not merging them.

**How I got unstuck:** I decided the logic should only auto-merge on name+city when it's a clean, one-to-one match, meaning there's exactly one candidate on each side, no ambiguity. If there's more than one possible match, it just logs it and leaves it alone instead of guessing. I actually tested this again later in Task 3: submitted a form with "Arjun Mehta" and a phone number that didn't match either existing record, and it correctly created a brand new person instead of guessing which one it was. That felt like a good sign the logic was actually sound, not just a one-off.

**What I rejected:** I thought about adding some kind of secondary signal, like matching on overlapping skills, to try and break the tie. Decided against it; that's not real proof of anything, it's just a guess wearing a fancier outfit.

### 3. n8n Cloud can't touch my local database

Original plan for Task 2 was to have n8n read and write directly to my actual `consultbae.db` file. Turns out that doesn't work, since n8n Cloud runs on their servers, not my laptop, so it has zero way of reaching a file sitting on my machine.

**What I looked into:** I do have n8n installed locally too, through Node.js, which would've solved this directly since it'd be on the same machine as the database. But the local version doesn't have the AI workflow builder that the cloud version has, and building the whole flow node-by-node by hand felt like a bigger time risk than I wanted to take with the clock running.

**What I decided instead:** bridged it through a Google Sheet. I exported my `persons` table (with a joined-in skills column) into a Sheet, and n8n Cloud reads/writes that live using its actual Google Sheets node. It's a real connection, just to a synced copy of the data instead of the raw file.

**What I rejected and why:** I dropped the self-hosted route for this one, purely because of time. Yeah, connecting straight to the actual DB file is the "more real" version of this automation, but the setup and debugging that would've taken wasn't worth it against a 48-hour clock, especially when the Sheets version still does everything the task actually asked for.

