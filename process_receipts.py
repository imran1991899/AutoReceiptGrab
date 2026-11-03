import os
import re
import base64
import json
from datetime import datetime
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from PIL import Image
from bs4 import BeautifulSoup
from pdf2image import convert_from_path
import requests

# Load configuration from config.json if it exists, otherwise fallback to environment variables
if os.path.exists('config.json'):
    with open('config.json', 'r') as f:
        config = json.load(f)
        CLIENT_ID = config.get("GMAIL_CLIENT_ID")
        CLIENT_SECRET = config.get("GMAIL_CLIENT_SECRET")
        REFRESH_TOKEN = config.get("GMAIL_REFRESH_TOKEN")
        DRIVE_FOLDER_ID = config.get("GOOGLE_DRIVE_FOLDER_ID")
else:
    CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID")
    CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET")
    REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN")
    DRIVE_FOLDER_ID = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")

SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/drive.file'
]

def get_credentials():
    return Credentials(
        token=None,
        refresh_token=REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        scopes=SCOPES
    )

def extract_email_body(payload):
    """Recursively extract HTML/plain text body from email parts."""
    body = ""
    if 'data' in payload.get('body', {}):
        data = payload['body']['data']
        body += base64.urlsafe_b64decode(data.encode('UTF-8')).decode('utf-8', errors='ignore')
    for part in payload.get('parts', []):
        body += extract_email_body(part)
    return body

def main():
    creds = get_credentials()
    gmail_service = build('gmail', 'v1', credentials=creds)
    drive_service = build('drive', 'v3', credentials=creds)

    output_dir = 'downloaded_receipts'
    grid_dir = 'grid_pages'
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(grid_dir, exist_ok=True)

    # Search query for Grab receipts
    query = 'subject:"Your Grab Receipt" OR from:no-reply@grab.com'
    results = gmail_service.users().messages().list(userId='me', q=query, maxResults=100).execute()
    messages = results.get('messages', [])

    print(f"Found {len(messages)} matching emails.")
    receipt_records = []

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

        # 1. Check for PDF/Image attachments
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
                                    print(f"PDF conversion error: {e}")
                                    continue

                            receipt_records.append((email_date, img_path))
                            attachments_found = True
                if 'parts' in part:
                    process_parts(part['parts'])

        process_parts(parts)

        # 2. Fallback: If no attachment, check for images embedded directly in the HTML body
        if not attachments_found:
            html_body = extract_email_body(message['payload'])
            soup = BeautifulSoup(html_body, 'html.parser')
            img_tags = soup.find_all('img')
            
            for idx, img_tag in enumerate(img_tags):
                img_url = img_tag.get('src')
                if img_url and img_url.startswith('http') and ('grab' in img_url.lower() or 'receipt' in img_url.lower()):
                    try:
                        response = requests.get(img_url, timeout=10)
                        if response.status_code == 200 and len(response.content) > 10000: # Filter out tiny tracking icons
                            img_path = os.path.join(output_dir, f"{msg_id}_inline_{idx}.png")
                            with open(img_path, 'wb') as f:
                                f.write(response.content)
                            receipt_records.append((email_date, img_path))
                            break # Grab the main receipt graphic and move on
                    except Exception as e:
                        print(f"Could not download inline image: {e}")

    # Sort chronologically (oldest to newest) based on email date
    receipt_records.sort(key=lambda x: x[0])
    image_paths = [path for _, path in receipt_records]

    if not image_paths:
        print("No receipts to process.")
        return

    # Create 2x2 grid pages (4 receipts per page)
    page_width, page_height = 1240, 1754 # A4 size at 150 DPI
    margin = 60
    slot_w = (page_width - (margin * 3)) // 2
    slot_h = (page_height - (margin * 3)) // 2

    page_num = 1
    for i in range(0, len(image_paths), 4):
        batch = image_paths[i:i+4]
        canvas = Image.new('RGB', (page_width, page_height), (255,
