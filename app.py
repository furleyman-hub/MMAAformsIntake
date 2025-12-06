import base64
import io
import json
import os
from typing import Dict, List, Tuple

import requests
import streamlit as st
from PIL import Image

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None


# -----------------------------
# CONFIG
# -----------------------------

OPENAI_API_KEY = st.secrets.get("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY"))
ZAPIER_WEBHOOK_URL = st.secrets.get(
    "ZAPIER_WEBHOOK_URL",
    os.getenv("ZAPIER_WEBHOOK_URL")
)

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = "gpt-4o"   # Vision-capable model


# -----------------------------
# HELPERS: FILE -> PAGE IMAGE BYTES
# -----------------------------

def ensure_pdf_ready():
    if fitz is None:
        raise RuntimeError(
            "PyMuPDF (pymupdf) is not installed. Install with 'pip install pymupdf'."
        )


def file_to_page_images(filename: str, data: bytes) -> List[bytes]:
    """
    Convert uploaded file bytes to a list of page images (PNG) as bytes.

    - If PDF: return one PNG bytes object per page.
    - If image: return a single-item list with that image as PNG bytes.
    """
    name = filename.lower()

    # PDF → use PyMuPDF for all pages
    if name.endswith(".pdf"):
        ensure_pdf_ready()
        doc = fitz.open(stream=data, filetype="pdf")
        if doc.page_count == 0:
            doc.close()
            raise ValueError("PDF has no pages.")

        page_images: List[bytes] = []
        for page in doc:
            pix = page.get_pixmap(dpi=200)
            img_bytes = pix.tobytes("png")
            page_images.append(img_bytes)

        doc.close()
        return page_images

    # Image → treat as single page
    img = Image.open(io.BytesIO(data))
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return [buf.getvalue()]


# -----------------------------
# OPENAI VISION → FINAL EFC PAYLOAD
# -----------------------------

def ensure_openai_ready():
    if not OPENAI_API_KEY:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Add it to .streamlit/secrets.toml or environment."
        )


def extract_efc_payload_from_image(image_bytes: bytes) -> Dict[str, str]:
    """
    Use OpenAI Vision to read a single form page image and return the FINAL EFC payload.

    Expected keys (all required by your existing integration):

      student_title        -> always "Miss"
      student_first_name   -> participant first name
      student_last_name    -> participant last name
      contact_title        -> always "Mrs"
      contact_first_name   -> parent first name
      contact_last_name    -> parent last name
      email                -> parent email
      phone                -> parent phone
      location             -> always "Marti Martial Arts Academy - NY - Marti Martial Arts - NY"
    """
    ensure_openai_ready()

    b64 = base64.b64encode(image_bytes).decode("utf-8")

    prompt = (
        "You are reading a printed signup form for a Girl Scout or seminar event at "
        "Marti Martial Arts Academy.\n\n"
        "From the form, identify:\n"
        "- The PARTICIPANT (student/child) full name\n"
        "- The PARENT or GUARDIAN full name\n"
        "- The parent/guardian email address\n"
        "- The parent/guardian mobile phone number\n\n"
        "Then build exactly this JSON object (no extra keys, no comments, no markdown):\n\n"
        "{\n"
        '  \"student_title\": \"Miss\",\n'
        '  \"student_first_name\": \"<participant first name>\",\n'
        '  \"student_last_name\": \"<participant last name>\",\n'
        '  \"contact_title\": \"Mrs\",\n'
        '  \"contact_first_name\": \"<parent first name>\",\n'
        '  \"contact_last_name\": \"<parent last name>\",\n'
        '  \"email\": \"<parent email>\",\n'
        '  \"phone\": \"<parent mobile phone>\",\n'
        '  \"location\": \"Marti Martial Arts Academy - NY - Marti Martial Arts - NY\"\n'
        "}\n\n"
        "Rules:\n"
        "- Always use the literal string \"Miss\" for student_title.\n"
        "- Always use the literal string \"Mrs\" for contact_title.\n"
        "- Split names into first and last at the first space; if only one name is present, "
        "use it as the first name and leave the last name empty.\n"
        "- If any value is missing or unreadable, use an empty string for that field.\n"
        "- Respond with JSON ONLY, no additional text or formatting."
    )

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    body = {
        "model": OPENAI_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "You extract structured EFC lead data from images of forms.",
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{b64}"
                        },
                    },
                ],
            },
        ],
        "temperature": 0,
    }

    resp = requests.post(OPENAI_API_URL, headers=headers, data=json.dumps(body), timeout=45)
    if resp.status_code >= 300:
        raise RuntimeError(f"OpenAI API error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    content = data["choices"][0]["message"]["content"]

    # Parse JSON from the model
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        try:
            start = content.index("{")
            end = content.rindex("}") + 1
            parsed = json.loads(content[start:end])
        except Exception:
            raise RuntimeError(f"Could not parse JSON from OpenAI response: {content}")

    # Ensure all required keys exist and are strings
    required_keys = [
        "student_title",
        "student_first_name",
        "student_last_name",
        "contact_title",
        "contact_first_name",
        "contact_last_name",
        "email",
        "phone",
        "location",
    ]

    normalized: Dict[str, str] = {}
    for key in required_keys:
        value = parsed.get(key, "")
        if value is None:
            value = ""
        normalized[key] = str(value).strip()

    # Enforce your hard-coded values regardless of model behavior
    normalized["student_title"] = "Miss"
    normalized["contact_title"] = "Mrs"
    normalized["location"] = "Marti Martial Arts Academy - NY - Marti Martial Arts - NY"

    return normalized


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
    return False, f"Zapier error {resp.status_code}: {resp.text[:300]}"


# -----------------------------
# STREAMLIT APP UI
# -----------------------------

def main():
    st.set_page_config(
        page_title="Seminar Form OCR → EFC Leads",
        page_icon="📝",
        layout="centered",
    )

    st.title("Seminar Form OCR → EFC Aquilla Lead Generator")
    st.write(
        "Upload one or more scanned PDFs or images of completed forms. "
        "Each PDF page or image will be sent to OpenAI Vision, converted into an EFC lead payload, "
        "and submitted to Zapier."
    )

    if not OPENAI_API_KEY:
        st.warning("OPENAI_API_KEY is not set. The app will not work until you add it to secrets.")
    if not ZAPIER_WEBHOOK_URL:
        st.warning("ZAPIER_WEBHOOK_URL is not set. Add it to .streamlit/secrets.toml.")

    uploaded_files = st.file_uploader(
        "Upload PDFs or images",
        type=["pdf", "png", "jpg", "jpeg"],
        accept_multiple_files=True,
    )

    if uploaded_files and st.button("Process & Send Leads"):
        for file_index, uploaded in enumerate(uploaded_files, start=1):
            st.markdown(f"---\n## File {file_index}: **{uploaded.name}**")

            try:
                raw_data = uploaded.read()

                # Convert file → list of page images
                page_images = file_to_page_images(uploaded.name, raw_data)
                num_pages = len(page_images)
                st.info(f"{uploaded.name}: detected {num_pages} page(s).")

                for page_index, image_bytes in enumerate(page_images, start=1):
                    st.markdown(f"### Page {page_index} of {num_pages}")

                    # Show preview
                    preview_img = Image.open(io.BytesIO(image_bytes))
                    st.image(preview_img, caption=f"Preview (page {page_index})", use_column_width=True)

                    with st.spinner("Calling OpenAI Vision to build EFC payload..."):
                        efc_payload = extract_efc_payload_from_image(image_bytes)

                    st.subheader("EFC payload to Zapier")
                    st.json(efc_payload)

                    success, msg = send_to_zapier(efc_payload)
                    if success:
                        st.success(f"Lead for page {page_index} sent successfully: {msg}")
                    else:
                        st.error(f"Failed to send lead for page {page_index}: {msg}")

            except Exception as e:
                st.error(f"Error processing {uploaded.name}: {e}")


if __name__ == "__main__":
    main()
