"""MediRoute AI - FastAPI backend for the hackathon demo.

Run:
    uvicorn main:app --reload

This prototype uses in-memory demo data. Government verification and SMS OTP
are intentionally represented by mock/demo services unless production
credentials/integrations are added.
"""
import math
import os
import random
import time
import uuid
import threading
import sqlite3
import json
import urllib.request
import urllib.error
from datetime import date
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    from twilio.rest import Client as TwilioClient
except Exception:
    TwilioClient = None

app = FastAPI(title="MediRoute AI")
now = time.time
BASE_DIR = Path(__file__).resolve().parent
TN_REGISTRY_DB = BASE_DIR / "data" / "tn_clinical_establishments_2026.sqlite"
TN_REGISTRY_SOURCE = "Tamil Nadu Government Gazette No.20A, Part VI-Section 1 Supplement"
TN_REGISTRY_DATE = "2026-05-20"

def tn_registry_lookup(registration_no: str):
    reg = (registration_no or "").strip().upper()
    if not reg or not TN_REGISTRY_DB.exists():
        return None
    con = sqlite3.connect(TN_REGISTRY_DB)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT * FROM clinical_establishments WHERE UPPER(tncera_registration_no)=? LIMIT 1",
            (reg,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()

def tn_registry_status(record):
    if not record:
        return "not_found"
    end = record.get("validity_to_iso") or ""
    if not end:
        return "unknown"
    return "current" if end >= date.today().isoformat() else "expired"
CLOSE_ETA = 2

def dept(doctor, shift, on=True):
    return {"doctor": doctor, "shift": shift, "on": on}

# ---------------- demo data ----------------
HOSPITALS = {
    # Real hospitals in Thiruvananthapuram. Coordinates are used for the
    # hackathon map/routing demo; live capacity/specialist status is simulated.
    "H1": dict(name="General Hospital Thiruvananthapuram", lat=8.5004128, lon=76.9424379, beds=4, icu=2, status="open", updated=now() - 20,
               depts={"cardiology": dept("Dr. Arun", "09:00-17:00"), "neurology": dept("Dr. Meera", "09:00-21:00"),
                      "trauma": dept("Dr. Salim", "00:00-24:00"), "respiratory": dept("Dr. Anand", "08:00-16:00"),
                      "general": dept("Dr. Latha", "00:00-24:00")}),
    "H2": dict(name="Government Medical College Hospital Thiruvananthapuram", lat=8.5240, lon=76.9280, beds=5, icu=7, status="open", updated=now() - 45,
               depts={"cardiology": dept("Dr. Rao", "00:00-24:00"), "neurology": dept("Dr. Nair", "00:00-24:00"),
                      "trauma": dept("Dr. Biju", "00:00-24:00"), "respiratory": dept("Dr. Divya", "00:00-24:00"),
                      "general": dept("Dr. Priya", "00:00-24:00")}),
    "H3": dict(name="KIMSHEALTH Hospital Trivandrum", lat=8.513712, lon=76.909345, beds=3, icu=4, status="open", updated=now() - 70,
               depts={"cardiology": dept("Dr. Kumar", "09:00-21:00"), "neurology": dept("Dr. Joseph", "09:00-17:00"),
                      "respiratory": dept("Dr. Fatima", "00:00-24:00"), "general": dept("Dr. Hari", "00:00-24:00")}),
    "H4": dict(name="Ananthapuri Hospitals and Research Institute", lat=8.4864671, lon=76.9267161, beds=3, icu=3, status="open", updated=now() - 100,
               depts={"cardiology": dept("Dr. Biju", "00:00-24:00"), "trauma": dept("Dr. Sheela", "00:00-24:00"),
                      "respiratory": dept("Dr. Vimal", "00:00-24:00"), "general": dept("Dr. Meera", "00:00-24:00")}),
    "H5": dict(name="Sree Uthradom Thirunal (SUT) Hospital", lat=8.516498, lon=76.940622, beds=4, icu=4, status="open", updated=now() - 35,
               depts={"cardiology": dept("Dr. Nair", "00:00-24:00"), "neurology": dept("Dr. Maya", "09:00-21:00"),
                      "trauma": dept("Dr. Jose", "00:00-24:00"), "respiratory": dept("Dr. Anil", "00:00-24:00"), "general": dept("Dr. Priya", "00:00-24:00")}),
    "H6": dict(name="PRS Hospital", lat=8.4811163, lon=76.9592286, beds=3, icu=3, status="open", updated=now() - 55,
               depts={"cardiology": dept("Dr. Raj", "09:00-21:00"), "neurology": dept("Dr. Deepa", "09:00-17:00"),
                      "trauma": dept("Dr. Vivek", "00:00-24:00"), "general": dept("Dr. Lakshmi", "00:00-24:00")}),
}
PICKUPS = {
    "Kowdiar": (8.5190, 76.9570),
    "Kazhakkoottam": (8.5686, 76.8730),
    "Vizhinjam Road": (8.4560, 76.9900),
    "Palayam": (8.5009, 76.9430),
    "Medical College": (8.5240, 76.9280),
    # Tamil Nadu demo pickup points so an approved TN hospital can be tested end-to-end.
    "Nagercoil": (8.1833, 77.4119),
    "Kanyakumari": (8.0883, 77.5385),
    "Madurai": (9.9252, 78.1198),
    "Chennai": (13.0827, 80.2707),
}

# Language-independent emergency ontology. English and Tamil inputs are normalized
# into the same concepts before severity/routing is calculated. This keeps the
# routing result independent of the UI language and avoids a Tamil-only keyword
# path accidentally becoming GENERAL.
KEYWORDS = {
    "cardiology": [
        "chest pain", "heart attack", "myocardial infarction", "cardiac", "heart",
        "நெஞ்சு வலி", "மார்பு வலி", "மாரடைப்பு", "இதய", "இதய வலி", "இதயத் தாக்கம்"
    ],
    "respiratory": [
        "breath", "breathing", "asthma", "choking", "suffoc", "wheeze", "oxygen",
        "மூச்சு", "மூச்சுத்திணறல்", "சுவாசிக்க முடியவில்லை", "சுவாச சிரமம்", "மூச்சு விட முடியவில்லை"
    ],
    "neurology": [
        "stroke", "unconscious", "unresponsive", "seizure", "convulsion", "faint",
        "paralysis", "weakness on one side", "speech difficulty", "cannot speak",
        "பக்கவாதம்", "மயக்கம்", "நினைவிழப்பு", "நினைவிழந்த", "சுயநினைவின்றி", "வலிப்பு",
        "வலிப்பு நோய்", "ஒரு பக்கம் பலவீனம்", "பேச முடியவில்லை", "பேச்சு தடுமாற்றம்", "முகம் கோணல்"
    ],
    "trauma": [
        "accident", "crash", "collision", "fall", "fell", "fallen", "fracture", "injury",
        "wound", "cut", "crush", "hit by", "road traffic", "rta", "cannot move", "unable to move",
        "விபத்து", "சாலை விபத்து", "கீழே விழுந்த", "விழுந்தார்", "காயம்", "காயமடைந்த",
        "எலும்பு முறிவு", "எலும்பு உடைப்பு", "இரத்தக்காயம்", "அடிபட்ட", "அசைக்க முடியவில்லை"
    ],
    "obstetrics": [
        "pregnancy", "pregnant", "labour", "labor", "delivery", "contractions", "water broke",
        "water breaking", "maternity", "antenatal", "obstetric", "pregnancy bleeding",
        "bleeding during pregnancy", "கர்ப்பம்", "கர்ப்பிணி", "கர்ப்பமாக", "பிரசவம்", "பிரசவ வலி",
        "பிரசவ வலி", "நீர் உடைதல்", "கர்ப்ப கால இரத்தப்போக்கு", "கர்ப்பத்தில் இரத்தப்போக்கு"
    ],
}

# Critical / urgent concepts are deliberately shared by both languages.
# A confirmed/explicit emergency term such as மாரடைப்பு (heart attack) is
# critical; a symptom such as நெஞ்சு வலி (chest pain) is assessed as urgent
# unless additional critical features are present.
CRITICAL = [
    "unconscious", "unresponsive", "not responding", "not breathing", "cannot breathe",
    "difficulty breathing", "severe breathing difficulty", "cardiac arrest", "heart attack",
        "seizure", "convulsion",
    "myocardial infarction", "severe bleeding", "heavy bleeding", "uncontrolled bleeding",
    "stroke", "severe chest pain", "major trauma", "major accident", "pregnancy bleeding",
    "severe burns", "major burn", "poisoning", "overdose", "snake bite", "snakebite",
    "drowning", "electrocution", "electric shock", "anaphylaxis", "severe allergic reaction",
    "choking", "no pulse", "மாரடைப்பு", "இதயத் தாக்கம்", "பக்கவாதம்", "சுயநினைவின்றி",
    "நினைவிழந்த", "மூச்சு விட முடியவில்லை", "கடுமையான மூச்சுத்திணறல்", "அதிக இரத்தப்போக்கு",
    "கட்டுப்படுத்த முடியாத இரத்தப்போக்கு", "கர்ப்ப கால இரத்தப்போக்கு", "கர்ப்பத்தில் இரத்தப்போக்கு",
    "கடுமையான தீக்காயம்", "விஷம் குடித்த", "விஷம் உட்கொண்ட", "அளவுக்கு அதிக மருந்து",
    "பாம்பு கடி", "நீரில் மூழ்கி", "மின்சாரம் தாக்கியது", "மின்சார அதிர்ச்சி", "மூச்சுத் திணறல்"
]
SERIOUS = [
    "chest pain", "bleeding", "accident", "fracture", "seizure", "contractions", "water broke",
    "burn", "dog bite", "animal bite", "vomiting", "abdominal pain", "stomach pain", "high fever",
    "severe pain", "injury", "fall", "head injury", "back injury", "vision loss", "dislocation",
    "severe weakness", "நெஞ்சு வலி", "மார்பு வலி", "இரத்தப்போக்கு", "விபத்து", "எலும்பு முறிவு",
    "வலிப்பு", "பிரசவ வலி", "நீர் உடைதல்", "தீக்காயம்", "நாய் கடி", "விலங்கு கடி", "வாந்தி",
    "வயிற்று வலி", "கடுமையான வலி", "காயம்", "விழுந்தார்", "தலைக்காயம்", "முதுகுக்காயம்",
    "பார்வை இழப்பு", "மூட்டு விலகல்", "கடுமையான பலவீனம்", "அதிக காய்ச்சல்", "மூச்சுத்திணறல்"
]

# Real Tamil Nadu hospital locations used for the live-GPS routing prototype.
# Names/locations are real; bed counts and specialist availability below are
# simulated for the hackathon and must not be presented as live hospital data.
# Names/locations are real; bed counts and specialist availability below are
# simulated for the hackathon and must not be presented as live hospital data.
HOSPITALS.update({
    "TNREAL_CBE_GANGA": dict(
        name="Ganga Hospital, Coimbatore", lat=11.0261290, lon=76.9524873,
        beds=6, icu=4, status="open", updated=now()-30,
        depts={"cardiology": dept("Emergency Cardiology Team", "00:00-24:00"),
               "neurology": dept("Neurology Team", "00:00-24:00"),
               "trauma": dept("Trauma Team", "00:00-24:00"),
               "respiratory": dept("Respiratory Team", "00:00-24:00"),
               "general": dept("Emergency Medicine Team", "00:00-24:00")},
        state="Tamil Nadu", district="Coimbatore", address="Nesavaalar Colony, Saibaba Colony, Coimbatore, Tamil Nadu 641011",
        demo=False, data_source="Public hospital location data; operational availability simulated"),
    "TNREAL_CBE_PSG": dict(
        name="PSG Hospitals, Coimbatore", lat=11.0188398, lon=77.0073136,
        beds=7, icu=5, status="open", updated=now()-25,
        depts={"cardiology": dept("Cardiology Emergency Team", "00:00-24:00"),
               "neurology": dept("Neurology Team", "00:00-24:00"),
               "trauma": dept("Emergency Medicine Team", "00:00-24:00"),
               "respiratory": dept("Critical Care Team", "00:00-24:00"),
               "obstetrics": dept("Obstetrics & Gynaecology Team", "00:00-24:00"),
               "general": dept("Emergency Medicine Team", "00:00-24:00")},
        state="Tamil Nadu", district="Coimbatore", address="Peelamedu, Coimbatore, Tamil Nadu 641004",
        demo=False, data_source="Hospital's public website/location; operational availability simulated"),
    "TNREAL_CBE_KG": dict(
        name="KG Hospital, Coimbatore", lat=11.0226, lon=76.9539,
        beds=5, icu=3, status="open", updated=now()-40,
        depts={"cardiology": dept("Cardiology Team", "00:00-24:00"),
               "neurology": dept("Neurology Team", "00:00-24:00"),
               "trauma": dept("Trauma Team", "00:00-24:00"),
               "respiratory": dept("Emergency Medicine Team", "00:00-24:00"),
               "general": dept("Emergency Medicine Team", "00:00-24:00")},
        state="Tamil Nadu", district="Coimbatore", address="Government Arts College Road, Coimbatore, Tamil Nadu 641018",
        demo=False, data_source="Hospital public location page; operational availability simulated"),
    "TNREAL_CBE_RANJAN": dict(
        name="Ranjan Medical Centre and Hospital, Coimbatore",
        # Approximate map coordinate for the published Arasur Pirivu address;
        # hospital operational availability is simulated for the hackathon.
        lat=11.07154, lon=77.10391,
        beds=8, icu=5, status="open", updated=now()-20,
        depts={"obstetrics": dept("Obstetrics & Gynaecology Team", "00:00-24:00"),
               "general": dept("General Medicine Team", "00:00-24:00"),
               "trauma": dept("Emergency Team", "00:00-24:00")},
        state="Tamil Nadu", district="Coimbatore",
        address="8/34, Arasur Pirivu, Avinashi Road, Thennampalayam, Coimbatore, Tamil Nadu 641407",
        demo=False,
        data_source="Hospital website confirms Obstetrics & Gynaecology; location/address verified from public listings; capacity/availability simulated"),
    "TNREAL_CHN_RGGGH": dict(
        name="Rajiv Gandhi Government General Hospital, Chennai", lat=13.0808972, lon=80.2773284,
        beds=10, icu=8, status="open", updated=now()-35,
        depts={"cardiology": dept("Cardiology Emergency Team", "00:00-24:00"),
               "neurology": dept("Neurology Team", "00:00-24:00"),
               "trauma": dept("Trauma Team", "00:00-24:00"),
               "respiratory": dept("Respiratory Team", "00:00-24:00"),
               "general": dept("Emergency Medicine Team", "00:00-24:00")},
        state="Tamil Nadu", district="Chennai", address="General Hospital Road, Park Town, Chennai, Tamil Nadu 600003",
        demo=False, data_source="Public hospital location data; operational availability simulated"),
})

# Mock government/board verification registry for the hackathon.
# This is NOT a live government database.
REGISTRY = {
    "ambulance": {
        "KL01AB1234": {"driver": "Arun Kumar", "license": "KL-LIC-1001", "phone": "9876543210", "affiliation": "City General Hospital EMS", "authorization_id": "AMB-AUTH-001"},
        "KL01CD5678": {"driver": "Vijay Raj", "license": "KL-LIC-1002", "phone": "9876543211", "affiliation": "Lakeview Emergency Services", "authorization_id": "AMB-AUTH-002"},
        "KL21EF9012": {"driver": "Suresh Babu", "license": "KL-LIC-1003", "phone": "9876543212", "affiliation": "City General Hospital EMS", "authorization_id": "AMB-AUTH-003"},
    },
    "hospital": {
        "HOSP-KL-001": {"hospital_id": "H1", "name": "General Hospital Thiruvananthapuram", "staff_name": "Dr. Admin", "staff_id": "CGH-STAFF-001", "staff_phone": "9876543220"},
        "HOSP-KL-002": {"hospital_id": "H2", "name": "Government Medical College Hospital Thiruvananthapuram", "staff_name": "Dr. Priya", "staff_id": "MCH-STAFF-001", "staff_phone": "9876543221"},
        "HOSP-KL-003": {"hospital_id": "H3", "name": "KIMSHEALTH Hospital Trivandrum", "staff_name": "Dr. Kiran", "staff_id": "KIMS-STAFF-001", "staff_phone": "9876543222"},
        "HOSP-KL-004": {"hospital_id": "H4", "name": "Ananthapuri Hospitals and Research Institute", "staff_name": "Dr. Anu", "staff_id": "AHRI-STAFF-001", "staff_phone": "9876543223"},
        "HOSP-KL-005": {"hospital_id": "H5", "name": "Sree Uthradom Thirunal (SUT) Hospital", "staff_name": "Dr. Sree", "staff_id": "SUT-STAFF-001", "staff_phone": "9876543224"},
        "HOSP-KL-006": {"hospital_id": "H6", "name": "PRS Hospital", "staff_name": "Dr. Pranav", "staff_id": "PRS-STAFF-001", "staff_phone": "9876543225"},
    },
}

# Tamil Nadu hospital onboarding workflow.
# Registration/verification is a hackathon demo; it is NOT a live Tamil Nadu
# government API integration. Approved hospitals are added to the same live
# hospital pool used by ambulance ranking.
PENDING_HOSPITALS = {}
HOSPITAL_APPLICATIONS = {}
ADMIN_USERS = {"admin": "admin123"}

RES = {}
AMBS = {}
PUBLIC_USERS = {}
OTP_STORE = {}
ACTIVE = ("pending", "accepted")
# Protects the in-memory reservation state so simultaneous dispatches cannot
# allocate the same scarce emergency bed/resource.
RES_LOCK = threading.RLock()


# Built-in Tamil Nadu demo hospital for immediate end-to-end login testing.
# Synthetic data only; not a live government record.
HOSPITALS["TNDEMO1"] = dict(name="Nagercoil Government Medical Centre (Demo)", lat=8.1830, lon=77.4119, beds=8, icu=2, status="open", updated=now(), depts={"general": dept("Dr. Medical Officer", "00:00-24:00", True), "cardiology": dept("Cardiology on-call", "00:00-24:00", True), "trauma": dept("Trauma on-call", "00:00-24:00", True)}, state="Tamil Nadu", district="Kanyakumari", address="Nagercoil, Kanyakumari, Tamil Nadu", government_registration_no="TN-DEMO-REG-001", licence_no="TN-DEMO-LIC-001", verified_source="MediRoute demo verification")
REGISTRY["hospital"]["HOSP-TN-001"] = {"hospital_id":"TNDEMO1", "name":"Nagercoil Government Medical Centre (Demo)", "staff_name":"Dr. Tamil", "staff_id":"TN-STAFF-001", "staff_phone":"9876543230", "government_registration_no":"TN-DEMO-REG-001", "state":"Tamil Nadu"}

# Synthetic Tamil Nadu demo hospital directory. Names/locations are used only
# to exercise nearby-hospital routing during the hackathon; capacity and
# specialist availability are simulated and are NOT live hospital data.
HOSPITALS.update({
    "TNDEMO2": dict(name="Coimbatore Medical College Hospital (Demo)", lat=11.0190, lon=76.9558, beds=6, icu=3, status="open", updated=now(),
        depts={"cardiology": dept("Dr. Kumar", "00:00-24:00"), "trauma": dept("Dr. Arun", "00:00-24:00"), "respiratory": dept("Dr. Meena", "08:00-20:00"), "general": dept("Dr. Ravi", "00:00-24:00")},
        state="Tamil Nadu", district="Coimbatore", address="Coimbatore, Tamil Nadu", demo=True, demo_source="Synthetic hackathon directory"),
    "TNDEMO3": dict(name="PSG Hospitals (Demo)", lat=11.0186, lon=76.9780, beds=5, icu=4, status="open", updated=now(),
        depts={"cardiology": dept("Dr. Priya", "09:00-21:00"), "neurology": dept("Dr. Nair", "09:00-17:00"), "trauma": dept("Dr. Vivek", "00:00-24:00"), "general": dept("Dr. Latha", "00:00-24:00")},
        state="Tamil Nadu", district="Coimbatore", address="Coimbatore, Tamil Nadu", demo=True, demo_source="Synthetic hackathon directory"),
    "TNDEMO4": dict(name="KMCH Hospital (Demo)", lat=11.0467, lon=77.0060, beds=7, icu=5, status="open", updated=now(),
        depts={"cardiology": dept("Dr. Suresh", "00:00-24:00"), "neurology": dept("Dr. Divya", "00:00-24:00"), "respiratory": dept("Dr. Anil", "00:00-24:00"), "general": dept("Dr. Hari", "00:00-24:00")},
        state="Tamil Nadu", district="Coimbatore", address="Coimbatore, Tamil Nadu", demo=True, demo_source="Synthetic hackathon directory"),
    "TNDEMO5": dict(name="Government General Hospital Chennai (Demo)", lat=13.0789, lon=80.2707, beds=8, icu=5, status="open", updated=now(),
        depts={"cardiology": dept("Dr. Anand", "00:00-24:00"), "neurology": dept("Dr. Rekha", "00:00-24:00"), "trauma": dept("Dr. Bala", "00:00-24:00"), "general": dept("Dr. Priya", "00:00-24:00")},
        state="Tamil Nadu", district="Chennai", address="Chennai, Tamil Nadu", demo=True, demo_source="Synthetic hackathon directory"),
    "TNDEMO6": dict(name="Stanley Medical College Hospital (Demo)", lat=13.0840, lon=80.2785, beds=5, icu=4, status="open", updated=now(),
        depts={"cardiology": dept("Dr. Joseph", "09:00-21:00"), "trauma": dept("Dr. Meena", "00:00-24:00"), "general": dept("Dr. Kumar", "00:00-24:00")},
        state="Tamil Nadu", district="Chennai", address="Chennai, Tamil Nadu", demo=True, demo_source="Synthetic hackathon directory"),
    "TNDEMO7": dict(name="Apollo Hospitals Chennai (Demo)", lat=13.0067, lon=80.2572, beds=4, icu=3, status="open", updated=now(),
        depts={"cardiology": dept("Dr. Shah", "00:00-24:00"), "neurology": dept("Dr. Iyer", "00:00-24:00"), "respiratory": dept("Dr. Farah", "00:00-24:00"), "general": dept("Dr. Arun", "00:00-24:00")},
        state="Tamil Nadu", district="Chennai", address="Chennai, Tamil Nadu", demo=True, demo_source="Synthetic hackathon directory"),
    "TNDEMO8": dict(name="Government Rajaji Hospital Madurai (Demo)", lat=9.9252, lon=78.1198, beds=7, icu=4, status="open", updated=now(),
        depts={"cardiology": dept("Dr. Babu", "00:00-24:00"), "neurology": dept("Dr. Meera", "00:00-24:00"), "trauma": dept("Dr. Karthik", "00:00-24:00"), "general": dept("Dr. Priya", "00:00-24:00")},
        state="Tamil Nadu", district="Madurai", address="Madurai, Tamil Nadu", demo=True, demo_source="Synthetic hackathon directory"),
    "TNDEMO9": dict(name="Meenakshi Mission Hospital Madurai (Demo)", lat=9.9430, lon=78.1340, beds=5, icu=4, status="open", updated=now(),
        depts={"cardiology": dept("Dr. Kumar", "09:00-21:00"), "respiratory": dept("Dr. Anand", "00:00-24:00"), "general": dept("Dr. Latha", "00:00-24:00")},
        state="Tamil Nadu", district="Madurai", address="Madurai, Tamil Nadu", demo=True, demo_source="Synthetic hackathon directory")
})

for _hid, _h in HOSPITALS.items():
    _h.setdefault("state", "Kerala")
    _h.setdefault("demo", False)
    _h.setdefault("emergency_capable", True)
    _h.setdefault("blood_inventory", {"A+":2,"A-":1,"B+":2,"B-":1,"AB+":1,"AB-":0,"O+":3,"O-":1})

# Normalize hospitals added by later demo-directory updates too.
for _hid,_h in HOSPITALS.items():
    _h.setdefault("emergency_capable",True)
    _h.setdefault("blood_inventory",{"A+":2,"A-":1,"B+":2,"B-":1,"AB+":1,"AB-":0,"O+":3,"O-":1})

# ---------------- helpers ----------------
def km(a, b, c, d):
    p = math.pi / 180
    x = math.sin((c - a) * p / 2) ** 2 + math.cos(a * p) * math.cos(c * p) * math.sin((d - b) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(x))

def confidence(h):
    return round(max(0.2, math.exp(-(now() - h["updated"]) / 1800)), 2)

def held(hid, skip=None):
    return sum(1 for r in RES.values() if r["hospital"] == hid and r["status"] in ACTIVE and r["id"] != skip)

GENERAL_SYMPTOMS = [
    "fever", "vomiting", "diarrhea", "diarrhoea", "abdominal pain", "stomach pain", "headache",
    "dizziness", "weakness", "high fever", "allergy", "allergic", "pain", "swelling", "burn",
    "poison", "poisoning", "pregnancy", "delivery", "labor", "labour", "infection",
    "காய்ச்சல்", "வாந்தி", "வயிற்று வலி", "தலைவலி", "தலைசுற்றல்", "பலவீனம்", "வலி", "வீக்கம்", "தீக்காயம்"
]
IGNORED_INPUTS = {"hi", "hai", "hello", "hey", "hii", "test", "testing", "ok", "okay", "fine", "yes", "no", "good morning", "good evening"}

def validate_symptoms(text):
    """Accept natural-language emergency descriptions instead of a fixed phrase list.

    This is intentionally permissive: an ambulance must never be blocked merely
    because MediRoute has not seen a particular symptom phrase before. The text
    is sent to the optional healthcare-NLP provider when configured; otherwise
    the local emergency classifier provides a conservative routing category.
    """
    s = " ".join((text or "").strip().lower().split())
    if not s or len(s) < 3 or s in IGNORED_INPUTS:
        raise HTTPException(400, "Please describe the patient's actual symptoms or incident.")
    return s

# Optional production NLP integration. Keep credentials on the server, never in
# the browser. Azure Text Analytics for health supports NER, relation extraction,
# entity linking and assertion detection for unstructured medical text.
def azure_health_nlp(text):
    endpoint = os.getenv("AZURE_LANGUAGE_ENDPOINT", "").strip().rstrip("/")
    key = os.getenv("AZURE_LANGUAGE_KEY", "").strip()
    if not endpoint or not key:
        return []
    # Current Azure Language unified analyze-text endpoint. The API version can
    # be overridden because Azure service versions can change independently.
    version = os.getenv("AZURE_LANGUAGE_API_VERSION", "2026-05-01")
    url = f"{endpoint}/language/:analyze-text?api-version={version}"
    body = {
        "kind": "Healthcare",
        "parameters": {"modelVersion": "latest"},
        "analysisInput": {"documents": [{"id": "1", "language": "en", "text": text}]}
    }
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type":"application/json", "Ocp-Apim-Subscription-Key":key},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            raw=resp.read().decode("utf-8")
            payload=json.loads(raw) if raw else {}
            op_location=resp.headers.get("operation-location") or resp.headers.get("Operation-Location")
        if op_location and not _extract_health_entities(payload):
            # Some Azure health API versions return an asynchronous job. Poll
            # briefly; if unavailable, the local fallback still runs.
            for _ in range(4):
                time.sleep(0.4)
                poll=urllib.request.Request(op_location, headers={"Ocp-Apim-Subscription-Key":key})
                try:
                    with urllib.request.urlopen(poll, timeout=5) as pr:
                        payload=json.loads(pr.read().decode("utf-8"))
                    status=str(payload.get("status","")).lower()
                    if status in ("succeeded","completed","complete") or "results" in payload:
                        break
                except Exception:
                    break
        return _extract_health_entities(payload)
    except Exception:
        return []

def _extract_health_entities(payload):
    """Best-effort parser for Azure healthcare entity responses across API shapes."""
    found=[]
    def walk(x):
        if isinstance(x, dict):
            text=x.get("text") or x.get("normalizedText") or x.get("preferredTerm")
            category=str(x.get("category") or x.get("type") or "")
            confidence=x.get("confidenceScore", x.get("confidence", 0))
            if text and category and category.lower() in {
                "symptomsign","symptom","sign","diagnosis","condition","bodystructure","injury","problem"
            }:
                found.append({"text":str(text),"category":category,"confidence":confidence})
            for v in x.values(): walk(v)
        elif isinstance(x, list):
            for v in x: walk(v)
    walk(payload)
    # de-duplicate while retaining order
    out=[]; seen=set()
    for e in found:
        k=(e["text"].lower(),e["category"].lower())
        if k not in seen:
            seen.add(k); out.append(e)
    return out

def _norm_medical_text(text):
    """Normalize English/Tamil emergency wording into one local concept space.

    This is intentionally deterministic and local: no external medical API is
    required. The original wording is retained for the final symptom packet.
    """
    import unicodedata
    s = unicodedata.normalize("NFC", (text or "").lower())
    s = " ".join(s.split())
    # Tamil/English phrase aliases -> canonical English concepts.
    aliases = {
        # cardiac
        "மாரடைப்பு": "heart attack", "இதயத் தாக்கம்": "heart attack", "இதய தாக்கம்": "heart attack",
        "இதய வலி": "chest pain", "மார்பு வலி": "chest pain", "நெஞ்சு வலி": "chest pain",
        # respiratory
        "மூச்சுத்திணறல்": "breathing difficulty", "கடுமையான மூச்சுத்திணறல்": "severe breathing difficulty",
        "சுவாசிக்க முடியவில்லை": "cannot breathe", "சுவாச சிரமம்": "breathing difficulty",
        "மூச்சு விட முடியவில்லை": "cannot breathe", "மூச்சு திணறல்": "breathing difficulty",
        # neuro
        "பக்கவாதம்": "stroke", "வலிப்பு": "seizure", "வலிப்பு நோய்": "seizure",
        "மயக்கம்": "faint", "மயக்கமடைந்தார்": "unconscious", "நினைவிழப்பு": "unconscious",
        "நினைவிழந்தார்": "unconscious", "நினைவிழந்த": "unconscious", "சுயநினைவின்றி": "unconscious",
        "பேச முடியவில்லை": "cannot speak", "பேச்சு தடுமாற்றம்": "speech difficulty",
        "முகம் கோணல்": "face drooping", "ஒரு பக்கம் பலவீனம்": "one side weak",
        # trauma
        "விபத்து": "accident", "சாலை விபத்து": "road accident", "கீழே விழுந்தார்": "fall",
        "விழுந்தார்": "fall", "காயம்": "injury", "காயமடைந்தார்": "injury",
        "எலும்பு முறிவு": "fracture", "எலும்பு உடைப்பு": "fracture", "அசைக்க முடியவில்லை": "cannot move",
        # bleeding
        "அதிக இரத்தப்போக்கு": "heavy bleeding", "கட்டுப்படுத்த முடியாத இரத்தப்போக்கு": "uncontrolled bleeding",
        "இரத்தப்போக்கு": "bleeding", "அதிக இரத்தம் வெளியேறுகிறது": "heavy bleeding",
        # obstetric
        "கர்ப்பம்": "pregnancy", "கர்ப்பிணி": "pregnant", "கர்ப்பமாக": "pregnant",
        "பிரசவம்": "delivery", "பிரசவ வலி": "contractions", "நீர் உடைதல்": "water broke",
        "கர்ப்ப கால இரத்தப்போக்கு": "pregnancy bleeding", "கர்ப்பத்தில் இரத்தப்போக்கு": "pregnancy bleeding",
        # poisoning / bites / burns / drowning / electrical
        "விஷம் குடித்தார்": "poisoning", "விஷம் குடித்த": "poisoning", "விஷம் உட்கொண்டார்": "poisoning",
        "விஷம் உட்கொண்ட": "poisoning", "பாம்பு கடி": "snake bite", "பாம்பு கடித்தது": "snake bite",
        "நாய் கடி": "dog bite", "விலங்கு கடி": "animal bite", "தீக்காயம்": "burn",
        "கடுமையான தீக்காயம்": "severe burns", "நீரில் மூழ்கினார்": "drowning", "நீரில் மூழ்கி": "drowning",
        "மின்சாரம் தாக்கியது": "electric shock", "மின்சார அதிர்ச்சி": "electric shock",
        # common urgent/general symptoms
        "வாந்தி": "vomiting", "வயிற்று வலி": "abdominal pain", "கடுமையான வயிற்று வலி": "severe abdominal pain",
        "தலைவலி": "headache", "கடுமையான தலைவலி": "severe headache", "தலைசுற்றல்": "dizziness", "பலவீனம்": "weakness",
        "கடுமையான பலவீனம்": "severe weakness", "காய்ச்சல்": "fever", "அதிக காய்ச்சல்": "high fever",
        "பார்வை இழப்பு": "vision loss", "மூட்டு விலகல்": "dislocation",
    }
    # Longest phrases first prevents a short alias from breaking a longer one.
    for src in sorted(aliases, key=len, reverse=True):
        s = s.replace(src, aliases[src])
    return s

def _classify_local(s):
    # The classifier sees normalized English concepts plus any remaining
    # English/Tamil wording. This makes language choice irrelevant to routing.
    s = _norm_medical_text(s)
    broad = {
        "cardiology": ["chest pain", "heart attack", "heart", "cardiac", "palpitation", "pressure in chest"],
        "respiratory": ["breath", "breathing", "asthma", "choking", "suffoc", "wheeze", "oxygen"],
        "neurology": ["stroke", "unconscious", "unresponsive", "seizure", "convulsion", "faint", "paralysis", "one side weak", "speech difficulty", "cannot speak", "face drooping"],
        "trauma": ["accident", "crash", "collision", "fall", "fell", "fracture", "injury", "wound", "cut", "crush", "hit by", "road traffic", "rta", "cannot move", "unable to move"],
        "obstetrics": KEYWORDS["obstetrics"] + ["pregnancy", "pregnant", "labour", "labor", "maternity", "antenatal"],
        "burns": ["burn", "burned", "burnt", "scald", "electrical burn", "chemical burn"],
        "poisoning": ["poison", "poisoning", "overdose", "swallowed chemical", "pesticide", "toxic"],
        "snake_bite": ["snake bite", "snakebite", "snake bit", "bitten by a snake"],
        "animal_bite": ["dog bite", "animal bite", "cat bite", "bite wound"],
        "drowning": ["drowning", "drowned", "near drowning", "water inhalation", "pulled from water"],
        "pediatric": ["baby", "infant", "newborn", "child", "kid"],
        "eye": ["eye injury", "chemical in eye", "vision loss", "cannot see", "eye pain"],
        "orthopedic": ["dislocation", "joint injury", "limb injury", "broken arm", "broken leg"],
        "infection": ["sepsis", "septic", "high fever", "fever and confusion", "severe infection"],
        "electrical": ["electric shock", "electrocution", "electrical injury", "electricity shock"],
    }
    for dep, phrases in broad.items():
        if any(x in s for x in phrases):
            return dep
    return "general"

def classify(text):
    original = validate_symptoms(text)
    # Local-only normalization. Azure is deliberately not required for this
    # hackathon build; Tamil and English follow exactly the same path.
    combined = _norm_medical_text(original)
    d = _classify_local(combined)

    # Obstetric capability matching, while severity is still determined below.
    if any(x in combined for x in ["pregnancy", "pregnant", "labour", "labor", "delivery", "contractions", "water broke", "maternity", "obstetric"]):
        d = "obstetrics"

    critical_phrases = [
        "unconscious", "unresponsive", "not responding", "not breathing", "cannot breathe",
        "severe breathing difficulty", "cardiac arrest", "heart attack",
        "seizure", "convulsion", "myocardial infarction",
        "severe bleeding", "heavy bleeding", "uncontrolled bleeding", "stroke", "severe chest pain",
        "major trauma", "major accident", "pregnancy bleeding", "severe burns", "major burn",
        "poisoning", "overdose", "snake bite", "snakebite", "drowning", "electrocution",
        "electric shock", "anaphylaxis", "severe allergic reaction", "choking", "no pulse",
        "cannot speak", "face drooping", "one side weak", "major accident"
    ]
    serious_phrases = [
        "chest pain", "bleeding", "accident", "fracture", "seizure", "contractions", "water broke",
        "burn", "dog bite", "animal bite", "vomiting", "abdominal pain", "stomach pain", "high fever",
        "severe pain", "injury", "fall", "head injury", "back injury", "vision loss", "dislocation",
        "severe weakness", "breathing difficulty", "high fever", "severe headache", "severe dizziness"
    ]
    pr = 1 if any(x in combined for x in critical_phrases) else 2 if any(x in combined for x in serious_phrases) else 3

    # Context combinations: these remain language-independent after normalization.
    if ("pregnan" in combined or "pregnancy" in combined) and any(x in combined for x in ["heavy bleeding", "severe bleeding", "bleeding", "blood loss"]):
        pr = 1
    if any(x in combined for x in ["cannot speak", "unable to speak", "speech difficulty", "face drooping", "one side weak"]):
        pr = 1
    if "electric shock" in combined or "electrocution" in combined:
        pr = 1
    if d in ("snake_bite", "poisoning", "electrical", "drowning"):
        pr = 1
    if any(x in combined for x in ["snake bite", "snakebite", "bitten by a snake", "poisoning", "overdose", "drowning"]):
        pr = 1
    # Low-risk wording stays non-critical; a red-flag qualifier can raise it.
    if any(x in combined for x in ["mild fever", "low fever", "slight fever", "லேசான காய்ச்சல்"]):
        pr = 3
    if combined.strip() in {"fever", "headache", "dizziness", "weakness", "pain"}:
        pr = 3

    return d, pr, []

def max_eta_for_priority(priority):
    # Preferred emergency travel limits for the prototype. These are routing
    # guardrails, not clinical rules; production values must be set with
    # emergency-medicine stakeholders and local EMS policy.
    return {1:30,2:45,3:60}.get(priority,60)

def severity_label(priority):
    return {1: "CRITICAL", 2: "URGENT", 3: "NON-CRITICAL"}.get(priority, "URGENT")

def care_requirements(dep, priority, symptoms, blood_group=None):
    s=(symptoms or "").lower()
    bleeding=any(x in s for x in ["severe bleeding","heavy bleeding","bleeding during pregnancy","pregnancy bleeding","major trauma","இரத்த"])
    req=[]
    if priority==1:
        req.append("emergency stabilization")
        if dep in ("respiratory","neurology","trauma","obstetrics") or any(x in s for x in ["unconscious","not breathing","difficulty breathing"]):
            req.append("ICU/critical-care readiness")
        if bleeding: req.append("blood / transfusion readiness")
        if dep=="obstetrics" and bleeding: req.append("obstetric emergency + OT readiness")
        if dep=="trauma": req.append("trauma/OT readiness")
        if dep=="cardiology": req.append("cardiac emergency evaluation")
    elif priority==2:
        req.append("emergency stabilization")
        if dep=="obstetrics": req.append("obstetric care")
    else:
        req.append("appropriate emergency/department care")
    return req

def _blood_available(h, blood_group):
    # Do not use a hospital-maintained blood count as a routing gate.
    # Hospitals should not be expected to manually maintain stock in MediRoute;
    # the patient blood group is transmitted securely for preparation and the
    # receiving team verifies actual stock through its authorized blood-bank
    # workflow/e-RaktKosh.
    return None if blood_group else True

def _is_stabilization_capable(h, avail):
    return h.get("status")=="open" and avail>0 and h.get("emergency_capable",True)

def rank(lat, lon, dep, priority=3, exclude=(), skip=None, include_demo=False, public_user_id=None, symptoms=""):
    max_eta=max_eta_for_priority(priority)
    blood_group=(PUBLIC_USERS.get(public_user_id) or {}).get("blood_group") if public_user_id else None
    requirements=care_requirements(dep, priority, symptoms, blood_group)
    out=[]
    for hid,h in HOSPITALS.items():
        if h.get("demo",False) and not include_demo: continue
        distance=km(lat,lon,h["lat"],h["lon"]); eta=round(distance/35*60+1)
        avail=h["beds"]-held(hid,skip); d=h["depts"].get(dep)
        stabilization=_is_stabilization_capable(h,avail); definitive=bool(d and d.get("on"))
        blood_ok=_blood_available(h,blood_group) if any("blood" in x for x in requirements) else True
        icu_ok=(h.get("icu",0)>0) if any("ICU" in x for x in requirements) else True
        too_far=eta>max_eta; excluded=hid in exclude
        if definitive:
            tdc=eta; care_mode="direct definitive care"
        elif stabilization:
            transfers=[]
            for ohid,oh in HOSPITALS.items():
                if ohid==hid or ohid in exclude or (oh.get("demo",False) and not include_demo): continue
                od=oh.get("depts",{}).get(dep)
                if not od or not od.get("on") or oh.get("status")!="open": continue
                te=round(km(h["lat"],h["lon"],oh["lat"],oh["lon"])/35*60+1)
                if te<=max_eta: transfers.append(te)
            transfer_eta=min(transfers) if transfers else 999
            tdc=eta+transfer_eta+5 if transfer_eta<999 else eta+15
            care_mode="stabilize then transfer"
        else:
            tdc=999; care_mode="not suitable"
        if excluded: why="excluded"
        elif h.get("status")!="open": why="closed"
        elif too_far: why="too_far"
        elif not stabilization: why="no_emergency_capacity"
        elif priority==1 and not definitive and tdc>=999: why="no_definitive_path"
        else: why=None
        conf=confidence(h)
        resource_penalty=0 if definitive else (9 if priority==1 else 4)
        if priority==1 and not icu_ok: resource_penalty += 12
        score=(tdc if priority<=2 else eta)+max(0,10-min(avail,10))*0.7+resource_penalty+(1-conf)*12
        out.append(dict(id=hid,name=h["name"],eta=eta,km=round(distance,1),available=why is None,reason=why,confidence=conf,
            updated_ago=int(now()-h["updated"]),doctor=d["doctor"] if d else None,shift=d["shift"] if d else None,
            definitive=definitive,stabilization=stabilization,care_mode=care_mode,time_to_definitive=round(tdc,1) if tdc<999 else None,
            blood_available=blood_ok,icu_available=icu_ok,score=round(score,1),lat=h["lat"],lon=h["lon"],state=h.get("state",""),district=h.get("district",""),
            demo=bool(h.get("demo",False)),data_source=h.get("data_source",""),max_eta=max_eta,requirements=requirements))
    return sorted(out,key=lambda x:(not x["available"],x["score"]))

def say(amb, en, ta):
    amb["events"].append({"t": time.strftime("%H:%M:%S"), "en": en, "ta": ta})

def assign(res, exclude=(), rerouted=False):
    amb=AMBS[res["ambulance"]]
    ranked=rank(amb["lat"],amb["lon"],res["dept"],res["priority"],exclude,res["id"],include_demo=bool(res.get("demo_mode")),public_user_id=res.get("public_user_id"),symptoms=res.get("symptoms",""))
    best=next((h for h in ranked if h["available"]),None)
    res["candidates"]=[{k:h.get(k) for k in ("name","km","eta","available","reason","doctor","definitive","stabilization","care_mode","time_to_definitive","blood_available","icu_available")} for h in ranked[:8]]
    if not best:
        res["status"]="no_hospital"
        say(amb,"No hospital can currently provide a safe emergency path within the preferred travel-time window. Contact the control room.","பரிந்துரைக்கப்பட்ட பயண நேரத்திற்குள் பாதுகாப்பான அவசர சிகிச்சை பாதை கொண்ட மருத்துவமனை இல்லை. கட்டுப்பாட்டு அறையை தொடர்பு கொள்ளவும்.")
        return
    severity=severity_label(res["priority"])
    if best.get("definitive"):
        selection_reason=(f"{severity}: {best['name']} can provide the required {res['dept']} care directly. Estimated time to required care is {best.get('time_to_definitive',best['eta'])} min, with emergency capacity and current resources available.")
    else:
        selection_reason=(f"{severity}: the selected hospital is a nearby safe stabilization option. It does not show the required specialist as available, so MediRoute compares stabilization + transfer time instead of rejecting it automatically.")
    if res["dept"]=="obstetrics":
        selection_reason += " Pregnancy cases are severity-matched: routine pregnancy care can use a nearby obstetric-capable hospital; critical bleeding/shock requires critical-care, blood and OT readiness when available."
    if res.get("blood_group") and any("blood" in x for x in res.get("required_resources", [])):
        selection_reason += " Blood is required for this emergency; the patient's blood group is sent to the authorized receiving team, which must verify current blood-bank stock through its approved workflow."
    if best.get("icu_available") is False:
        selection_reason += " ICU capacity is not currently recorded as available; the system therefore favors a safer stabilization/transfer path when another option is faster for definitive care."
    if res.get("blood_group"):
        selection_reason += f" Patient blood group {res['blood_group']} is attached to the authorized emergency packet for hospital preparation."
    res.update(hospital=best["id"],eta=best["eta"],status="pending",hospital_km=best["km"],hospital_data_source=best.get("data_source",""), nlp_entities=res.get("nlp_entities", []),
               selection_reason=selection_reason,severity=severity,required_resources=best.get("requirements",[]),
               definitive_capability=best.get("definitive",False),stabilization_capability=best.get("stabilization",False),
               care_mode=best.get("care_mode"),time_to_required_care=best.get("time_to_definitive"),
               selected_blood_available=best.get("blood_available"),selected_icu_available=best.get("icu_available"),
               blood_verification="Receiving hospital must verify current blood-bank stock; MediRoute does not require manual hospital blood-stock entry.")
    res["tried"]=list(set(res.get("tried",[])+[best["id"]]))
    n=best["name"]
    if rerouted:
        say(amb,f"Destination changed. {n} is now recommended, {best['km']} km away.",f"இலக்கு மாற்றப்பட்டது. {n} இப்போது பரிந்துரைக்கப்படுகிறது, {best['km']} கிமீ தூரம்.")
    else:
        say(amb,f"Hospital selected: {n}. {best['km']} km away. Waiting for confirmation.",f"மருத்துவமனை தேர்வு: {n}. {best['km']} கிமீ தூரம். உறுதிப்படுத்தலுக்காக காத்திருக்கிறது.")

def normalize_phone(phone):
    digits = "".join(ch for ch in phone if ch.isdigit())
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    return digits

def deliver_otp(phone, otp):
    """Send real SMS when Twilio environment variables are configured.
    Without credentials, keep the hackathon demo fully local and return a demo OTP.
    """
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    sender = os.getenv("TWILIO_FROM_NUMBER")
    if not (TwilioClient and sid and token and sender):
        return False
    try:
        TwilioClient(sid, token).messages.create(
            body=f"MediRoute AI verification code: {otp}. Valid for 5 minutes.",
            from_=sender,
            to=f"+91{phone}"
        )
        return True
    except Exception:
        return False

def create_otp(phone, kind, meta=None):
    otp = f"{random.randint(0, 999999):06d}"
    OTP_STORE[(kind, phone)] = {"otp": otp, "expires": now() + 300, "meta": meta or {}}
    return otp, deliver_otp(phone, otp)

def verify_otp(kind, phone, otp):
    item = OTP_STORE.get((kind, phone))
    if not item or now() > item["expires"] or item["otp"] != otp:
        return None
    del OTP_STORE[(kind, phone)]
    return item


# ---------------- API models ----------------
class Verify(BaseModel):
    kind: str
    reg_id: str

class Dispatch(BaseModel):
    ambulance_id: str
    symptoms: str
    pickup: str | None = None
    lat: float | None = None
    lon: float | None = None
    public_user_id: str | None = None
    demo_mode: bool = False

class Respond(BaseModel):
    accept: bool

class Capacity(BaseModel):
    beds: int
    icu: int = 0

class BloodInventory(BaseModel):
    inventory: dict[str,int] = {}

class Spec(BaseModel):
    dept: str
    on: bool
    doctor: str | None = None
    shift: str | None = None

class PublicOtpRequest(BaseModel):
    name: str
    phone: str
    blood_group: str
    blood_contact_consent: bool = True

class PublicOtpVerify(BaseModel):
    phone: str
    otp: str

class AmbulanceOtpRequest(BaseModel):
    reg_id: str
    driver_name: str
    license_number: str
    affiliation: str
    authorization_id: str
    phone: str

class AmbulanceOtpVerify(BaseModel):
    reg_id: str
    phone: str
    otp: str

class HospitalOtpRequest(BaseModel):
    registration_id: str
    staff_name: str
    staff_id: str
    phone: str

class HospitalOtpVerify(BaseModel):
    registration_id: str
    phone: str
    otp: str

class HospitalRegistration(BaseModel):
    hospital_name: str
    state: str
    district: str
    address: str
    government_registration_no: str
    licence_no: str
    staff_name: str
    staff_id: str
    phone: str
    email: str = ""
    latitude: float
    longitude: float
    beds: int = 2
    icu: int = 1
    departments: list[str] = []

class AdminLogin(BaseModel):
    username: str
    password: str

class AdminDecision(BaseModel):
    application_id: str
    approve: bool
    note: str = ""

class LocationUpdate(BaseModel):
    lat: float
    lon: float

class PatientStatusUpdate(BaseModel):
    status: str
    note: str | None = None


# ---------------- authentication ----------------
@app.post("/api/auth/public/request-otp")
def public_request_otp(d: PublicOtpRequest):
    phone = normalize_phone(d.phone)
    if len(phone) != 10:
        raise HTTPException(400, "Enter a valid 10-digit Indian mobile number.")
    if not d.name.strip() or not d.blood_group.strip():
        raise HTTPException(400, "Name and blood group are required.")
    blood_group = d.blood_group.strip().upper()
    if blood_group not in {"A+","A-","B+","B-","AB+","AB-","O+","O-","UNKNOWN"}:
        raise HTTPException(400, "Select a valid blood group.")
    user_id = "PUB-" + uuid.uuid4().hex[:8].upper()
    otp, sent = create_otp(phone, "public", {"user_id": user_id, "name": d.name.strip(), "blood_group": blood_group, "blood_contact_consent": bool(d.blood_contact_consent)})
    return {"ok": True, "message": "OTP sent by SMS." if sent else "Demo OTP generated. Configure Twilio for real SMS delivery.",
            "sms_sent": sent, "demo_otp": None if sent else otp}

@app.post("/api/auth/public/verify-otp")
def public_verify_otp(d: PublicOtpVerify):
    phone = normalize_phone(d.phone)
    item = verify_otp("public", phone, d.otp.strip())
    if not item:
        raise HTTPException(401, "Invalid or expired OTP.")
    user_id = item["meta"]["user_id"]
    PUBLIC_USERS[user_id] = {
        "id": user_id, "name": item["meta"]["name"], "phone": phone,
        "blood_group": item["meta"]["blood_group"],
        "blood_contact_consent": bool(item["meta"].get("blood_contact_consent", True)),
        "created_at": now()
    }
    return {"ok": True, "user": PUBLIC_USERS[user_id]}

@app.post("/api/auth/ambulance/request-otp")
def ambulance_request_otp(d: AmbulanceOtpRequest):
    reg = d.reg_id.strip().upper()
    record = REGISTRY["ambulance"].get(reg)
    if not record:
        raise HTTPException(401, "Ambulance registration was not found in the demo verification registry.")
    if record["driver"].lower() != d.driver_name.strip().lower() or record["license"].lower() != d.license_number.strip().lower():
        raise HTTPException(401, "Driver or licence details do not match the verified registry.")
    if record["affiliation"].lower() != d.affiliation.strip().lower() or record["authorization_id"].lower() != d.authorization_id.strip().lower():
        raise HTTPException(401, "EMS/hospital affiliation or ambulance authorization does not match the verified registry.")
    phone = normalize_phone(d.phone)
    if phone != record["phone"]:
        raise HTTPException(401, "Phone number does not match the verified ambulance record.")
    otp, sent = create_otp(phone, "ambulance", {"reg_id": reg})
    return {"ok": True, "message": "Ambulance verified. OTP sent by SMS." if sent else "Ambulance verified. Demo OTP generated.",
            "sms_sent": sent, "demo_otp": None if sent else otp}

@app.post("/api/auth/ambulance/verify-otp")
def ambulance_verify_otp(d: AmbulanceOtpVerify):
    reg = d.reg_id.strip().upper()
    phone = normalize_phone(d.phone)
    item = verify_otp("ambulance", phone, d.otp.strip())
    if not item or item["meta"].get("reg_id") != reg:
        raise HTTPException(401, "Invalid or expired OTP.")
    record = REGISTRY["ambulance"][reg]
    AMBS.setdefault(reg, {"id": reg, "lat": PICKUPS["Kowdiar"][0], "lon": PICKUPS["Kowdiar"][1], "events": []})
    return {"ok": True, "ambulance": {"id": reg, **record, "verified": True}}

@app.get("/api/tn-registry/lookup")
def tn_registry_lookup_api(registration_no: str):
    record = tn_registry_lookup(registration_no)
    if not record:
        return {"found": False, "source": TN_REGISTRY_SOURCE, "source_date": TN_REGISTRY_DATE}
    return {"found": True, "status": tn_registry_status(record), "record": record, "source": TN_REGISTRY_SOURCE, "source_date": TN_REGISTRY_DATE}

@app.post("/api/hospital/register")
def register_hospital(d: HospitalRegistration):
    if d.state.strip().lower() != "tamil nadu":
        raise HTTPException(400, "This onboarding form is currently for Tamil Nadu hospitals. Choose Tamil Nadu.")
    required = [d.hospital_name, d.district, d.address, d.government_registration_no, d.licence_no, d.staff_name, d.staff_id, d.phone]
    if any(not str(x).strip() for x in required):
        raise HTTPException(400, "Please complete all required hospital, government registration and authorized staff fields.")
    phone = normalize_phone(d.phone)
    if len(phone) != 10:
        raise HTTPException(400, "Enter a valid 10-digit Indian mobile number.")
    gov = d.government_registration_no.strip().upper()
    tn_record = tn_registry_lookup(gov)
    if not tn_record:
        raise HTTPException(400, "TNCERA registration number was not found in the Tamil Nadu Government Gazette registry included with this demo. Check the number exactly as printed in the Gazette.")
    if str(tn_record.get("establishment_type", "")).strip().lower() != "hospital":
        raise HTTPException(400, "The supplied Tamil Nadu registration record is not classified as a Hospital in the Gazette registry.")
    tn_status = tn_registry_status(tn_record)
    if tn_status == "expired":
        raise HTTPException(400, f"The Gazette record was found, but its printed registration validity ended on {tn_record.get('validity_to_source')}. Use a currently valid registration record.")
    if any(x.get("government_registration_no") == gov for x in list(PENDING_HOSPITALS.values()) + list(HOSPITAL_APPLICATIONS.values())):
        raise HTTPException(409, "That government registration number already has a MediRoute application.")
    if any(v.get("government_registration_no") == gov for v in REGISTRY["hospital"].values()):
        raise HTTPException(409, "That government registration number is already registered with MediRoute.")
    app_id = "APP-TN-" + uuid.uuid4().hex[:8].upper()
    item = {
        "application_id": app_id, "status": "pending", "submitted_at": now(),
        "hospital_name": d.hospital_name.strip(), "state": "Tamil Nadu", "district": d.district.strip(),
        "address": d.address.strip(), "government_registration_no": gov, "licence_no": d.licence_no.strip().upper(),
        "staff_name": d.staff_name.strip(), "staff_id": d.staff_id.strip().upper(), "phone": phone,
        "email": d.email.strip(), "lat": d.latitude, "lon": d.longitude,
        "beds": max(0, d.beds), "icu": max(0, d.icu),
        "tn_registry_status": tn_status,
        "tn_registry_record": tn_record,
        "tn_registry_source": TN_REGISTRY_SOURCE,
        "tn_registry_source_date": TN_REGISTRY_DATE,
        "departments": [x.strip().lower() for x in d.departments if x.strip()] or ["general"],
    }
    PENDING_HOSPITALS[app_id] = item
    return {"ok": True, "application_id": app_id, "status": "pending", "message": "Application submitted for MediRoute admin verification."}

@app.post("/api/admin/login")
def admin_login(d: AdminLogin):
    if ADMIN_USERS.get(d.username.strip()) != d.password:
        raise HTTPException(401, "Invalid admin credentials.")
    return {"ok": True, "admin": {"username": d.username.strip(), "role": "admin"}}

@app.get("/api/admin/applications")
def admin_applications():
    return {"pending": list(PENDING_HOSPITALS.values()), "processed": list(HOSPITAL_APPLICATIONS.values())}

def _approve_hospital(item):
    hid = "TN" + uuid.uuid4().hex[:6].upper()
    departments = {}
    names = {"general":"Dr. Medical Officer", "cardiology":"Cardiology on-call", "neurology":"Neurology on-call", "trauma":"Trauma on-call", "respiratory":"Respiratory on-call"}
    for d in item["departments"]:
        if d not in names: d = "general"
        departments[d] = dept(names[d], "00:00-24:00", True)
    tnrec = item.get("tn_registry_record", {})
    HOSPITALS[hid] = dict(name=item["hospital_name"], lat=item["lat"], lon=item["lon"], beds=item["beds"], icu=item["icu"], status="open", updated=now(), depts=departments, state="Tamil Nadu", district=item["district"], address=item["address"], government_registration_no=item["government_registration_no"], licence_no=item["licence_no"], verified_source=TN_REGISTRY_SOURCE, tn_registry_status=item.get("tn_registry_status", "unknown"), tn_registry_name=tnrec.get("hospital_name_address", ""), tn_registry_district=tnrec.get("district", ""), tn_registry_validity_to=tnrec.get("validity_to_source", ""), emergency_capable=True, blood_inventory={"A+":2,"A-":1,"B+":2,"B-":1,"AB+":1,"AB-":0,"O+":3,"O-":1})
    reg_id = "MR-TN-" + uuid.uuid4().hex[:8].upper()
    REGISTRY["hospital"][reg_id] = {"hospital_id": hid, "name": item["hospital_name"], "staff_name": item["staff_name"], "staff_id": item["staff_id"], "staff_phone": item["phone"], "government_registration_no": item["government_registration_no"], "state": "Tamil Nadu"}
    out = dict(item)
    out.update(status="approved", hospital_id=hid, mediroute_registration_id=reg_id, approved_at=now())
    return out

@app.post("/api/admin/decision")
def admin_decision(d: AdminDecision):
    item = PENDING_HOSPITALS.pop(d.application_id, None)
    if not item:
        raise HTTPException(404, "Application not found or already processed.")
    if d.approve:
        out = _approve_hospital(item)
        out["admin_note"] = d.note
    else:
        out = dict(item, status="rejected", rejected_at=now(), admin_note=d.note)
    HOSPITAL_APPLICATIONS[d.application_id] = out
    return {"ok": True, "application": out}

@app.post("/api/auth/hospital/request-otp")
def hospital_request_otp(d: HospitalOtpRequest):
    reg = d.registration_id.strip().upper()
    record = REGISTRY["hospital"].get(reg)
    # Accept either the MediRoute hospital ID (MR-/HOSP-...) or the
    # government registration number entered during onboarding.
    if not record:
        for key, candidate in REGISTRY["hospital"].items():
            if str(candidate.get("government_registration_no", "")).upper() == reg:
                record = candidate
                reg = key
                break
    if not record:
        raise HTTPException(401, "Hospital registration ID was not found. Use the MediRoute ID shown after Admin approval, or the government registration number submitted during onboarding. Built-in demo: HOSP-TN-001 / Dr. Tamil / TN-STAFF-001 / 9876543230.")
    phone = normalize_phone(d.phone)
    # Demo hospital staff phone is intentionally fixed.
    if record["staff_name"].lower() != d.staff_name.strip().lower() or record["staff_id"].lower() != d.staff_id.strip().lower():
        raise HTTPException(401, "Authorized staff name or staff ID does not match the verified hospital record.")
    if phone != record["staff_phone"]:
        raise HTTPException(401, "Staff phone does not match the verified hospital record.")
    otp, sent = create_otp(phone, "hospital", {"registration_id": reg, "staff_name": d.staff_name.strip(), "staff_id": d.staff_id.strip(), "hospital_id": record["hospital_id"]})
    return {"ok": True, "message": "Hospital verified. OTP sent by SMS." if sent else "Hospital verified. Demo OTP generated.",
            "sms_sent": sent, "demo_otp": None if sent else otp}

@app.post("/api/auth/hospital/verify-otp")
def hospital_verify_otp(d: HospitalOtpVerify):
    reg = d.registration_id.strip().upper()
    phone = normalize_phone(d.phone)
    item = verify_otp("hospital", phone, d.otp.strip())
    if not item or item["meta"].get("registration_id") != reg:
        raise HTTPException(401, "Invalid or expired OTP.")
    record = REGISTRY["hospital"][reg]
    return {"ok": True, "hospital": {"id": record["hospital_id"], "name": record["name"], "staff": item["meta"]["staff_name"], "staff_id": item["meta"]["staff_id"], "verified": True}}


# ---------------- optional healthcare NLP status ----------------
@app.get("/api/health-nlp/status")
def health_nlp_status():
    return {"provider": "Azure AI Language - Text Analytics for health" if os.getenv("AZURE_LANGUAGE_ENDPOINT") and os.getenv("AZURE_LANGUAGE_KEY") else "local fallback", "configured": bool(os.getenv("AZURE_LANGUAGE_ENDPOINT") and os.getenv("AZURE_LANGUAGE_KEY")), "accepts_natural_language": True}

# ---------------- existing emergency APIs ----------------
@app.post("/api/verify")
def verify(v: Verify):
    ok = v.reg_id.strip().upper() in REGISTRY.get(v.kind, {})
    return {"verified": ok, "source": "MOCK registry (demo only - not a government database)"}

@app.get("/api/pickups")
def pickups():
    return list(PICKUPS)

@app.get("/api/public/hospitals")
def public_hospitals(lat: float, lon: float, dept: str = "general"):
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(400, "Invalid live location")
    # Public search uses the person's current browser GPS. Synthetic/demo
    # hospitals are excluded unless an explicit judge-only flow is used.
    return [{k: v for k, v in h.items() if k != "score"} for h in rank(lat, lon, dept, 3)]

@app.get("/api/hospital/{hid}/blood-donors")
def hospital_blood_donors(hid: str, blood_group: str):
    """Return opt-in public users who may be contacted for an urgent blood need.

    This is a coordination directory, not proof that a person is a donor or that
    their blood is medically suitable. Hospital staff must verify identity,
    current availability, eligibility and blood group before any clinical use.
    """
    if hid not in HOSPITALS:
        raise HTTPException(404, "Hospital not found")
    bg = blood_group.strip().upper()
    if bg not in {"A+","A-","B+","B-","AB+","AB-","O+","O-"}:
        raise HTTPException(400, "Select a valid blood group.")
    rows=[]
    for u in PUBLIC_USERS.values():
        if u.get("blood_group") == bg and u.get("blood_contact_consent", True):
            rows.append({
                "id": u["id"],
                "name": u["name"],
                "phone": u["phone"],
                "blood_group": u["blood_group"],
                "note": "Self-reported blood group; call the person to verify current availability and blood group before use."
            })
    return {"blood_group": bg, "count": len(rows), "donors": rows}

@app.post("/api/dispatch")
def dispatch(d: Dispatch):
    if (d.lat is None) != (d.lon is None):
        raise HTTPException(400, "Both latitude and longitude are required when sending live GPS.")
    # Live GPS is the default. The only exception is the explicit judge demo
    # switch, which simulates Nagercoil without pretending that the browser is
    # physically there.
    if d.demo_mode and d.pickup == "Nagercoil":
        lat, lon = PICKUPS["Nagercoil"]
        demo = True
    elif d.lat is not None and d.lon is not None:
        lat, lon = d.lat, d.lon
        demo = False
    else:
        raise HTTPException(400, "Live ambulance GPS is required. Allow location access or use the clearly labelled Judge Demo mode.")
    with RES_LOCK:
        for r in RES.values():
            if r["ambulance"] == d.ambulance_id and r["status"] in ACTIVE:
                r["status"] = "cancelled"
        dep, pr, nlp_entities = classify(d.symptoms)
        if d.public_user_id and d.public_user_id not in PUBLIC_USERS:
            raise HTTPException(400,"Emergency profile ID was not found. Ask the patient to share the correct MediRoute emergency profile ID.")
        patient=PUBLIC_USERS.get(d.public_user_id) if d.public_user_id else None
        # Map broad emergency categories to a routable hospital capability.
        # The original category is retained in the NLP metadata for transparency.
        original_dep = dep
        dep = {"burns":"trauma", "snake_bite":"general", "animal_bite":"general", "drowning":"respiratory", "pediatric":"general", "eye":"general", "orthopedic":"trauma", "infection":"general", "poisoning":"general"}.get(dep, dep)
        AMBS[d.ambulance_id] = {"id": d.ambulance_id, "lat": lat, "lon": lon, "events": AMBS.get(d.ambulance_id, {}).get("events", [])}
        res = dict(id=uuid.uuid4().hex[:6], ambulance=d.ambulance_id, symptoms=d.symptoms, dept=dep,
                   priority=pr, severity=severity_label(pr), tried=[], hospital=None, eta=0, status="pending", public_user_id=d.public_user_id,
                   blood_group=(patient or {}).get("blood_group"), patient_name=(patient or {}).get("name"),
                   nlp_entities=nlp_entities, original_emergency_category=original_dep,
                   patient_phone_last4=((patient or {}).get("phone") or "")[-4:], origin_lat=lat, origin_lon=lon, demo_mode=demo)
        RES[res["id"]] = res
        if demo:
            # Demo mode still uses the normal capability/capacity/travel-time
            # ranking; Nagercoil is only the simulated origin.
            assign(res)
            if res.get("hospital"):
                say(AMBS[d.ambulance_id], "Judge Demo mode: simulated Nagercoil origin used.", "ஜட்ஜ் டெமோ: நாகர்கோவில் சிமுலேஷன் இடம் பயன்படுத்தப்பட்டது.")
        else:
            assign(res)
        return res

@app.post("/api/ambulance/{aid}/location")
def update_location(aid: str, d: LocationUpdate):
    with RES_LOCK:
        AMBS.setdefault(aid, {"id": aid, "lat": d.lat, "lon": d.lon, "events": []})
        AMBS[aid]["lat"], AMBS[aid]["lon"] = d.lat, d.lon
        # Re-evaluate both pending and accepted trips. A live location update
        # must not silently leave an ambulance committed to a destination that
        # has lost emergency capacity/specialist coverage.
        active = next((r for r in reversed(list(RES.values())) if r["ambulance"] == aid and r["status"] in ACTIVE), None)
        if active and active.get("hospital"):
            current = rank(d.lat, d.lon, active["dept"], active["priority"], exclude=(), skip=active["id"], include_demo=bool(active.get("demo_mode")), public_user_id=active.get("public_user_id"), symptoms=active.get("symptoms",""))
            current_ok = next((x for x in current if x["id"] == active["hospital"]), None)
            if not current_ok or not current_ok["available"]:
                tried = set(active.get("tried", []))
                assign(active, exclude=tried, rerouted=True)
            else:
                active["eta"] = current_ok["eta"]
            # Keep the origin used by the reservation in sync for audit/debugging.
            active["origin_lat"], active["origin_lon"] = d.lat, d.lon
        return {"ok": True, "lat": d.lat, "lon": d.lon}

@app.post("/api/ambulance/{aid}/patient-status")
def patient_status(aid: str, d: PatientStatusUpdate):
    status = (d.status or "").strip().lower()
    allowed = {"deteriorating", "critical", "stable", "deceased"}
    if status not in allowed:
        raise HTTPException(400, "Status must be deteriorating, critical, stable, or deceased.")
    with RES_LOCK:
        active = next((r for r in reversed(list(RES.values())) if r["ambulance"] == aid and r["status"] in ACTIVE), None)
        if not active:
            raise HTTPException(404, "No active emergency trip was found for this ambulance.")
        amb = AMBS[aid]
        note = (d.note or "").strip()
        if status == "deceased":
            # MediRoute never determines death. This status is only recorded
            # after an authorized medical professional makes that determination.
            active["status"] = "medical_escalation"
            active["patient_status"] = "deceased_authorized"
            active["routing_suspended"] = True
            active["clinical_note"] = note
            say(amb, "Routing suspended: authorized medical personnel reported the patient deceased. Follow the local death/transport protocol and notify the receiving facility/control room.",
                "வழித்தடம் நிறுத்தப்பட்டது: அங்கீகரிக்கப்பட்ட மருத்துவ பணியாளர் நோயாளி இறந்ததாக பதிவு செய்துள்ளார். உள்ளூர் நடைமுறைப்படி செயல்பட்டு பெறும் மருத்துவமனை/கட்டுப்பாட்டு அறைக்கு தெரிவிக்கவும்.")
            return {"ok": True, "status": active["status"], "routing_suspended": True, "message": "Death recorded only as an authorized medical-personnel status; MediRoute does not determine death."}

        active["patient_status"] = status
        active["clinical_note"] = note
        if status in ("deteriorating", "critical"):
            # Re-rank from the ambulance's CURRENT GPS position, but do not
            # change the destination automatically. The crew/authorized EMS
            # decides whether to accept the recommended reroute.
            ranked = rank(amb["lat"], amb["lon"], active["dept"], 1,
                          exclude=(), skip=active["id"], include_demo=bool(active.get("demo_mode")),
                          public_user_id=active.get("public_user_id"), symptoms=active.get("symptoms", ""))
            current = next((x for x in ranked if x["id"] == active.get("hospital")), None)
            alternatives = [x for x in ranked if x["available"] and x["id"] != active.get("hospital")]
            best_alt = alternatives[0] if alternatives else None
            current_metric = (current.get("time_to_definitive") if current and current.get("time_to_definitive") is not None else current.get("eta") if current else 9999)
            alt_metric = (best_alt.get("time_to_definitive") if best_alt and best_alt.get("time_to_definitive") is not None else best_alt.get("eta") if best_alt else 9999)
            recommend = bool(best_alt and alt_metric + 5 < current_metric)
            active["reroute_recommended"] = recommend
            active["reroute_reason"] = "Patient condition escalated; a different currently suitable hospital has a materially lower estimated time-to-required-care." if recommend else "Patient condition escalated; no clearly faster suitable alternative was found from the current GPS position."
            if best_alt:
                active["recommended_hospital"] = best_alt["id"]
                active["recommended_hospital_name"] = best_alt["name"]
                active["recommended_hospital_eta"] = best_alt["eta"]
                active["recommended_time_to_care"] = best_alt.get("time_to_definitive")
            say(amb, "Patient condition escalated. Reassessing the safest destination from the current location. Authorized EMS must confirm any reroute.",
                "நோயாளியின் நிலை மோசமடைந்துள்ளது. தற்போதைய இடத்திலிருந்து பாதுகாப்பான இலக்கு மீண்டும் மதிப்பிடப்படுகிறது. வழித்தட மாற்றத்தை அங்கீகரிக்கப்பட்ட EMS உறுதிப்படுத்த வேண்டும்.")
        else:
            active["reroute_recommended"] = False
        return {"ok": True, "status": active.get("patient_status"), "reroute_recommended": active.get("reroute_recommended", False),
                "recommended_hospital": active.get("recommended_hospital_name"), "recommended_eta": active.get("recommended_hospital_eta"),
                "recommended_time_to_care": active.get("recommended_time_to_care")}

@app.post("/api/ambulance/{aid}/apply-reroute")
def apply_reroute(aid: str):
    with RES_LOCK:
        active = next((r for r in reversed(list(RES.values())) if r["ambulance"] == aid and r["status"] in ACTIVE), None)
        if not active:
            raise HTTPException(404, "No active emergency trip was found for this ambulance.")
        hid = active.get("recommended_hospital")
        if not hid:
            raise HTTPException(400, "No recommended alternate hospital is available.")
        tried = set(active.get("tried", []))
        # Re-run ranking from the latest GPS before applying the recommendation.
        ranked = rank(AMBS[aid]["lat"], AMBS[aid]["lon"], active["dept"], 1, exclude=tried - {hid}, skip=active["id"], include_demo=bool(active.get("demo_mode")), public_user_id=active.get("public_user_id"), symptoms=active.get("symptoms", ""))
        chosen = next((x for x in ranked if x["id"] == hid and x["available"]), None)
        if not chosen:
            active["reroute_recommended"] = False
            active.pop("recommended_hospital", None)
            raise HTTPException(409, "The recommended hospital is no longer available; reassessment is required.")
        active["hospital"] = chosen["id"]
        active["eta"] = chosen["eta"]
        active["hospital_km"] = chosen["km"]
        active["care_mode"] = chosen["care_mode"]
        active["time_to_required_care"] = chosen.get("time_to_definitive")
        active["required_resources"] = chosen.get("requirements", [])
        active["selection_reason"] = "Critical-condition reroute confirmed by authorized EMS/ambulance crew. Destination was rechecked against current capacity and capability."
        active["reroute_recommended"] = False
        active.pop("recommended_hospital", None)
        active.pop("recommended_hospital_name", None)
        active.pop("recommended_hospital_eta", None)
        active.pop("recommended_time_to_care", None)
        active["tried"] = list(set(active.get("tried", []) + [chosen["id"]]))
        say(AMBS[aid], f"Reroute confirmed. Proceed to {chosen['name']}.", f"வழித்தட மாற்றம் உறுதி செய்யப்பட்டது. {chosen['name']} மருத்துவமனைக்கு செல்லவும்.")
        return active

@app.get("/api/ambulance/{aid}")
def ambulance(aid: str):
    res = next((r for r in reversed(list(RES.values())) if r["ambulance"] == aid), None)
    amb = AMBS.get(aid, {"lat": PICKUPS["Kowdiar"][0], "lon": PICKUPS["Kowdiar"][1], "events": []})
    if not res:
        return {"reservation": None, "events": amb["events"], "location": {"lat": amb["lat"], "lon": amb["lon"]}}
    h = HOSPITALS.get(res["hospital"])
    return {"reservation": {**res, "hospital_name": h["name"] if h else None, "hospital_lat": h["lat"] if h else None,
                            "hospital_lon": h["lon"] if h else None},
            "events": amb["events"], "location": {"lat": amb["lat"], "lon": amb["lon"]}}

@app.post("/api/tick")
def tick():
    for r in RES.values():
        if r["status"] in ACTIVE:
            r["eta"] = max(0, r["eta"] - 1)
            if r["eta"] == 0:
                r["status"] = "arrived"
                say(AMBS[r["ambulance"]], "Arrived at hospital.", "மருத்துவமனையை அடைந்தது.")
    return {"ok": True}

@app.get("/api/hospital/{hid}")
def hospital(hid: str):
    if hid not in HOSPITALS:
        raise HTTPException(404, "Hospital not found")
    h = HOSPITALS[hid]
    inc = [r for r in RES.values() if r["hospital"] == hid and r["status"] in ACTIVE]
    incoming=[]
    for r in inc:
        item=dict(r)
        item["emergency_profile"]={"id":r.get("public_user_id"),"name":r.get("patient_name"),"blood_group":r.get("blood_group"),"phone_last4":r.get("patient_phone_last4")} if r.get("public_user_id") else None
        incoming.append(item)
    return {**h, "id": hid, "held": len(inc), "available": h["beds"] - len(inc), "confidence": confidence(h),
            "incoming": incoming, "identity_locked": True,
            "blood_workflow": "Patient blood group is delivered in the authorized emergency packet. Verify current blood-bank stock through the hospital's approved blood-bank workflow / e-RaktKosh."}

@app.post("/api/hospital/{hid}/respond/{rid}")
def respond(hid: str, rid: str, r: Respond):
    with RES_LOCK:
        if rid not in RES or RES[rid]["hospital"] != hid:
            raise HTTPException(404, "Emergency request not found for this hospital")
        res = RES[rid]
        amb = AMBS[res["ambulance"]]
        if r.accept:
            # The resource was already temporarily allocated while pending.
            # Acceptance confirms that allocation for the incoming ambulance.
            res["status"] = "accepted"
            say(amb, f"{HOSPITALS[hid]['name']} accepted. Team is preparing.", f"{HOSPITALS[hid]['name']} ஏற்றுக்கொண்டது. குழு தயாராகிறது.")
        else:
            say(amb, f"{HOSPITALS[hid]['name']} rejected the request.", f"{HOSPITALS[hid]['name']} கோரிக்கையை நிராகரித்தது.")
            assign(res, exclude=set(res["tried"]), rerouted=True)
        return res

@app.post("/api/hospital/{hid}/capacity")
def capacity(hid: str, c: Capacity):
    with RES_LOCK:
        if hid not in HOSPITALS:
            raise HTTPException(404, "Hospital not found")
        h = HOSPITALS[hid]
        h.update(beds=max(0, c.beds), icu=max(0, c.icu), updated=now())
        inc = sorted((r for r in RES.values() if r["hospital"] == hid and r["status"] in ACTIVE),
                     key=lambda r: (r["priority"], r["eta"]))
        # Keep only as many temporary reservations as the hospital says it can
        # currently handle. Lower-priority/farther ambulances are rerouted.
        keep_count = max(0, h["beds"])
        for r in inc[keep_count:]:
            amb = AMBS[r["ambulance"]]
            if r["eta"] <= CLOSE_ETA:
                say(amb, "Hospital capacity changed, but you are very close. Continue and alert hospital staff.",
                    "மருத்துவமனை திறன் மாறியுள்ளது, ஆனால் நீங்கள் மிக அருகில் உள்ளீர்கள். தொடரவும்.")
            else:
                assign(r, exclude=set(r["tried"]), rerouted=True)
        return hospital(hid)

@app.post("/api/hospital/{hid}/blood")
def blood_inventory(hid: str, b: BloodInventory):
    with RES_LOCK:
        if hid not in HOSPITALS: raise HTTPException(404,"Hospital not found")
        allowed={"A+","A-","B+","B-","AB+","AB-","O+","O-"}
        HOSPITALS[hid]["blood_inventory"]={k:max(0,int(v)) for k,v in b.inventory.items() if k in allowed}
        HOSPITALS[hid]["updated"]=now()
        return hospital(hid)

@app.post("/api/hospital/{hid}/specialist")
def specialist(hid: str, s: Spec):
    with RES_LOCK:
        if hid not in HOSPITALS:
            raise HTTPException(404, "Hospital not found")
        dept_key = s.dept.strip().lower()
        if not dept_key:
            raise HTTPException(400, "Department is required.")
        h = HOSPITALS[hid]
        if dept_key not in h["depts"]:
            h["depts"][dept_key] = dept(s.doctor.strip() if s.doctor else "Medical Officer", s.shift.strip() if s.shift else "00:00-24:00", s.on)
        else:
            item = h["depts"][dept_key]
            if s.doctor is not None and s.doctor.strip(): item["doctor"] = s.doctor.strip()
            if s.shift is not None and s.shift.strip(): item["shift"] = s.shift.strip()
            item["on"] = s.on
        h["updated"] = now()
        for r in [r for r in RES.values() if r["hospital"] == hid and r["status"] in ACTIVE and r["dept"] == dept_key and not s.on]:
            assign(r, exclude=set(r["tried"]), rerouted=True)
        return hospital(hid)

@app.post("/api/hospital/{hid}/specialist/add")
def specialist_add(hid: str, s: Spec):
    with RES_LOCK:
        if hid not in HOSPITALS:
            raise HTTPException(404, "Hospital not found")
        dept_key = s.dept.strip().lower()
        if not dept_key or dept_key in HOSPITALS[hid]["depts"]:
            raise HTTPException(400, "Enter a new department name.")
        HOSPITALS[hid]["depts"][dept_key] = dept(s.doctor.strip() if s.doctor else "Medical Officer", s.shift.strip() if s.shift else "00:00-24:00", s.on)
        HOSPITALS[hid]["updated"] = now()
        return hospital(hid)


# ---------------- open-source routing / traffic adapter ----------------
VALHALLA_ROUTE_URL = os.getenv("VALHALLA_ROUTE_URL", "https://valhalla1.openstreetmap.de/route")

# These are demo congestion corridors for the hackathon traffic layer. They are
# deliberately labelled as simulated; they are NOT claimed to be live traffic.
DEMO_TRAFFIC_ZONES = [
    {"name": "Gandhipuram", "lat": 11.0168, "lon": 76.9558, "radius_km": 2.2, "level": "heavy", "multiplier": 1.35},
    {"name": "Avinashi Road", "lat": 11.0142, "lon": 77.0015, "radius_km": 2.8, "level": "moderate", "multiplier": 1.20},
    {"name": "Peelamedu", "lat": 11.0257, "lon": 77.0090, "radius_km": 2.0, "level": "moderate", "multiplier": 1.18},
]

def _decode_polyline6(encoded):
    """Decode Valhalla's encoded polyline6 shape into [[lat, lon], ...]."""
    coords, index = [], 0
    lat = lon = 0
    factor = 10 ** 6
    while index < len(encoded):
        result = 0; shift = 0
        while True:
            b = ord(encoded[index]) - 63; index += 1
            result |= (b & 0x1f) << shift; shift += 5
            if b < 0x20: break
        dlat = -(result >> 1) if result & 1 else result >> 1
        lat += dlat
        result = 0; shift = 0
        while True:
            b = ord(encoded[index]) - 63; index += 1
            result |= (b & 0x1f) << shift; shift += 5
            if b < 0x20: break
        dlon = -(result >> 1) if result & 1 else result >> 1
        lon += dlon
        coords.append([lat / factor, lon / factor])
    return coords

def _zone_factor(points):
    """Return a transparent demo traffic estimate from route geometry."""
    hits = []
    max_mult = 1.0
    for z in DEMO_TRAFFIC_ZONES:
        hit = any(km(a[0], a[1], z["lat"], z["lon"]) <= z["radius_km"] for a in points[::max(1, len(points)//80)] if len(a) >= 2)
        if hit:
            hits.append(z["name"])
            max_mult = max(max_mult, z["multiplier"])
    if max_mult >= 1.30: level = "heavy"
    elif max_mult >= 1.15: level = "moderate"
    else: level = "light"
    return max_mult, level, hits

def _valhalla_route(payload):
    raw = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        VALHALLA_ROUTE_URL,
        data=raw,
        headers={"Content-Type": "application/json", "User-Agent": "MediRouteAI-Hackathon/9.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))

class RouteRequest(BaseModel):
    origin_lat: float
    origin_lon: float
    destination_lat: float
    destination_lon: float
    demo_traffic: bool = True

@app.post("/api/route")
def route(req: RouteRequest):
    payload = {
        "locations": [
            {"lat": req.origin_lat, "lon": req.origin_lon},
            {"lat": req.destination_lat, "lon": req.destination_lon},
        ],
        "costing": "auto",
        "units": "kilometers",
        "alternates": 2,
        "directions_options": {"units": "kilometers"},
    }
    try:
        data = _valhalla_route(payload)
    except Exception as exc:
        raise HTTPException(503, f"Open-source routing service unavailable: {exc}")

    trips = [data.get("trip", {})] + [x.get("trip", x) for x in data.get("alternates", [])]
    candidates = []
    for trip in trips:
        shape = trip.get("legs", [{}])[0].get("shape", "") if trip.get("legs") else ""
        if not shape:
            continue
        points = _decode_polyline6(shape)
        if len(points) < 2:
            continue
        summary = trip.get("summary", {})
        base_minutes = max(1, round(float(summary.get("time", 0)) / 60))
        distance_km = float(summary.get("length", 0))
        mult, level, zones = _zone_factor(points) if req.demo_traffic else (1.0, "unknown", [])
        traffic_minutes = max(base_minutes, round(base_minutes * mult))
        maneuvers = []
        for leg in trip.get("legs", []):
            maneuvers.extend(leg.get("maneuvers", []))
        next_m = next((m for m in maneuvers if m.get("type") not in (1, 2) and m.get("length", 0) > 0.01), None)
        instruction = (next_m or {}).get("instruction") or "Continue toward the hospital"
        verbal = (next_m or {}).get("verbal_pre_transition_instruction") or instruction
        next_distance = float((next_m or {}).get("length", 0)) * 1000
        steps = [{"instruction": x.get("instruction", ""), "distance_m": round(float(x.get("length", 0)) * 1000)}
                 for x in maneuvers if x.get("instruction")][:60]
        candidates.append({
            "steps": steps,
            "points": points,
            "base_eta_min": base_minutes,
            "eta_min": traffic_minutes,
            "distance_km": distance_km,
            "traffic_level": level,
            "traffic_zones": zones,
            "instruction": instruction,
            "verbal_instruction": verbal,
            "instruction_distance_m": next_distance,
        })
    if not candidates:
        raise HTTPException(502, "Routing engine returned no usable road route")
    best = min(candidates, key=lambda x: x["eta_min"])
    return {
        "provider": "Valhalla + OpenStreetMap",
        "open_source": True,
        "traffic_mode": "MediRoute demo traffic layer" if req.demo_traffic else "base road routing",
        "traffic_live": False,
        "route": best,
        "alternatives_considered": len(candidates),
    }

app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
