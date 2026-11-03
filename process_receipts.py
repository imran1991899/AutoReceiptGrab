import streamlit as st
import os
import re
import base64
import json
import io
import zipfile
from datetime import datetime
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from PIL import Image
from bs4 import BeautifulSoup
from pdf2image import convert_from_path
import requests

# Disable OAuth HTTPS requirement for local testing if needed, keep True for production
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '0' 

st.set_page_config(page_title="Grab Receipt Extractor", layout="wide")

# App Redirect URI matching your Streamlit Cloud deployment URL
REDIRECT_URI = "https://autoreceiptgrab-8wufs7kjhpxgcevtwhgiut.streamlit.app/"

# Load configuration from config.json
def load_config():
    if os.path.exists('config.json'):
        with open('config.json', 'r') as f:
            return json.load(f)
    return None

config = load_config()

st.title("🚖 Grab Receipt Extractor & Viewer")

if not config:
    st.error("`config.json` not found! Please create a `config.json` file with your `GMAIL_CLIENT_ID` and `GMAIL_CLIENT_SECRET`.")
else:
    CLIENT_ID = config.get("GMAIL_CLIENT_ID")
    CLIENT_SECRET = config.get("GMAIL_CLIENT_SECRET")

    SCOPES = [
        'https://www.googleapis.com/auth/gmail.readonly',
        'https://www.googleapis.com/auth/drive.file'
    ]

    # Set up Google OAuth Flow configuration for Web Client
    client_config = {
        "web": {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI]
        }
    }

    flow = Flow.from_client_config(
        client_config=client_config,
        scopes=SCOPES
    )
    flow.redirect_uri = REDIRECT_URI

    # Check query parameters for authorization code coming back from Google
    query_params = st.query_params
    if "code" in query_params and "credentials" not in st.session_state:
        try:
            code = query_params["code"]
            flow.fetch_token(code=code)
            creds = flow.credentials
            
            # Save credentials into session state
            st.session_state["credentials"] = {
                'token': creds.token,
                'refresh_token': creds.refresh_token,
                'token_uri': creds.token_uri,
                'client_id': creds.client_id,
                'client_secret': creds.client_secret,
                'scopes': creds.scopes
            }
            # Clear the query params from the URL cleanly
            st.query_params.clear()
            st.rerun()
        except Exception as e:
            st.error(f"Authentication failed: {e}")

    # Authentication Check
    if "credentials" not in st.session_state:
        st.markdown("### Please log in with your Google account to access your Grab receipts.")
        auth_url, _ = flow.authorization_url(prompt='consent', access_type='offline', include_granted_scopes='true')
        
        # Styled Login Button
        st.markdown(f"""
            <a href="{auth_url}" target="_self" style="
                display: inline-block;
                padding: 10px 20px;
                background-color: #4285F4;
                color: white;
                text-decoration: none;
                border-radius: 5px;
                font-weight: bold;
                font-family: sans-serif;
            ">🔑 Log in with Google</a>
        """, unsafe_allow_html=True)
    
    else:
        st.success("Successfully logged in with Google!")
        
        if st.button("Log out"):
            del st.session_state["credentials"]
            if 'receipt_records' in st.session_state:
                del st.session_state['receipt_records']
            st.rerun()

        st.markdown("---")
        st.markdown("Click the button below to scan your emails for **'Hope you enjoyed your ride!'** or Grab receipts, preview them instantly, and download them.")

        if st.button("Generate Grab Receipts", type="primary"):
            with st.spinner("Connecting to Gmail and searching for Grab receipts..."):
                try:
                    creds_dict = st.session_state["credentials"]
                    creds = Credentials(**creds_dict)
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
                        st.success(f"Found {len(messages)} matching emails. Processing receipts...")
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
                                non_local_found = False
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
                                                    except Exception:
                                                        continue

                                                receipt_records.append((email_date, img_path))
                                                non_local_found = True
                                    if 'parts' in part:
                                        if process_parts(part['parts']):
                                            non_local_found = True
                                return non_local_found

                            if process_parts(parts):
                                attachments_found = True

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
                                        except Exception:
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
