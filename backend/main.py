from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
import zipfile, tempfile, os, re
import pdfplumber
import fitz
import pytesseract
from PIL import Image
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
from rapidfuzz import fuzz
from fastapi.responses import FileResponse
import pandas as pd

app = FastAPI(title="Flight Verifier API")
latest_results = []

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def home():
    return {"message": "Flight Verifier Backend Running Successfully"}


def clean_name(name):
    name = name.upper()
    name = re.sub(r"\b(MR|MS|MRS|MISS|MASTER|DR)\b", "", name)
    name = re.sub(r"[^A-Z ]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def extract_text_from_pdf(path):
    text = ""
    try:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                text += page.extract_text() or ""
                text += "\n"
    except:
        pass
    return text


def ocr_pdf_or_image(path):
    text = ""
    ext = path.lower().split(".")[-1]

    try:
        if ext in ["jpg", "jpeg", "png"]:
            img = Image.open(path)
            text = pytesseract.image_to_string(img)

        elif ext == "pdf":
            doc = fitz.open(path)
            for page in doc:
                pix = page.get_pixmap(dpi=400)
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                text += pytesseract.image_to_string(img)
                text += "\n"

    except Exception as e:
        print("OCR ERROR:", e)
        text = ""

    return text

def extract_ticket_names(text):
    names = []
    lines = text.splitlines()

    bad_words = [
        "GENERATION", "BOOKING", "STATUS", "TIME", "PNR", "TICKET",
        "FLIGHT", "AIRLINE", "SEAT", "MEAL", "BAGGAGE", "MOBILE",
        "FARE", "PROMO", "CONTACT", "CUSTOMER", "CARE", "EMAIL",
        "ADDRESS", "PHONE", "DEPARTS", "ARRIVES", "TERMINAL"
    ]

    for line in lines:
        original_line = line.strip()
        upper_line = original_line.upper()

        if "CONFIRMED" not in upper_line:
            continue

        if any(word in upper_line for word in bad_words):
            continue

        before_confirmed = upper_line.split("CONFIRMED")[0]

        before_confirmed = re.sub(r"^\d+\.\s*", "", before_confirmed)
        before_confirmed = clean_name(before_confirmed)

        words = before_confirmed.split()

        if 2 <= len(words) <= 5:
            names.append(before_confirmed)

    return list(set(names))

def extract_aadhaar_name(text):
    raw_lines = [x.strip() for x in text.splitlines() if x.strip()]
    lines = [clean_name(x) for x in raw_lines if clean_name(x)]

    bad_words = [
        "GOVERNMENT", "INDIA", "AADHAAR", "UNIQUE", "IDENTIFICATION",
        "AUTHORITY", "ENROLMENT", "ENROLLMENT", "ADDRESS", "VID",
        "DOB", "YEAR", "BIRTH", "MALE", "FEMALE", "MOBILE", "PIN",
        "STATE", "DISTRICT", "SUB", "VTC", "YOUR", "NO", "QR",
        "DATE", "ISSUE"
    ]

    possible_names = []

    for i, line in enumerate(lines):
        words = line.split()

        if any(word in line for word in bad_words):
            continue

        if not (2 <= len(words) <= 4):
            continue

        if any(char.isdigit() for char in line):
            continue

        score = 0

        if i + 1 < len(lines):
            next_line = lines[i + 1]
            if any(x in next_line for x in ["DOB", "YEAR", "BIRTH", "MALE", "FEMALE"]):
                score += 50

        if i + 2 < len(lines):
            next2_line = lines[i + 2]
            if any(x in next2_line for x in ["DOB", "YEAR", "BIRTH", "MALE", "FEMALE"]):
                score += 30

        score += len(words) * 5
        possible_names.append((score, line))

    if possible_names:
        possible_names.sort(key=lambda x: x[0], reverse=True)
        return possible_names[0][1]

    return "NAME NOT FOUND"

def smart_name_score(ticket_name, aadhaar_name):
    ticket_name = clean_name(ticket_name)
    aadhaar_name = clean_name(aadhaar_name)

    base_score = fuzz.token_sort_ratio(ticket_name, aadhaar_name)

    ticket_parts = ticket_name.split()
    aadhaar_parts = aadhaar_name.split()

    if len(ticket_parts) >= 2 and len(aadhaar_parts) >= 2:
        ticket_last = ticket_parts[-1]
        aadhaar_last = aadhaar_parts[-1]

        ticket_first = ticket_parts[0]
        aadhaar_first = aadhaar_parts[0]

        if ticket_last == aadhaar_last:
            if len(aadhaar_first) == 1 and ticket_first.startswith(aadhaar_first):
                return max(base_score, 88)

            if len(ticket_first) == 1 and aadhaar_first.startswith(ticket_first):
                return max(base_score, 88)

    return base_score


def status_from_score(score):
    if score >= 95:
        return "GREEN"
    elif score >= 80:
        return "YELLOW"
    return "RED"


@app.post("/verify")
async def verify(
    aadhaar_zip: UploadFile = File(...),
    ticket_zip: UploadFile = File(...)
):
    temp_dir = tempfile.mkdtemp()

    aadhaar_zip_path = os.path.join(temp_dir, "aadhaar.zip")
    ticket_zip_path = os.path.join(temp_dir, "tickets.zip")

    with open(aadhaar_zip_path, "wb") as f:
        f.write(await aadhaar_zip.read())

    with open(ticket_zip_path, "wb") as f:
        f.write(await ticket_zip.read())

    aadhaar_folder = os.path.join(temp_dir, "aadhaar")
    ticket_folder = os.path.join(temp_dir, "tickets")

    os.makedirs(aadhaar_folder, exist_ok=True)
    os.makedirs(ticket_folder, exist_ok=True)

    with zipfile.ZipFile(aadhaar_zip_path, "r") as z:
        z.extractall(aadhaar_folder)

    with zipfile.ZipFile(ticket_zip_path, "r") as z:
        z.extractall(ticket_folder)

    aadhaar_names = []
    ticket_names = []

    for root, dirs, files in os.walk(aadhaar_folder):
        for file in files:
            path = os.path.join(root, file)
            text = ocr_pdf_or_image(path)
            name = extract_aadhaar_name(text)

            aadhaar_names.append({
                "file": file,
                "name": name
            })
            print("AADHAAR EXTRACTED:", file, "->", name)

    for root, dirs, files in os.walk(ticket_folder):
        for file in files:
            path = os.path.join(root, file)
            text = extract_text_from_pdf(path)
            names = extract_ticket_names(text)

            for name in names:
                ticket_names.append({
                    "file": file,
                    "name": name
                })

    results = []

    for ticket in ticket_names:
        best_match = None
        best_score = 0

        for aadhaar in aadhaar_names:
            score = fuzz.token_sort_ratio(ticket["name"], aadhaar["name"])

            if score > best_score:
                best_score = score
                best_match = aadhaar

        if best_score < 80:
            best_match = {
                "file": "MANUAL REVIEW",
                "name": "NO RELIABLE AADHAAR MATCH"
            }

        results.append({
            "ticket_file": ticket["file"],
            "ticket_name": ticket["name"],
            "aadhaar_file": best_match["file"] if best_match else "NOT FOUND",
            "aadhaar_name": best_match["name"] if best_match else "NOT FOUND",
            "confidence": round(best_score, 2),
            "status": "YELLOW" if best_score < 80 else status_from_score(best_score)
        })

    green = len([r for r in results if r["status"] == "GREEN"])
    yellow = len([r for r in results if r["status"] == "YELLOW"])
    red = len([r for r in results if r["status"] == "RED"])
    
    global latest_results
    latest_results = results
    
    return {
        "green": green,
        "yellow": yellow,
        "red": red,
        "total": len(results),
        "results": results
    }
@app.get("/download-report")
def download_report():

    if not latest_results:
        return {
            "message": "No report available. Run verification first."
        }

    report_path = "verification_report.xlsx"

    df = pd.DataFrame(latest_results)

    df = df.rename(columns={
        "ticket_file": "Ticket File",
        "ticket_name": "Ticket Name",
        "aadhaar_file": "Aadhaar File",
        "aadhaar_name": "Aadhaar Name",
        "confidence": "Confidence",
        "status": "Status"
    })

    df.to_excel(report_path, index=False)

    return FileResponse(
        report_path,
        filename="verification_report.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )