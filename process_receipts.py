import streamlit as st
import os
import re
import base64
import json
import io
import zipfile
from datetime import datetime
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from PIL import Image
from bs4 import BeautifulSoup
from pdf2image import convert_from_path
import requests

st.set_page_config(page_title="Grab Receipt Extractor", layout="wide")

# Load configuration from config.json
def load_config():
    if os.path.exists('config.json'):
        with open('config.json', 'r') as f:
            return json.load(f)
    return None

config = load_config()

st.title("🚖 Grab Receipt Extractor & Viewer")
st.markdown("Click the button below to fetch your Grab emails (looking for **'Hope you enjoyed your ride!'** or Grab receipts), preview them instantly, and download them.")

if not config:
    st.error("`config.json` not found! Please create a `config.json` file in your project folder with your Google API credentials (`GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN`).")
else:
    CLIENT_ID = config.get("GMAIL_CLIENT_ID")
    CLIENT_SECRET = config.get("GMAIL_CLIENT_SECRET")
    REFRESH_TOKEN = config.get("GMAIL_REFRESH_TOKEN")

    SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

    if st.button("Generate Grab Receipts", type="primary"):
        with st.spinner("Connecting to Gmail and fetching receipts..."):
            try:
                creds = Credentials(
                    token=None,
                    refresh_token=REFRESH_TOKEN,
                    token_uri="https://oauth2.googleapis.com/token",
                    client_id=CLIENT_ID,
                    client_secret=CLIENT_SECRET,
                    scopes=SCOPES
                )
                gmail_service = build('gmail', 'v1', credentials=creds)

                output_dir = 'downloaded_receipts'
                os.makedirs(output_dir, exist_ok=True)

                # Search query targeting the phrase and Grab senders/subjects
                query = '"Hope you enjoyed your ride!" OR subject:"Your Grab Receipt" OR from:no-reply@grab.com'
                results = gmail_service.users().messages().list(userId='me', q=query, maxResults=50).execute()
                messages = results.get('messages', [])

                if not messages:
                    st.warning("No matching emails found.")
                else:
                    st.success(f"Found {len(messages)} matching emails. Processing...")
                    receipt_records = []

                    def extract_email_body(payload):
                        body = ""
                        if 'data' in payload.get('body', {}):
                            data = payload['body']['data']
                            body += base64.urlsafe_b64decode(data.encode('UTF-8')).decode('utf-8', errors='ignore')
                        for part in payload.get('parts', []):
                            body += extract_email_body(part)
                        return body

                    for msg in messages:
                        msg_id = msg['id']
                        message = gmail_service.users().messages().get(userId='me', id=msg_id, format='full').execute()
                        
                        headers = message['payload']['headers']
                        date_str = next((h['value'] for h in headers if h['name'] == 'Date'), None)
                        
                        try:
                            clean_date_str = re.sub(r'\s*\([^)]*\)', '', date_str)
                            email_date = datetime.strptime(clean_date_str.strip()[:25].strip(), '%a, %d %b %Y %H:%M:%S')
                        except Exception:
                            email_date = datetime.now()

                        attachments_found = False
                        parts = message['payload'].get('parts', [])
                        
                        def process_parts(parts_list):
                            nonlocal attachments_found
                            for part in parts_list:
                                if part.get('filename'):
                                    filename = part['filename']
                                    if 'receipt' in filename.lower() or filename.endswith(('.pdf', '.png', '.jpg', '.jpeg')):
                                        att_id = part['body'].get('attachmentId')
                                        if att_id:
                                            attachment = gmail_service.users().messages().attachments().get(
                                                userId='me', messageId=msg_id, id=att_id).execute()
                                            file_data = base64.urlsafe_b64decode(attachment['data'].encode('UTF-8'))
                                            
                                            file_path = os.path.join(output_dir, f"{msg_id}_{filename}")
                                            with open(file_path, 'wb') as f:
                                                f.write(file_data)
                                            
                                            img_path = file_path
                                            if filename.endswith('.pdf'):
                                                try:
                                                    images = convert_from_path(file_path)
                                                    if images:
                                                        img_path = file_path.replace('.pdf', '.png')
                                                        images[0].save(img_path, 'PNG')
                                                except Exception as e:
                                                    continue

                                            receipt_records.append((email_date, img_path))
                                            attachments_found = True
                                if 'parts' in part:
                                    process_parts(part['parts'])

                        process_parts(parts)

                        # Fallback to inline graphic images if no attachments found
                        if not attachments_found:
                            html_body = extract_email_body(message['payload'])
                            soup = BeautifulSoup(html_body, 'html.parser')
                            img_tags = soup.find_all('img')
                            
                            for idx, img_tag in enumerate(img_tags):
                                img_url = img_tag.get('src')
                                if img_url and img_url.startswith('http') and ('grab' in img_url.lower() or 'receipt' in img_url.lower()):
                                    try:
                                        response = requests.get(img_url, timeout=10)
                                        if response.status_code == 200 and len(response.content) > 10000:
                                            img_path = os.path.join(output_dir, f"{msg_id}_inline_{idx}.png")
                                            with open(img_path, 'wb') as f:
                                                f.write(response.content)
                                            receipt_records.append((email_date, img_path))
                                            break
                                    except Exception as e:
                                        pass

                    # Sort receipts chronologically (newest first)
                    receipt_records.sort(key=lambda x: x[0], reverse=True)
                    st.session_state['receipt_records'] = receipt_records

            except Exception as e:
                st.error(f"An error occurred during processing: {e}")

    # Display previews if available in session state
    if 'receipt_records' in st.session_state and st.session_state['receipt_records']:
        st.markdown("---")
        st.subheader(f"📄 Extracted Receipts ({len(st.session_state['receipt_records'])} found)")

        # Bulk Download ZIP button
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w') as zip_file:
            for email_date, img_path in st.session_state['receipt_records']:
                if os.path.exists(img_path):
                    zip_file.write(img_path, os.path.basename(img_path))
        zip_buffer.seek(0)

        st.download_button(
            label="📥 Download All Receipts as ZIP",
            data=zip_buffer,
            file_name="grab_receipts.zip",
            mime="application/zip"
        )

        # Responsive 3-column preview grid
        cols = st.columns(3)
        for idx, (email_date, img_path) in enumerate(st.session_state['receipt_records']):
            if os.path.exists(img_path):
                col = cols[idx % 3]
                with col:
                    img = Image.open(img_path)
                    st.image(img, caption=f"Ride Date: {email_date.strftime('%Y-%m-%d %H:%M')}", use_container_width=True)
                    
                    with open(img_path, "rb") as file:
                        st.download_button(
                            label=f"Download Image {idx+1}",
                            data=file,
                            file_name=os.path.basename(img_path),
                            mime="image/png",
                            key=f"download_{idx}"
                        )
