import io
import os
import re
from typing import Dict, Tuple, List

import requests
import streamlit as st
from PIL import Image

try:
    import pytesseract
except ImportError:
    pytesseract = None

try:
    from pdf2image import convert_from_bytes
except ImportError:
    convert_from_bytes = None


# -----------------------------
# CONFIG (Zapier URL stored in secrets)
# -----------------------------
ZAPIER_WEBHOOK_URL = st.secrets.get(
    "ZAPIER_WEBHOOK_URL",
    os.getenv("ZAPIER_WEBHOOK_URL")
)


# -----------------------------
# OCR + FILE HANDLING
# -----------------------------

def ensure_ocr_ready():
    if pytesseract is None:
        raise RuntimeError(
            "pytesseract is not installed. Install with 'pip install pytesseract pillow' "
            "and ensure Tesseract OCR is installed on your system."
        )


def bytes_to_images(filename: str, data: bytes) -> List[Image.Image]:
    filename = filename.lower()

    # PDF → convert pages to images
    if filename.endswith(".pdf"):
        if convert_from_bytes is None:
            raise RuntimeError(
                "pdf2image is not installed. Run 'pip install pdf2image' and install poppler."
            )
        pages = convert_from_bytes(data)
        return [p.convert("RGB") for p in pages]

    # Image file
    img = Image.open(io.BytesIO(data))
    if img.mode != "RGB":
        img = img.convert("RGB")
    return [img]


def ocr_images(images: List[Image.Image]) -> str:
    ensure_ocr_ready()
    texts = []
    for img in images:
        text = pytesseract.image_to_string(img)
        texts.append(text)
    return "\n\n".join(texts)


# -----------------------------
# FIELD EXTRACTION
# -----------------------------

def extract_field(text: str, pattern: str) -> str:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return ""
    value = match.group(1).strip()
    return value.strip(":-._ \t")


def parse_form_fields(text: str) -> Dict[str, str]:
    """
    Extract fields matching the sample Girl Scout / Seminar form.
    Update patterns if OCR output varies.
    """
    FIELD_PATTERNS = {
        "participant_name": r"Participant[s']?\s*Name[:\-]?\s*(.+)",
        "parent_name": r"Parent\/?Guardian\s*Name[:\-]?\s*(.+)",
        "address": r"Address[:\-]?\s*(.+)",
        "city": r"City[:\-]?\s*(.+)",
        "state": r"State[:\-]?\s*([A-Za-z]{2})",
        "zip": r"Zip\s*Code[:\-]?\s*([0-9]{5}(?:-[0-9]{4})?)",
        "phone": r"Parent\/?Guardian\s*Phone\s*#?[:\-]?\s*(.+)",
        "email": r"Parent\/?Guardian\s*Email[:\-]?\s*(.+)",
    }

    parsed = {}
    for key, pattern in FIELD_PATTERNS.items():
        parsed[key] = extract_field(text, pattern)

    return parsed


# -----------------------------
# BUILD EFC PAYLOAD
# -----------------------------

def split_name(full: str):
    parts = full.split()
    if len(parts) == 0:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def build_lead_payload(parsed: Dict[str, str]) -> Dict[str, str]:
    """
    Final mapping required by EFC Aquilla.
    """

    # Student (participant)
    student_full = parsed.get("participant_name", "").strip()
    student_first, student_last = split_name(student_full)

    # Parent (contact)
    parent_full = parsed.get("parent_name", "").strip()
    contact_first, contact_last = split_name(parent_full)

    payload = {
        "title": "Miss",
        "first_name": student_first,
        "last_name": student_last,

        "contact_title": "Mrs",
        "contact_first_name": contact_first,
        "contact_last_name": contact_last,

        "email_address": parsed.get("email", "").strip(),
        "mobile_phone": parsed.get("phone", "").strip(),

        # Required hard-coded dojo/EFC location
        "location": "Marti Martial Arts Academy - NY - Marti Martial Arts - NY",
    }

    return payload


# -----------------------------
# SEND TO ZAPIER
# -----------------------------

def send_to_zapier(payload: Dict[str, str]) -> Tuple[bool, str]:
    if not ZAPIER_WEBHOOK_URL:
        return False, "Zapier webhook URL is missing. Add it to secrets.toml."

    try:
        resp = requests.post(ZAPIER_WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e:
        return False, f"Error contacting Zapier: {e}"

    if 200 <= resp.status_code < 300:
        return True, f"Webhook OK ({resp.status_code})"
    else:
        return False, f"Zapier error {resp.status_code}: {resp.text[:300]}"


# -----------------------------
# STREAMLIT APP UI
# -----------------------------

def main():
    st.set_page_config(
        page_title="Seminar Form OCR → EFC Leads",
        page_icon="📝",
        layout="centered"
    )

    st.title("Seminar Form OCR → EFC Aquilla Lead Generator")
    st.write(
        "Upload one or more scanned PDFs or images of completed forms. "
        "Each file will be OCR’d, parsed, and submitted as a lead."
    )

    uploaded_files = st.file_uploader(
        "Upload PDFs or Images",
        type=["pdf", "png", "jpg", "jpeg"],
        accept_multiple_files=True
    )

    if uploaded_files and st.button("Process & Send Leads"):
        for index, uploaded in enumerate(uploaded_files, start=1):
            st.markdown(f"---\n### File {index}: **{uploaded.name}**")

            try:
                data = uploaded.read()

                images = bytes_to_images(uploaded.name, data)
                st.image(images[0], caption="Preview (first page)", use_column_width=True)

                with st.spinner("Running OCR..."):
                    text = ocr_images(images)

                st.subheader("OCR Text")
                st.text_area("Detected Text", text, height=200, key=f"ocr_{index}")

                parsed = parse_form_fields(text)
                st.subheader("Extracted Fields")
                st.json(parsed)

                payload = build_lead_payload(parsed)
                st.subheader("Payload Sent to EFC")
                st.json(payload)

                success, msg = send_to_zapier(payload)
                if success:
                    st.success(f"Sent to Zapier successfully: {msg}")
                else:
                    st.error(f"Failed to send: {msg}")

            except Exception as e:
                st.error(f"Error processing {uploaded.name}: {e}")


if __name__ == "__main__":
    main()
