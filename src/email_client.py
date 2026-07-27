import os.path
import base64
import google_auth_oauthlib.flow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# If modifying these scopes, delete the file token.json.
# This scope allows us to read metadata, labels, and the email body securely.
SCOPES = ['https://www.googleapis.com/auth/gmail.modify']

def authenticate_gmail():
    """Shows basic usage of the Gmail API.
    Authenticates the user and returns the Gmail service object.
    """
    creds = None
    # The file token.json stores the user's access and refresh tokens.
    # It is created automatically when the authorization flow completes for the first time.
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
        
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                # Attempt to refresh the token automatically
                creds.refresh(Request())
            except Exception as e:
                print(f"Error refreshing token: {e}. Forcing re-authentication.")
                os.remove('token.json')
                return authenticate_gmail()
        else:
            # Monkey-patch the WSGI app to serve HTML instead of plain text
            
            if not hasattr(google_auth_oauthlib.flow._RedirectWSGIApp, '_patched'):
                old_call = google_auth_oauthlib.flow._RedirectWSGIApp.__call__
                def new_call(self, environ, start_response):
                    def custom_start_response(status, headers, exc_info=None):
                        new_headers = [(n, 'text/html; charset=utf-8') if n.lower() == 'content-type' else (n, v) for n, v in headers]
                        return start_response(status, new_headers, exc_info)
                    return old_call(self, environ, custom_start_response)
                google_auth_oauthlib.flow._RedirectWSGIApp.__call__ = new_call
                google_auth_oauthlib.flow._RedirectWSGIApp._patched = True

            # Trigger the browser-based OAuth flow with auto-redirect
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES)
            success_html = '<html><head><meta http-equiv="refresh" content="0;url=http://localhost:5173/"></head><body style="background:#0c1324;color:#22d3ee;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;"><h2>Authentication successful! Redirecting to MailMind...</h2></body></html>'
            creds = flow.run_local_server(port=0, success_message=success_html)
            
        # Save the credentials for the next run so we don't have to log in every time
        with open('token.json', 'w') as token:
            token.write(creds.to_json())

    try:
        # Build and return the Gmail API service object
        service = build('gmail', 'v1', credentials=creds)
        print("Authentication successful! Gmail API service is ready.")
        
        # Quick test: fetch the user's email address to prove it works
        profile = service.users().getProfile(userId='me').execute()
        email_address = profile.get('emailAddress', 'Unknown')
        print(f"Authenticated as: {email_address}")
        
        # Save email to a local file for the UI to read
        import json
        profile_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'data', 'user_profile.json'))
        os.makedirs(os.path.dirname(profile_path), exist_ok=True)
        with open(profile_path, 'w') as f:
            json.dump({"email": email_address}, f)
        
        return service
        
    except Exception as error:
        print(f"An error occurred during authentication or service creation: {error}")
        return None
    
def get_unread_emails(service,max_results=5):
    """Fetches and decodes unread emails from the inbox."""
    print(f"Querying the API for up to {max_results} unread emails...")
    try:
        # Step 1: Request a list of message ids
        results = service.users().messages().list(
            userId='me',
            labelIds = ['INBOX', 'UNREAD'],
            maxResults=max_results
        ).execute()

        messages = results.get('messages',[])

        if not messages:
            print("No unread messages found in your inbox.")
            return []
        
        email_data_list = []

        # Step 2: Fetch the full payload for each message ID
        for msg in messages:
            msg_id = msg['id']
            # format='full' gets the headers and the body
            message = service.users().messages().get(userId='me', id=msg_id, format='full').execute()

            payload = message.get('payload',{})
            headers = payload.get('headers',[])

            # Extarct subject and sender using list comprehension
            subject = next((header['value'] for header in headers if header['name'].lower() == 'subject'), 'No Subject ')
            sender = next((header['value'] for header in headers if header['name'].lower() == 'from'), 'Unknown Sender')

            # Extract and decode the body
            body = ""
            if 'parts' in payload:
                # Loop through the parts to find the plain text version
                for part in payload['parts']:
                    if part['mimeType'] == 'text/plain':
                        data = part['body'].get('data', '')
                        body = base64.urlsafe_b64decode(data).decode('utf-8')
                        break
            elif 'body' in payload and 'data' in payload['body']:
                # Sometimes the body isn't nested in parts
                data = payload['body']['data']
                body = base64.urlsafe_b64decode(data).decode('utf-8')

            # Store the extracted data in a clean dictionary
            email_data_list.append({
                'id': msg_id,
                'sender': sender,
                'subject': subject,
                # Truncating the body to 200 characters so our terminal doesn't flood during testing
                'body_snippet': body[:200] + '...' if len(body) > 200 else body 
            })
            
        return email_data_list

    except Exception as error:
        print(f"An error occurred while fetching emails: {error}")
        return []
    
def mark_as_read(service, message_id):
    """
    Removes the 'UNREAD' label from a specific email ID.
    """
    try:
        # The Gmail API requires a dictionary specifying which labels to add or remove
        service.users().messages().modify(
            userId='me',
            id=message_id,
            body={
                'removeLabelIds': ['UNREAD']
            }
        ).execute()
        return True
    except Exception as error:
        print(f"Failed to mark email {message_id} as read: {error}")
        return False

if __name__ == '__main__':
    # 1. Get the authenticated service
    gmail_service = authenticate_gmail()
    
    # 2. If authentication succeeded, fetch the emails
    if gmail_service:
        print("\n--- Starting Fetch Process ---")
        emails = get_unread_emails(gmail_service, max_results=5)
        
        # 3. Print the results to the terminal
        for i, email in enumerate(emails, 1):
            print(f"\n[Email {i}]")
            print(f"From: {email['sender']}")
            print(f"Subject: {email['subject']}")
            print(f"Body snippet: {email['body_snippet']}")
            print("-" * 40)