# streamlit_app.py
"""
Aiclex Hallticket Mailer — Final with persistent SQLite send log + Resume
Drop-in streamlit app. Run: streamlit run streamlit_app.py
Dependencies: streamlit, pandas
"""

import streamlit as st
import pandas as pd
import zipfile, os, io, tempfile, shutil, time, re, sqlite3, json
try:
    import PyPDF2
except ImportError:
    PyPDF2 = None
from collections import defaultdict
from email.message import EmailMessage
import smtplib
from datetime import datetime

# ---------------- Config ----------------
# Use /tmp — writable on Streamlit Cloud (unlike /mount/src which is read-only)
APP_DB = os.path.join(tempfile.gettempdir(), "aiclex_send_logs.db")
LOG_TABLE = "email_sends"

st.set_page_config(page_title="Aiclex Hallticket Mailer", layout="wide")

# ── Custom CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
/* ── Global ── */
html, body, [class*="css"] { font-family: 'Inter', 'Segoe UI', sans-serif; }

/* ── Page title ── */
h1 { font-size: 1.6rem !important; font-weight: 700 !important;
     color: #0f172a !important; letter-spacing: -0.5px; }

/* ── Section headers ── */
h2 { font-size: 1.1rem !important; font-weight: 600 !important;
     color: #1e293b !important; text-transform: uppercase;
     letter-spacing: 0.5px; margin-top: 0.5rem !important; }

/* ── Metric cards ── */
[data-testid="metric-container"] {
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    padding: 12px 16px;
}
[data-testid="metric-container"] label { color: #64748b !important; font-size: 0.75rem !important; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
[data-testid="metric-container"] [data-testid="stMetricValue"] { color: #0f172a !important; font-size: 1.6rem !important; font-weight: 700; }

/* ── Buttons ── */
button[kind="primary"],
button[data-testid="baseButton-primary"] {
    background: #1d4ed8 !important;
    border: none !important;
    border-radius: 6px !important;
    font-weight: 600 !important;
    letter-spacing: 0.2px;
}
button[kind="secondary"],
button[data-testid="baseButton-secondary"] {
    border-radius: 6px !important;
    font-weight: 500 !important;
    border: 1px solid #cbd5e1 !important;
    color: #334155 !important;
}

/* ── Download buttons ── */
[data-testid="stDownloadButton"] button {
    background: #0f766e !important;
    color: #ffffff !important;
    border: none !important;
    border-radius: 6px !important;
    font-weight: 600 !important;
}

/* ── Divider ── */
hr { border: none; border-top: 1px solid #e2e8f0; margin: 1.2rem 0; }

/* ── Info / warning / error boxes ── */
[data-testid="stAlert"] { border-radius: 8px !important; }

/* ── Dataframe ── */
[data-testid="stDataFrame"] { border-radius: 8px; overflow: hidden; border: 1px solid #e2e8f0; }

/* ── Sidebar ── */
[data-testid="stSidebar"] { background: #f1f5f9; }
[data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2,
[data-testid="stSidebar"] h3 { color: #1e293b !important; }

/* ── Expander ── */
[data-testid="stExpander"] { border: 1px solid #e2e8f0 !important; border-radius: 8px !important; }
</style>
""", unsafe_allow_html=True)

st.title("Aiclex Hallticket Mailer")


# ---------------- DB helpers ----------------
def init_db():
    conn = sqlite3.connect(APP_DB, timeout=30)
    cur = conn.cursor()
    cur.execute(f"""
    CREATE TABLE IF NOT EXISTS {LOG_TABLE} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        location TEXT,
        recipients TEXT,
        halltickets TEXT,
        part TEXT,
        file TEXT,
        file_path TEXT DEFAULT '',
        files_in_part INTEGER,
        status TEXT,
        error TEXT
    )
    """)
    # Migrate existing DBs that don't have file_path column yet
    try:
        cur.execute(f"ALTER TABLE {LOG_TABLE} ADD COLUMN file_path TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.commit()
    return conn

def append_log(conn, row):
    cur = conn.cursor()
    cur.execute(f"""
      INSERT INTO {LOG_TABLE} (timestamp, location, recipients, halltickets, part, file, file_path, files_in_part, status, error)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        row.get("location",""),
        row.get("recipients",""),
        json.dumps(row.get("halltickets",[]), ensure_ascii=False),
        row.get("part",""),
        row.get("file",""),
        row.get("file_path",""),
        int(row.get("files_in_part",0)),
        row.get("status",""),
        str(row.get("error",""))
    ))
    conn.commit()

def update_log_status(conn, log_id, status, error=""):
    cur = conn.cursor()
    cur.execute(f"UPDATE {LOG_TABLE} SET status=?, error=? WHERE id=?", (status, str(error), log_id))
    conn.commit()

def fetch_stats(conn):
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {LOG_TABLE}")
    total = cur.fetchone()[0]
    cur.execute(f"SELECT COUNT(*) FROM {LOG_TABLE} WHERE status='Sent'")
    sent = cur.fetchone()[0]
    cur.execute(f"SELECT COUNT(*) FROM {LOG_TABLE} WHERE status!='Sent'")
    pending = cur.fetchone()[0]
    cur.execute(f"SELECT COUNT(*) FROM {LOG_TABLE} WHERE status='Failed'")
    failed = cur.fetchone()[0]
    return {"total": total, "sent": sent, "pending": pending, "failed": failed}

def fetch_pending_rows(conn):
    cur = conn.cursor()
    cur.execute(f"SELECT id, location, recipients, halltickets, part, file, file_path, files_in_part, status FROM {LOG_TABLE} WHERE status!='Sent' ORDER BY id")
    rows = cur.fetchall()
    res = []
    for r in rows:
        res.append({
            "id": r[0],
            "location": r[1],
            "recipients": r[2],
            "halltickets": json.loads(r[3]) if r[3] else [],
            "part": r[4],
            "file": r[5],
            "file_path": r[6] or "",
            "files_in_part": r[7],
            "status": r[8]
        })
    return res

def clear_pending(conn):
    cur = conn.cursor()
    cur.execute(f"DELETE FROM {LOG_TABLE}")
    conn.commit()

# initialize DB connection
conn = init_db()

# ---------------- Sidebar / Settings ----------------
with st.sidebar:
    st.markdown("### Settings")
    st.divider()

    st.markdown("**Email Templates**")
    subject_template = st.text_input("Subject", value="Hall Tickets — {location} (Part {part}/{total})")
    body_template = st.text_area("Body", value="Dear Coordinator,\n\nPlease find attached the hall tickets for {location}.\n\nRegards,\nAiclex Technologies", height=130)

    st.divider()
    st.markdown("**SMTP Credentials**")
    try:
        smtp_creds = dict(st.secrets.get("smtp_credentials", {}))
    except Exception:
        smtp_creds = {}   # no secrets.toml (local run) — fields stay empty, user types them in

    col_s1, col_s2 = st.columns(2)
    with col_s1:
        smtp_host = st.text_input("Host", value=smtp_creds.get("host", ""))
        sender_email = st.text_input("Email", value=smtp_creds.get("email", ""))
    with col_s2:
        smtp_port = st.text_input("Port", value=smtp_creds.get("port", "587"))
        sender_pass = st.text_input("Password", value=smtp_creds.get("password", ""), type="password")

    protocol = st.selectbox("Protocol", ["STARTTLS", "SMTPS"], index=0 if smtp_creds.get("protocol", "STARTTLS") == "STARTTLS" else 1)

    st.divider()
    st.markdown("**Send Settings**")
    delay_seconds = st.number_input("Delay between emails (sec)", value=2.0, step=0.5, min_value=0.0)
    max_mb = st.number_input("Max attachment size (MB)", value=3.0, step=0.5, min_value=0.5)

    st.divider()
    st.markdown("**Test Mode**")
    testing_mode_default = st.checkbox("Override recipients (test mode)", value=True)
    test_email_default = st.text_input("Test email address", value=smtp_creds.get("default_test_email", ""))

# ---------------- Send Log Stats — metric cards at top ----------------
stats = fetch_stats(conn)
_mc1, _mc2, _mc3, _mc4 = st.columns(4)
_mc1.metric("Total Logged", stats["total"])
_mc2.metric("Sent", stats["sent"])
_mc3.metric("Pending / Failed", stats["pending"])
_mc4.metric("Failed", stats["failed"])
st.divider()


# ---------------- Helpers for ZIP / matching ----------------
def extract_zip_recursively(zip_file_like, extract_to):
    if hasattr(zip_file_like, "read"):
        zf = zipfile.ZipFile(zip_file_like)
    else:
        zf = zipfile.ZipFile(zip_file_like, "r")
    try:
        zf.extractall(path=extract_to)
    finally:
        zf.close()
    for root, _, files in os.walk(extract_to):
        for f in files:
            if f.lower().endswith(".zip"):
                nested = os.path.join(root, f)
                nested_dir = os.path.join(root, f"_nested_{os.path.splitext(f)[0]}")
                os.makedirs(nested_dir, exist_ok=True)
                try:
                    with open(nested, "rb") as nf:
                        extract_zip_recursively(nf, nested_dir)
                except Exception:
                    continue

def human_bytes(n):
    try: n = float(n)
    except: return ""
    for unit in ['B','KB','MB','GB','TB']:
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"

def create_chunked_zips_with_counts(file_paths, out_dir, base_name, max_bytes):
    # Sanitize base_name — strip slashes and any path-unsafe characters
    # e.g. "HISAR/Jind" -> "HISAR_Jind" so it can't create phantom subdirectories
    base_name = re.sub(r'[^A-Za-z0-9_\-]', '_', base_name)

    # Ensure output directory exists (critical on Streamlit Cloud where paths may be read-only)
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError:
        # Fallback to a writable temp directory if out_dir is not writable
        out_dir = tempfile.mkdtemp(prefix="aiclex_zips_")

    # Filter to only existing, readable files to prevent FileNotFoundError
    file_paths = [fp for fp in file_paths if fp and os.path.isfile(fp)]
    if not file_paths:
        return []

    parts = []
    current_files = []
    part_index = 1

    for fp in file_paths:
        current_files.append(fp)
        test_path = os.path.join(out_dir, f"__test_{part_index}.zip")
        test_removed = False
        try:
            with zipfile.ZipFile(test_path, 'w', compression=zipfile.ZIP_DEFLATED) as z:
                for f in current_files:
                    if os.path.isfile(f):
                        z.write(f, arcname=os.path.basename(f))
            size = os.path.getsize(test_path)
        except Exception:
            # If test zip creation fails, skip this file
            current_files.pop()
            if os.path.exists(test_path):
                try:
                    os.remove(test_path)
                except OSError:
                    pass
            continue

        if size <= max_bytes:
            try:
                os.remove(test_path)
            except OSError:
                pass
            test_removed = True
            continue

        # Current batch exceeds limit — flush all but the last file into a part
        last = current_files.pop()
        part_path = os.path.join(out_dir, f"{base_name}_part{part_index}.zip")
        os.makedirs(os.path.dirname(part_path), exist_ok=True)
        try:
            with zipfile.ZipFile(part_path, 'w', compression=zipfile.ZIP_DEFLATED) as z:
                for f in current_files:
                    if os.path.isfile(f):
                        z.write(f, arcname=os.path.basename(f))
            with zipfile.ZipFile(part_path, 'r') as zc:
                names = zc.namelist()
            parts.append({"path": part_path, "files": names, "size": os.path.getsize(part_path)})
            part_index += 1
        except Exception:
            pass

        current_files = [last]
        if not test_removed:
            try:
                os.remove(test_path)
            except OSError:
                pass

    # Flush remaining files into the last part
    if current_files:
        part_path = os.path.join(out_dir, f"{base_name}_part{part_index}.zip")
        os.makedirs(os.path.dirname(part_path), exist_ok=True)
        try:
            with zipfile.ZipFile(part_path, 'w', compression=zipfile.ZIP_DEFLATED) as z:
                for f in current_files:
                    if os.path.isfile(f):
                        z.write(f, arcname=os.path.basename(f))
            with zipfile.ZipFile(part_path, 'r') as zc:
                names = zc.namelist()
            parts.append({"path": part_path, "files": names, "size": os.path.getsize(part_path)})
        except Exception:
            pass

    return parts

def hallticket_from_filename(fn: str) -> str:
    """Return the hallticket number encoded in a PDF filename.
    Prefers the last 9-digit group; falls back to the last group with >=6 digits,
    then to the last digit group of any length. Returns "" if no digits."""
    stem = os.path.splitext(fn)[0]
    all_groups = re.findall(r"\d+", stem)
    if not all_groups:
        return ""
    nine = [g for g in all_groups if len(g) == 9]
    if nine:
        return nine[-1]
    six = [g for g in all_groups if len(g) >= 6]
    if six:
        return six[-1]
    return all_groups[-1]

def build_pdf_index(pdf_map: dict) -> dict:
    """hallticket digits -> sorted list of PDF filenames. Built once per ZIP,
    so every Excel row lookup is O(1) instead of scanning all PDFs."""
    idx = defaultdict(list)
    for fn in pdf_map:
        ht = hallticket_from_filename(fn)
        if ht:
            idx[ht].append(fn)
    return {k: sorted(set(v)) for k, v in idx.items()}

def matched_pdfs_for_hall(hall: str, pdf_index: dict) -> list:
    """Filenames whose extracted hallticket exactly equals the Excel hallticket digits."""
    if not hall or hall in ("", "nan", "SR NO", "HALLTICKET", "HALL TICKET", "HT NO"):
        return []
    return pdf_index.get(re.sub(r"\D", "", hall), [])

@st.cache_data(show_spinner=False)
def load_table(file_bytes: bytes, file_name: str) -> pd.DataFrame:
    """Read the uploaded Excel/CSV once per file content; reruns hit the cache."""
    bio = io.BytesIO(file_bytes)
    if file_name.lower().endswith(".csv"):
        df = pd.read_csv(bio, dtype=str).fillna("")
    else:
        df = pd.read_excel(bio, dtype=str).fillna("")
    # Deduplicate column names (e.g. 'Hallticket No', 'Hallticket No' -> 'Hallticket No', 'Hallticket No_1')
    seen = {}
    new_cols = []
    for col in df.columns:
        if col in seen:
            seen[col] += 1
            new_cols.append(f"{col}_{seen[col]}")
        else:
            seen[col] = 0
            new_cols.append(col)
    df.columns = new_cols
    return df

def make_download_zip(paths, out_path):
    with zipfile.ZipFile(out_path, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        for p in paths:
            if os.path.exists(p):
                z.write(p, arcname=os.path.basename(p))
    return out_path

@st.cache_data(show_spinner=False)
def extract_exam_password(pdf_path: str) -> str:
    """Extract the exam password from a PDF file.
    Looks for a line/token labelled 'exam password', 'password', etc.
    and returns the value that follows it on the same or next line.
    Returns empty string if not found or on any error.
    """
    if PyPDF2 is None:
        return ""
    try:
        with open(pdf_path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            full_text = ""
            for page in reader.pages:
                try:
                    full_text += (page.extract_text() or "") + "\n"
                except Exception:
                    continue
        # Normalize whitespace for easier matching
        # Pattern: after label like "Exam Password", "Password", "EXAM PASSWORD"
        # the value follows — on same line after colon/space or on very next line
        pattern = re.compile(
            r'(?:exam\s*password|password)\s*[:\-]?\s*([A-Za-z0-9@#$!%^&*_\-\.]+)',
            re.IGNORECASE
        )
        m = pattern.search(full_text)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return ""



# ---------------- Session state defaults ----------------
if "workdir" not in st.session_state: st.session_state.workdir = None
if "pdf_map" not in st.session_state: st.session_state.pdf_map = {}
if "grouped" not in st.session_state: st.session_state.grouped = {}
if "prepared" not in st.session_state: st.session_state.prepared = {}
if "summary_rows" not in st.session_state: st.session_state.summary_rows = []
if "cancel_requested" not in st.session_state: st.session_state.cancel_requested = False
if "skip_delay" not in st.session_state: st.session_state.skip_delay = False
if "verified" not in st.session_state: st.session_state.verified = False
# Cache keys for speed optimisation
if "zip_extracted_for" not in st.session_state: st.session_state.zip_extracted_for = None
if "mapping_cache_key" not in st.session_state: st.session_state.mapping_cache_key = None
if "mapping_rows" not in st.session_state: st.session_state.mapping_rows = []
if "excel_halls" not in st.session_state: st.session_state.excel_halls = []
if "pdf_index" not in st.session_state: st.session_state.pdf_index = {}
if "reverse_rows" not in st.session_state: st.session_state.reverse_rows = []
if "group_cache_key" not in st.session_state: st.session_state.group_cache_key = None
if "group_summary_rows" not in st.session_state: st.session_state.group_summary_rows = []

status_ph = st.empty()

# ---------------- Upload ----------------
st.subheader("Step 1 — Upload Files")
client_mode = st.selectbox(
    "Client Mode",
    ["TVS", "Other"],
    index=0,
    key="client_mode",
    help="TVS: one email per hallticket (row emails = manager + student). "
         "Other: all halltickets of a location with the same email list go together in one ZIP, split into parts under the size limit.",
)
if client_mode == "TVS":
    st.caption("TVS mode — each hallticket is zipped and emailed separately to the emails on its row.")
else:
    st.caption("Other mode — halltickets are grouped by Location + Emails, zipped together, chunked under the max attachment size, and each part is sent to the common emails.")

col1, col2 = st.columns(2)
with col1:
    uploaded_excel = st.file_uploader("Excel file (.xlsx or .csv)  —  must have Hallticket, Email, Location columns", type=["xlsx","csv"], key="upl_excel")
with col2:
    uploaded_zip = st.file_uploader("ZIP file  —  contains all PDF hall tickets (nested ZIPs supported)", type=["zip"], key="upl_zip")

if not (uploaded_excel and uploaded_zip):
    st.info("Upload both files above to begin.")
    st.stop()

# ---------------- Read Excel ----------------
try:
    df = load_table(uploaded_excel.getvalue(), uploaded_excel.name)
except Exception as e:
    st.error("Failed to read Excel: " + str(e))
    st.stop()

st.divider()
cols = list(df.columns)
st.subheader("Step 2 — Map Columns")
_cm1, _cm2, _cm3 = st.columns(3)
with _cm1:
    ht_col = st.selectbox("Hallticket column", cols, index=0)
with _cm2:
    email_col = st.selectbox("Email column", cols, index=1 if len(cols)>1 else 0)
with _cm3:
    loc_col = st.selectbox("Location column", cols, index=2 if len(cols)>2 else 0)

st.caption("Data preview — first 8 rows")
st.dataframe(df[[ht_col, email_col, loc_col]].head(8), use_container_width=True)


# ---------------- Extract ZIP — only once per uploaded ZIP ----------------
_zip_key = getattr(uploaded_zip, "file_id", uploaded_zip.name)
if st.session_state.get("zip_extracted_for") != _zip_key:
    # New ZIP uploaded — reset workdir and extract fresh
    if st.session_state.workdir and os.path.exists(st.session_state.workdir):
        try:
            shutil.rmtree(st.session_state.workdir)
        except Exception:
            pass
    st.session_state.workdir = tempfile.mkdtemp(prefix="aiclex_zip_")
    st.session_state.pdf_map = {}
    st.session_state.mapping_cache_key = None  # invalidate mapping cache too
    workdir = st.session_state.workdir
    status_ph.info("Extracting uploaded ZIP into workspace...")
    try:
        bio = io.BytesIO(uploaded_zip.read())
        extract_zip_recursively(bio, workdir)
    except Exception as e:
        st.error("ZIP extraction failed: " + str(e))
        st.stop()
    # Scan PDFs once and store in session_state
    pdf_map = {}
    for root, _, files in os.walk(workdir):
        for f in files:
            if f.lower().endswith(".pdf"):
                pdf_map[f] = os.path.join(root, f)
    st.session_state.pdf_map = pdf_map
    st.session_state.pdf_index = build_pdf_index(pdf_map)
    st.session_state.zip_extracted_for = _zip_key
    status_ph.success(f"Extracted {len(pdf_map)} PDFs into workspace.")
else:
    # Same ZIP — reuse already-extracted files instantly
    workdir = st.session_state.workdir
    pdf_map = st.session_state.pdf_map
    if not st.session_state.get("pdf_index") and pdf_map:
        st.session_state.pdf_index = build_pdf_index(pdf_map)
    status_ph.success(f"Using cached workspace — {len(pdf_map)} PDFs ready.")
pdf_index = st.session_state.pdf_index

# ---------------- Mapping Excel → PDF — cached per column selection ----------------
_mapping_key = f"{getattr(uploaded_excel,'file_id',uploaded_excel.name)}|{_zip_key}|{ht_col}|{email_col}|{loc_col}"
if st.session_state.get("mapping_cache_key") == _mapping_key:
    # Column selection unchanged — reuse cached result instantly
    mapping_rows  = st.session_state.mapping_rows
    excel_halls   = st.session_state.excel_halls
    status_ph.success(f"Using cached mapping — {len(mapping_rows)} rows.")
else:
    # Recompute mapping (columns changed or first run)
    status_ph.info("Computing mapping...")
    mapping_rows = []
    excel_halls = []
    for idx, row in df.iterrows():
        hall = str(row[ht_col]).strip() if ht_col in row.index else str(row.iloc[0]).strip()
        raw_emails = str(row[email_col]).strip() if email_col in row.index else str(row.iloc[1]).strip()
        location = str(row[loc_col]).strip() if loc_col in row.index else str(row.iloc[2]).strip()
        excel_halls.append(hall)
        # Strict match via prebuilt index: filename hallticket == Excel hallticket digits
        matched_files = matched_pdfs_for_hall(hall, pdf_index)
        # Extract exam password from the first matched PDF (cached by @st.cache_data)
        password = ""
        for fn in matched_files:
            pdf_path = pdf_map.get(fn, "")
            if pdf_path and os.path.isfile(pdf_path):
                password = extract_exam_password(pdf_path)
                if password:
                    break
        mapping_rows.append({
            "Hallticket": hall,
            "Emails": raw_emails,
            "Location": location,
            "MatchedCount": len(matched_files),
            "MatchedFiles": "; ".join(matched_files),
            "Password": password
        })
    # Save to session_state for next rerun
    st.session_state.mapping_rows = mapping_rows
    st.session_state.excel_halls  = excel_halls
    st.session_state.mapping_cache_key = _mapping_key
    status_ph.success(f"Mapping complete — {len(mapping_rows)} rows.")

map_df = pd.DataFrame(mapping_rows)

st.divider()
st.subheader("Step 3 — Mapping Results")

# Stats bar
_matched_count = int((map_df["MatchedCount"] > 0).sum())
_unmatched_count = int((map_df["MatchedCount"] == 0).sum())
_s1, _s2, _s3 = st.columns(3)
_s1.metric("Total Rows", len(map_df))
_s2.metric("Matched", _matched_count)
_s3.metric("Unmatched", _unmatched_count)

# Download buttons in a row
_d1, _d2, _d3 = st.columns(3)
with _d1:
    st.download_button("Download mapping_check.csv", data=map_df.to_csv(index=False), file_name="mapping_check.csv", mime="text/csv", key="dl_map_check")
with _d2:
    missing_df = map_df[map_df["MatchedCount"] == 0][["Hallticket","Emails","Location"]]
    st.download_button("Download missing_in_zip.csv", data=missing_df.to_csv(index=False), file_name="missing_in_zip.csv", mime="text/csv", key="dl_missing")

st.dataframe(map_df, use_container_width=True)

# Reverse mapping — collapsed by default
with st.expander("Reverse mapping  (PDF → Excel detect)", expanded=False):
    if st.session_state.get("reverse_cache_key") == _mapping_key:
        pdf_reverse_rows = st.session_state.reverse_rows
    else:
        pdf_reverse_rows = []
        excel_set = set([str(x).strip().lower() for x in excel_halls if str(x).strip() != ""])
        for fn, p in pdf_map.items():
            fn_low = fn.lower()
            digits = re.findall(r"\d{4,20}", fn_low)
            matched_hall = ""
            for d in digits:
                if d in excel_set:
                    matched_hall = d
                    break
            if not matched_hall and digits:
                last = digits[-1]
                if last in excel_set:
                    matched_hall = last
            pdf_reverse_rows.append({"PDFFile": fn, "DetectedHallticket": matched_hall or "", "MatchedInExcel": bool(matched_hall)})
        st.session_state.reverse_rows = pdf_reverse_rows
        st.session_state.reverse_cache_key = _mapping_key
    pdf_rev_df = pd.DataFrame(pdf_reverse_rows)
    extra_csv = pdf_rev_df[pdf_rev_df["MatchedInExcel"]==False].to_csv(index=False)
    st.caption(f"{len(pdf_rev_df)} total PDFs — {int(pdf_rev_df['MatchedInExcel'].sum())} matched, {int((~pdf_rev_df['MatchedInExcel']).sum())} extra")
    st.download_button("Download extra_in_zip.csv", data=extra_csv, file_name="extra_in_zip.csv", mime="text/csv", key="dl_extra")
    st.dataframe(pdf_rev_df, use_container_width=True)

st.divider()

# ---------------- Grouping — mode-aware, cached per (mapping, mode) ----------------
# TVS   : key = (location, recip_key, hall)  -> every hallticket isolated in its own ZIP/email
# Other : key = (location, recip_key, "")    -> all halltickets of a location sharing the same
#                                               email list go into one ZIP (chunked into parts)
_group_key = f"{_mapping_key}|{client_mode}"
if st.session_state.get("group_cache_key") == _group_key and st.session_state.get("grouped"):
    grouped = st.session_state.grouped
    summary_rows_grp = st.session_state.group_summary_rows
else:
    grouped = defaultdict(list)
    seen_in_group = set()
    for m in mapping_rows:
        hall = m["Hallticket"]
        if not hall or hall in ("nan", "SR NO", "HALLTICKET", "HALL TICKET", "HT NO"):
            continue
        emails = [e.strip().lower() for e in re.split(r"[,;\n]+", m["Emails"]) if e.strip()]
        recip_key = tuple(sorted(emails))
        location = m["Location"]
        gkey = (location, recip_key, hall) if client_mode == "TVS" else (location, recip_key, "")
        if (gkey, hall) in seen_in_group:
            continue  # duplicate Excel row for the same hallticket — don't attach the PDF twice
        seen_in_group.add((gkey, hall))
        grouped[gkey].append(hall)
    grouped = dict(grouped)
    summary_rows_grp = []
    for (loc, recip_key, _h), halls in grouped.items():
        matched_count = sum(len(matched_pdfs_for_hall(h, pdf_index)) for h in halls)
        summary_rows_grp.append({
            "Location": loc,
            "Halltickets": ", ".join(halls),
            "HallticketCount": len(halls),
            "Recipients": ", ".join(recip_key),
            "MatchedPDFs": matched_count,
        })
    st.session_state.grouped = grouped
    st.session_state.group_summary_rows = summary_rows_grp
    st.session_state.group_cache_key = _group_key

# Group summary — collapsed by default
with st.expander(f"Group summary  ({client_mode} mode — {len(grouped)} groups)", expanded=False):
    st.dataframe(pd.DataFrame(summary_rows_grp), use_container_width=True)

st.divider()

# Verification gate
st.subheader("Step 4 — Verify Before Sending")
st.caption("Review the mapping results above. Check the box below to confirm accuracy and unlock Prepare & Send.")
st.session_state.verified = st.checkbox("I have reviewed the mapping and confirm it is accurate", value=False, key="verify_final")
if not st.session_state.verified:
    st.warning("Prepare & Send is locked until you confirm the mapping above.")
    st.stop()


# ---------------- Prepare ZIPs ----------------
st.subheader("Step 5 — Prepare ZIP Parts")
st.caption(f"Max attachment size: {max_mb} MB per part. Parts will be created automatically if total size exceeds limit.")

prep_col1, prep_col2 = st.columns([3, 1])
with prep_col1:
    if st.button("Prepare ZIPs", type="primary", use_container_width=True):
        st.session_state.cancel_requested = False
        status_ph.info("Preparing ZIP parts...")
        max_bytes = int(max_mb * 1024 * 1024)
        outroot = tempfile.mkdtemp(prefix="aiclex_out_")
        prepared = {}
        summary_rows = []
        groups = list(grouped.items())
        total = max(1, len(groups))
        prog = st.progress(0)
        used_names = {}
        for i, ((loc, recip_key, _h), halls) in enumerate(groups, start=1):
            if st.session_state.cancel_requested:
                status_ph.warning("Preparation cancelled.")
                break
            # Strict match via index: every hallticket in this group -> its PDF(s)
            matched_paths = []
            for h in halls:
                for fn in matched_pdfs_for_hall(h, pdf_index):
                    matched_paths.append(pdf_map[fn])
            matched_paths = sorted(set(matched_paths))

            recip_str = ", ".join(recip_key)
            halls_key = tuple(halls)
            if not matched_paths:
                prepared[(loc, recip_str, halls_key)] = []
                prog.progress(int(i/total*100))
                continue
            safe_loc = re.sub(r'[^A-Za-z0-9]', '_', loc)[:30]
            if client_mode == "TVS":
                safe_hall = re.sub(r'[^A-Za-z0-9]', '_', halls[0])[:20]
                base_name = f"{safe_loc}_{safe_hall}"   # e.g. Pune_803038629
            else:
                base_name = safe_loc                     # e.g. Mumbai  -> Mumbai_part1.zip
            # Same location with a different email list -> keep ZIP names unique
            if base_name in used_names:
                used_names[base_name] += 1
                base_name = f"{base_name}_g{used_names[base_name]}"
            else:
                used_names[base_name] = 1
            out_dir = os.path.join(outroot, base_name)
            os.makedirs(out_dir, exist_ok=True)
            parts = create_chunked_zips_with_counts(matched_paths, out_dir, base_name=base_name, max_bytes=max_bytes)
            prepared[(loc, recip_str, halls_key)] = parts
            total_files_in_group = sum(len(pinfo["files"]) for pinfo in parts)
            for idx_part, pinfo in enumerate(parts, start=1):
                # Halltickets actually inside this part (by filename)
                part_halls = sorted({hallticket_from_filename(n) for n in pinfo["files"]} & set(re.sub(r"\D", "", h) for h in halls))
                summary_rows.append({
                    "Location": loc,
                    "Halltickets": ", ".join(part_halls),
                    "HallticketCount": len(part_halls),
                    "Recipients": recip_str,
                    "Part": f"{idx_part}/{len(parts)}",
                    "File": os.path.basename(pinfo["path"]),
                    "Size": human_bytes(pinfo["size"]),
                    "FilesInPart": len(pinfo["files"]),
                    "TotalFilesInGroup": total_files_in_group,
                    "Path": pinfo["path"]
                })
            prog.progress(int(i/total*100))
        st.session_state.prepared = prepared
        st.session_state.summary_rows = summary_rows
        status_ph.success("ZIP parts created — ready to send.")
with prep_col2:
    if st.button("Cancel", use_container_width=True):
        st.session_state.cancel_requested = True
        status_ph.warning("Cancelled.")

# Prepared parts preview
if st.session_state.get("summary_rows"):
    st.divider()
    st.subheader("Step 6 — Prepared Parts")
    prep_df = pd.DataFrame(st.session_state["summary_rows"])
    _total_parts = len(prep_df)
    _total_size = sum(r.get("size", 0) if isinstance(r.get("size"), (int, float)) else 0 for r in st.session_state["summary_rows"])

    _p1, _p2, _p3 = st.columns(3)
    _p1.metric("Total Parts", _total_parts)
    _p2.metric("Locations", prep_df["Location"].nunique())
    _p3.metric("Total Files", int(prep_df["FilesInPart"].sum()))

    st.download_button("Download prepared_summary.csv", data=prep_df.to_csv(index=False), file_name="prepared_summary.csv", mime="text/csv", key="dl_prep")
    st.dataframe(prep_df[["Location","Halltickets","HallticketCount","Recipients","Part","File","Size","FilesInPart","TotalFilesInGroup"]], use_container_width=True)

    # Select and download individual part
    opts = [f"{r['Location']}  —  {r['File']}  ({r['Part']})  [{r['FilesInPart']} files]" for r in st.session_state["summary_rows"]]
    sel = st.selectbox("Select a part to download", opts, index=0, key="sel_part_ui")
    sel_idx = opts.index(sel)
    sel_row = st.session_state["summary_rows"][sel_idx]
    try:
        with open(sel_row["Path"], "rb") as f:
            st.download_button(label="Download selected part", data=f.read(), file_name=sel_row["File"], key=f"dl_sel_{sel_idx}")
    except Exception as e:
        st.warning(f"Cannot open selected part: {e}")

    # Download all combined
    all_paths = [r["Path"] for r in st.session_state["summary_rows"] if os.path.exists(r["Path"])]
    if all_paths:
        if st.button("Download all parts as single ZIP", use_container_width=False):
            tmp_all = os.path.join(tempfile.gettempdir(), f"aiclex_all_parts_{int(time.time())}.zip")
            try:
                make_download_zip(all_paths, tmp_all)
                with open(tmp_all, "rb") as af:
                    st.download_button(label="Download combined ZIP", data=af.read(), file_name=os.path.basename(tmp_all), key=f"dl_all_{int(time.time())}")
            except Exception as e:
                st.error("Failed to create combined download: " + str(e))

# ---------------- Test & Bulk send with DB logging ----------------
st.divider()
st.subheader("Step 7 — Send")

col_test, col_opts, col_send = st.columns([1,1,1])
with col_test:
    test_email = st.text_input("Test email address", value=test_email_default, key="test_email_input")
    if st.button("Send Test Email", use_container_width=True):
        if not st.session_state.get("prepared"):
            st.error("No prepared parts — run Prepare ZIPs first.")

        else:
            status_ph.info("Sending test email (first prepared part)...")
            sent = False
            try:
                if protocol.startswith("SMTPS"):
                    server = smtplib.SMTP_SSL(smtp_host, int(smtp_port), timeout=60)
                else:
                    server = smtplib.SMTP(smtp_host, int(smtp_port), timeout=60)
                    server.starttls()
                server.login(sender_email, sender_pass)
                for (loc, recip_str, halls_key), parts in st.session_state.prepared.items():
                    if not parts:
                        continue
                    first = parts[0]["path"]
                    msg = EmailMessage()
                    msg["From"] = sender_email
                    msg["To"] = test_email
                    try:
                        subj = subject_template.format(location=loc, part=1, total=len(parts))
                    except:
                        subj = f"{loc} part 1/{len(parts)}"
                    msg["Subject"] = "[TEST] " + subj
                    try:
                        body_txt = body_template.format(location=loc, part=1, total=len(parts))
                    except:
                        body_txt = f"Test: attached {os.path.basename(first)}"
                    msg.set_content(body_txt + "\n\n(This is a TEST email — only first part attached.)")
                    with open(first, "rb") as af:
                        msg.add_attachment(af.read(), maintype="application", subtype="zip", filename=os.path.basename(first))
                    server.send_message(msg)
                    # log test send as Sent in DB (so resume won't re-send)
                    append_log(conn, {"location": loc, "recipients": test_email, "halltickets": list(halls_key), "part": "1/1", "file": os.path.basename(first), "file_path": first, "files_in_part": len(parts[0]["files"]) if parts else 0, "status": "Sent", "error": ""})
                    sent = True
                    status_ph.success(f"Test email sent to {test_email} with {os.path.basename(first)}")
                    break
                try:
                    server.quit()
                except:
                    pass
                if not sent:
                    st.warning("No parts available to test send.")
            except Exception as e:
                st.error("Test send failed: " + str(e))

with col_opts:
    skip_delay_chk = st.checkbox("Skip delay between sends", value=False, key="skip_delay_send")
    if st.button("Cancel Operation", use_container_width=True):
        st.session_state.cancel_requested = True
        status_ph.warning("Cancel requested — will stop shortly.")

    # Resume pending sends (DB-based)
    if st.button("Resume Pending Sends", use_container_width=True):

        pending = fetch_pending_rows(conn)
        if not pending:
            st.info("No pending entries to resume.")
        else:
            status_ph.info(f"Resuming {len(pending)} pending sends...")
            prog = st.progress(0)
            total_pending = len(pending)
            sent_count = 0
            try:
                if protocol.startswith("SMTPS"):
                    server = smtplib.SMTP_SSL(smtp_host, int(smtp_port), timeout=60)
                else:
                    server = smtplib.SMTP(smtp_host, int(smtp_port), timeout=60)
                    server.starttls()
                server.login(sender_email, sender_pass)
                RECONNECT_EVERY = 100
                rc = 0
                for i, item in enumerate(pending, start=1):
                    if st.session_state.cancel_requested:
                        status_ph.warning("Resume cancelled by user.")
                        break
                    # build email
                    msg = EmailMessage()
                    msg["From"] = sender_email
                    # recipients stored as comma-separated; allow override if testing default on
                    target_to = test_email if testing_mode_default else item["recipients"]
                    msg["To"] = target_to
                    try:
                        msg["Subject"] = subject_template.format(location=item["location"], part=item["part"].split("/")[0], total=item["part"].split("/")[-1])
                    except:
                        msg["Subject"] = f"{item['location']} {item['part']}"
                    msg.set_content(f"Resuming send for {item['location']} — part {item['part']}")
                    # locate file path: DB file_path is primary (crash-safe), session_state is fallback
                    fname = item["file"]
                    ppath = item.get("file_path", "")  # from DB — works after crash/restart
                    if not ppath or not os.path.exists(ppath):
                        # Fallback: search session_state summary_rows (same-session only)
                        for r in st.session_state.get("summary_rows", []):
                            if r["File"] == fname:
                                ppath = r["Path"]
                                break
                    if not ppath or not os.path.exists(ppath):
                        append_log(conn, {"location": item["location"], "recipients": item["recipients"], "halltickets": item.get("halltickets",[]), "part": item["part"], "file": fname, "file_path": "", "files_in_part": item.get("files_in_part",0), "status": "Failed", "error": "Prepared file missing on server"})
                        continue
                    with open(ppath, "rb") as af:
                        msg.add_attachment(af.read(), maintype="application", subtype="zip", filename=os.path.basename(ppath))
                    try:
                        server.send_message(msg)
                        append_log(conn, {"location": item["location"], "recipients": target_to, "halltickets": item.get("halltickets",[]), "part": item["part"], "file": fname, "files_in_part": item.get("files_in_part",0), "status": "Sent", "error": ""})
                    except Exception as e:
                        append_log(conn, {"location": item["location"], "recipients": target_to, "halltickets": item.get("halltickets",[]), "part": item["part"], "file": fname, "files_in_part": item.get("files_in_part",0), "status": "Failed", "error": str(e)})
                    sent_count += 1
                    rc += 1
                    prog.progress(int(i/total_pending*100))
                    if rc >= RECONNECT_EVERY:
                        try: server.quit()
                        except: pass
                        if protocol.startswith("SMTPS"):
                            server = smtplib.SMTP_SSL(smtp_host, int(smtp_port), timeout=60)
                        else:
                            server = smtplib.SMTP(smtp_host, int(smtp_port), timeout=60)
                            server.starttls()
                        server.login(sender_email, sender_pass)
                        rc = 0
                    if not skip_delay_chk:
                        time.sleep(float(delay_seconds))
                try: server.quit()
                except: pass
                status_ph.success("Resume finished (see DB logs).")
            except Exception as e:
                st.error("Resume failed: " + str(e))

with col_send:
    if st.button("Send All Prepared Parts", type="primary", use_container_width=True):
        if not st.session_state.get("prepared"):
            st.error("No prepared parts — run Prepare ZIPs first.")

        else:
            st.session_state.cancel_requested = False
            total_parts = sum(len(parts) for parts in st.session_state.prepared.values())
            if total_parts == 0:
                st.warning("No parts to send.")
            else:
                status_ph.info("Starting bulk send...")
                sent_count = 0
                failed_count = 0
                logs = []

                # ── Live progress UI ──────────────────────────────────────
                prog        = st.progress(0)
                pct_ph      = st.empty()   # "47% — 47 of 100 sent"
                m1, m2, m3  = st.columns(3)
                sent_ph     = m1.empty()
                remain_ph   = m2.empty()
                failed_ph   = m3.empty()
                feed_ph     = st.empty()   # live scrolling table

                def _refresh_ui(sent, failed, total, log_rows):
                    pct = int(sent / total * 100)
                    prog.progress(pct)
                    pct_ph.markdown(
                        f"<div style='font-size:1.1rem;font-weight:700;color:#1d4ed8'>"
                        f"{pct}%  —  {sent} of {total} sent</div>",
                        unsafe_allow_html=True
                    )
                    sent_ph.metric("Sent", sent)
                    remain_ph.metric("Remaining", total - sent)
                    failed_ph.metric("Failed", failed)
                    if log_rows:
                        feed_ph.dataframe(
                            pd.DataFrame(log_rows[::-1]),   # newest on top
                            use_container_width=True,
                            hide_index=True
                        )

                # initial state
                _refresh_ui(0, 0, total_parts, [])

                try:
                    if protocol.startswith("SMTPS"):
                        server = smtplib.SMTP_SSL(smtp_host, int(smtp_port), timeout=60)
                    else:
                        server = smtplib.SMTP(smtp_host, int(smtp_port), timeout=60)
                        server.starttls()
                    server.login(sender_email, sender_pass)
                    RECONNECT_EVERY = 100
                    rc = 0
                    for (loc, recip_str, halls_key), parts in st.session_state.prepared.items():
                        hall = ", ".join(halls_key)          # display label for the log feed
                        hall_list = list(halls_key)          # stored in DB for audit/resume
                        if st.session_state.cancel_requested:
                            status_ph.warning("Bulk send cancelled.")
                            break
                        if not parts:
                            logs.append({"Location": loc, "Halltickets": hall, "To": recip_str, "Part": "", "File": "", "Status": "No parts"})
                            continue
                        for idx_part, pinfo in enumerate(parts, start=1):
                            if st.session_state.cancel_requested:
                                break
                            target_to = test_email if testing_mode_default else recip_str
                            msg = EmailMessage()
                            msg["From"] = sender_email
                            msg["To"]   = target_to
                            try:
                                subject_line = subject_template.format(location=loc, part=idx_part, total=len(parts))
                            except:
                                subject_line = f"{loc} part {idx_part}/{len(parts)}"
                            msg["Subject"] = subject_line
                            try:
                                body_txt = body_template.format(location=loc, part=idx_part, total=len(parts))
                            except:
                                body_txt = f"Please find attached part {idx_part} for {loc}."
                            msg.set_content(body_txt)
                            with open(pinfo["path"], "rb") as af:
                                msg.add_attachment(af.read(), maintype="application", subtype="zip", filename=os.path.basename(pinfo["path"]))
                            append_log(conn, {"location": loc, "recipients": recip_str, "halltickets": hall_list, "part": f"{idx_part}/{len(parts)}", "file": os.path.basename(pinfo["path"]), "file_path": pinfo["path"], "files_in_part": len(pinfo["files"]), "status": "Pending", "error": ""})
                            try:
                                server.send_message(msg)
                                append_log(conn, {"location": loc, "recipients": target_to, "halltickets": hall_list, "part": f"{idx_part}/{len(parts)}", "file": os.path.basename(pinfo["path"]), "file_path": pinfo["path"], "files_in_part": len(pinfo["files"]), "status": "Sent", "error": ""})
                                logs.append({"Location": loc, "Halltickets": hall, "To": target_to, "Part": f"{idx_part}/{len(parts)}", "File": os.path.basename(pinfo["path"]), "Status": "Sent"})
                            except Exception as e:
                                failed_count += 1
                                append_log(conn, {"location": loc, "recipients": target_to, "halltickets": hall_list, "part": f"{idx_part}/{len(parts)}", "file": os.path.basename(pinfo["path"]), "file_path": pinfo["path"], "files_in_part": len(pinfo["files"]), "status": "Failed", "error": str(e)})
                                logs.append({"Location": loc, "Halltickets": hall, "To": target_to, "Part": f"{idx_part}/{len(parts)}", "File": os.path.basename(pinfo["path"]), "Status": f"Failed: {e}"})
                            sent_count += 1
                            rc += 1
                            _refresh_ui(sent_count, failed_count, total_parts, logs)
                            if rc >= RECONNECT_EVERY:
                                try: server.quit()
                                except: pass
                                if protocol.startswith("SMTPS"):
                                    server = smtplib.SMTP_SSL(smtp_host, int(smtp_port), timeout=60)
                                else:
                                    server = smtplib.SMTP(smtp_host, int(smtp_port), timeout=60)
                                    server.starttls()
                                server.login(sender_email, sender_pass)
                                rc = 0
                            if not skip_delay_chk:
                                time.sleep(float(delay_seconds))
                    try: server.quit()
                    except: pass
                    status_ph.success(f"Done — {sent_count} sent, {failed_count} failed out of {total_parts} total.")
                except Exception as e:
                    st.error("Bulk send failed: " + str(e))


# ---------------- Send Log ----------------
st.divider()
st.subheader("Send Log  —  Audit & Resume")
st.caption("Inspect, export or reset the persistent send log. Use Resume to re-attempt any pending sends after a crash.")
col_a, col_b, col_c = st.columns(3)
with col_a:
    if st.button("Show Send Log", use_container_width=True):
        cur = conn.cursor()
        cur.execute(f"SELECT id, timestamp, location, recipients, part, file, files_in_part, status, error FROM {LOG_TABLE} ORDER BY id DESC LIMIT 200")
        rows = cur.fetchall()
        df_logs = pd.DataFrame(rows, columns=["id","timestamp","location","recipients","part","file","files_in_part","status","error"])
        st.dataframe(df_logs, use_container_width=True)
with col_b:
    if st.button("Prepare Log Download", use_container_width=True):
        cur = conn.cursor()
        cur.execute(f"SELECT id, timestamp, location, recipients, part, file, files_in_part, status, error FROM {LOG_TABLE} ORDER BY id")
        rows = cur.fetchall()
        df_logs = pd.DataFrame(rows, columns=["id","timestamp","location","recipients","part","file","files_in_part","status","error"])
        st.download_button("Download send_log.csv", data=df_logs.to_csv(index=False), file_name="send_log.csv", mime="text/csv", key="dl_sendlog")
with col_c:
    if st.button("Clear Send Log (New Batch)", use_container_width=True):
        clear_pending(conn)
        st.success("Send log cleared. Ready for a new batch.")

# ---------------- Cleanup workspace ----------------
st.divider()
if st.button("Clear workspace  (delete extracted and prepared files)"):
    try:
        wd = st.session_state.get("workdir")
        if wd and os.path.exists(wd):
            shutil.rmtree(wd)
        for key, parts in st.session_state.get("prepared", {}).items():
            for p in parts:
                try:
                    parent = os.path.dirname(p["path"]) if isinstance(p, dict) else os.path.dirname(p)
                    if parent and os.path.exists(parent):
                        shutil.rmtree(parent)
                except:
                    pass
        st.session_state.workdir = None
        st.session_state.pdf_map = {}
        st.session_state.pdf_index = {}
        st.session_state.grouped = {}
        st.session_state.prepared = {}
        st.session_state.summary_rows = []
        st.session_state.cancel_requested = False
        st.session_state.verified = False
        st.session_state.zip_extracted_for = None
        st.session_state.mapping_cache_key = None
        st.session_state.group_cache_key = None
        st.session_state.reverse_cache_key = None
        status_ph.info("Workspace cleared. Upload new files to start again.")
    except Exception as e:
        st.error("Cleanup failed: " + str(e))

