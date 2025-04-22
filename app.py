from flask import Flask, render_template, request, session, redirect, url_for, jsonify
import smtplib
import imaplib
import email
import groq
import json
import re

app = Flask(__name__)
app.secret_key = "your_secret_key"

# Set your GROQ API key here.
GROQ_API_KEY = ""
groq_client = groq.Groq(api_key=GROQ_API_KEY)

# Mapping of user-friendly folder names to Gmail's IMAP folder names.
FOLDER_MAPPING = {
    "inbox": "INBOX",
    "sent": "[Gmail]/Sent Mail",
    "spam": "[Gmail]/Spam",
    "trash": "[Gmail]/Trash",
    "drafts": "[Gmail]/Drafts",
    "all": "[Gmail]/All Mail",
    "important": "[Gmail]/Important",
    "snoozed": "[Gmail]/Snoozed",
    "starred": "[Gmail]/Starred"
}

@app.route('/')
def index():
    # Redirect to login if the user hasn't logged in.
    if 'email' not in session:
        return redirect(url_for('login'))
    message = request.args.get("message", "")
    emails_json = request.args.get("emails", "[]")
    try:
        emails = json.loads(emails_json)
    except Exception:
        emails = []
    return render_template('index.html', message=message, emails=emails)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        session['email'] = request.form['email']
        session['password'] = request.form['password']
        return redirect(url_for('index'))
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('email', None)
    session.pop('password', None)
    return redirect(url_for('login'))

@app.route('/execute', methods=['POST'])
def execute():
    if 'email' not in session:
        return redirect(url_for('login'))
    
    prompt = request.form['prompt']
    system_message = """
You are an email assistant integrated into a Flask email application. The project uses IMAP to fetch emails and SMTP to send emails from a Gmail account. The application supports accessing the following Gmail folders: "INBOX", "[Gmail]/Sent Mail", "[Gmail]/Spam", "[Gmail]/Trash", "[Gmail]/Drafts", "[Gmail]/All Mail", "[Gmail]/Important", "[Gmail]/Snoozed", and "[Gmail]/Starred". When fetching or deleting, ensure the correct folder is accessed based on the user's specified section.
Always respond in a single valid JSON object with no additional text or formatting. Do not include any extra commentary.
{
    "action": "send_email" | "fetch_email" | "delete_email",
    "recipient": "(email address if applicable)",
    "subject": "(email subject if applicable)",
    "body": "(email body if applicable)",
    "time": "latest" | "oldest" | "all",
    "section": "inbox" | "sent" | "spam" | "snoozed" | "trash" | "drafts" | "all" | "important" | "starred",
    "email_id": "(specific IMAP email ID if applicable, required for single deletion)",
    "identifier": "(optional: filter by sender name or email address)",
    "count": "(number of emails if applicable)"
}
"""
    
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt}
            ]
        )
    except Exception as e:
        return redirect(url_for('index', message=f"❌ Error in AI service: {str(e)}"))
    
    # Retrieve and debug-print the raw AI response
    command_text = response.choices[0].message.content.strip()
    print("DEBUG: Raw AI response:", command_text)
    
    # Try to load the JSON directly
    try:
        command_data = json.loads(command_text)
    except json.JSONDecodeError:
        # Attempt to extract JSON object using regex
        json_match = re.search(r'\{.*\}', command_text, re.DOTALL)
        if json_match:
            json_str = json_match.group()
            try:
                command_data = json.loads(json_str)
            except json.JSONDecodeError:
                return redirect(url_for('index', message=f"❌ Extracted JSON is invalid! Response: {json_str}"))
        else:
            return redirect(url_for('index', message="❌ No JSON object found in the response!"))
    
    # Default: if no section is provided, set section to "inbox"
    email_id = (command_data.get("email_id") or "").strip()
    section = command_data.get("section") or "inbox"
    action = command_data.get("action", "")
    
    if action == "fetch_email":
        # If time is "all", fetch all emails; otherwise, limit to count
        if command_data.get("time", "").lower().strip() == "all":
            count = None  # We'll treat None as "fetch all"
        else:
            try:
                count = int(command_data.get("count", 10))
            except:
                count = 10
        identifier = command_data.get("identifier")
        emails = fetch_emails(session['email'], session['password'], 
                              time=command_data.get("time", "latest"), 
                              section=section, 
                              count=count,
                              identifier=identifier,
                              email_id=email_id if email_id else None)
        return render_template('index.html', emails=emails, message=f"Fetched emails from {section}")
    
    elif action == "send_email":
        recipient = command_data.get("recipient", "").strip()
        subject = command_data.get("subject", "No Subject")
        body = command_data.get("body", "No Content")
        if not recipient:
            return redirect(url_for('index', message="❌ Missing recipient email!"))
        send_email(session['email'], session['password'], recipient, subject, body)
        return redirect(url_for('index', message=f"✅ Email sent successfully to {recipient}!"))
    
    elif action == "delete_email":
        # If time is "all", delete all emails; otherwise, limit to count
        if command_data.get("time", "").lower().strip() == "all":
            count = None  # We'll treat None as "delete all"
        else:
            try:
                count = int(command_data.get("count", 1))
            except:
                count = 1
        identifier = command_data.get("identifier")
        time_param = command_data.get("time", "latest")
        result_message = delete_email(session['email'], session['password'], section, 
                                      email_id if email_id else None, 
                                      time_param, count, identifier)
        return redirect(url_for('index', message=result_message))
    
    return redirect(url_for('index', message="❌ Command not recognized!"))

def send_email(user, password, recipient, subject, body):
    server = smtplib.SMTP('smtp.gmail.com', 587)
    server.starttls()
    server.login(user, password)
    msg = f"Subject: {subject}\n\n{body}"
    server.sendmail(user, recipient, msg)
    server.quit()

def fetch_emails(user, password, time="latest", section="inbox", count=10, identifier=None, email_id=None):
    mail = imaplib.IMAP4_SSL('imap.gmail.com')
    try:
        mail.login(user, password)
    except Exception as e:
        return [{"section": section.capitalize(), "from": "Error", "subject": "Login Error", "body": str(e)}]
    
    imap_section = FOLDER_MAPPING.get(section.lower(), "INBOX")
    try:
        result, _ = mail.select(f'"{imap_section}"')
        if result != "OK":
            return [{"section": section.capitalize(), "from": "Error", "subject": "Folder Access Error", "body": f"Could not access {section} folder."}]
        
        # If an email_id is provided, fetch only that email.
        if email_id:
            result, data = mail.fetch(email_id, '(RFC822)')
            if result == "OK" and data:
                raw_email = data[0][1]
                msg = email.message_from_bytes(raw_email)
                sender = (msg.get("From") or "Unknown")
                subject_line = msg.get("Subject") or "No Subject"
                body_content = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() in ["text/plain", "text/html"]:
                            charset = part.get_content_charset() or "utf-8"
                            try:
                                body_content = part.get_payload(decode=True).decode(charset, errors='ignore')
                            except Exception:
                                body_content = part.get_payload(decode=True).decode(errors='ignore')
                            break
                else:
                    charset = msg.get_content_charset() or "utf-8"
                    try:
                        body_content = msg.get_payload(decode=True).decode(charset, errors='ignore')
                    except Exception:
                        body_content = msg.get_payload(decode=True).decode(errors='ignore')
                mail.logout()
                return [{
                    "imap_id": email_id,
                    "section": section.capitalize(),
                    "from": sender,
                    "subject": subject_line,
                    "body": body_content
                }]
            else:
                mail.logout()
                return [{"section": section.capitalize(), "from": "Error", "subject": "Fetch Error", "body": f"Could not fetch email id {email_id}."}]
        
        result, data = mail.search(None, "ALL")
        if result != "OK" or not data or not data[0]:
            return [{"section": section.capitalize(), "from": "No emails", "subject": "No emails found", "body": f"No emails found in {section}."}]
        
        email_ids = data[0].split()
        if not email_ids:
            return [{"section": section.capitalize(), "from": "No emails", "subject": "No emails found", "body": f"No emails found in {section}."}]
        
        # Determine which emails to fetch based on time parameter.
        time = time.lower().strip()
        if time == "oldest":
            email_ids = email_ids[:count] if count is not None else email_ids
        elif time == "latest":
            email_ids = email_ids[-count:] if count is not None else email_ids
        elif time == "all":
            email_ids = email_ids  # Fetch all emails in the folder.
        else:
            email_ids = email_ids[-count:] if count is not None else email_ids
        
        emails = []
        for eid in reversed(email_ids):
            result, data = mail.fetch(eid, '(RFC822)')
            if result == "OK":
                raw_email = data[0][1]
                msg = email.message_from_bytes(raw_email)
                sender = (msg.get("From") or "Unknown")
                subject_line = msg.get("Subject") or "No Subject"
                body_content = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() in ["text/plain", "text/html"]:
                            charset = part.get_content_charset() or "utf-8"
                            try:
                                body_content = part.get_payload(decode=True).decode(charset, errors='ignore')
                            except Exception:
                                body_content = part.get_payload(decode=True).decode(errors='ignore')
                            break
                else:
                    charset = msg.get_content_charset() or "utf-8"
                    try:
                        body_content = msg.get_payload(decode=True).decode(charset, errors='ignore')
                    except Exception:
                        body_content = msg.get_payload(decode=True).decode(errors='ignore')
                emails.append({
                    "imap_id": eid.decode() if isinstance(eid, bytes) else str(eid),
                    "section": section.capitalize(),
                    "from": sender,
                    "subject": subject_line,
                    "body": body_content
                })
        mail.logout()
        # If an identifier is provided, filter by sender.
        if identifier:
            identifier = identifier.lower().strip()
            emails = [e for e in emails if identifier in (e["from"] or "").lower()]
        if not emails:
            return [{"section": section.capitalize(), "from": "No emails", "subject": "No emails found", "body": f"No emails found in {section} matching the criteria."}]
        return emails
    except Exception as e:
        mail.logout()
        return [{"section": section.capitalize(), "from": "Error", "subject": "Exception", "body": str(e)}]

def delete_email(user, password, section, email_id=None, time="latest", count=1, identifier=None):
    """
    If email_id is provided, delete that specific email.
    Otherwise, if identifier is provided, delete emails whose 'From' field contains the identifier.
    If neither is provided and time is 'all', delete all emails; otherwise, delete emails based on time and count.
    """
    mail = imaplib.IMAP4_SSL('imap.gmail.com')
    try:
        mail.login(user, password)
    except Exception as e:
        return f"❌ Login Error: {str(e)}"
    
    imap_section = FOLDER_MAPPING.get(section.lower(), "INBOX")
    try:
        result, _ = mail.select(f'"{imap_section}"')
        if result != "OK":
            mail.logout()
            return f"❌ Could not access {section} folder."
        
        if email_id:
            result, _ = mail.store(email_id, '+FLAGS', r'(\Deleted)')
            if result != "OK":
                mail.logout()
                return f"❌ Failed to mark email id {email_id} for deletion."
            mail.expunge()
            mail.logout()
            return f"✅ Email with id {email_id} deleted from {section}."
        else:
            result, data = mail.search(None, "ALL")
            if result != "OK" or not data or not data[0]:
                mail.logout()
                return f"❌ No emails found in {section} for deletion."
            email_ids = data[0].split()
            if not email_ids:
                mail.logout()
                return f"❌ No emails found in {section} for deletion."
            
            if identifier:
                identifier = identifier.lower().strip()
                matching_ids = []
                for eid in email_ids:
                    result, data = mail.fetch(eid, '(RFC822)')
                    if result == "OK":
                        msg = email.message_from_bytes(data[0][1])
                        sender = (msg.get("From") or "").lower()
                        if identifier in sender:
                            matching_ids.append(eid)
                    if count is not None and len(matching_ids) >= count:
                        break
                email_ids = matching_ids
            else:
                time = time.lower().strip()
                if time == "oldest":
                    email_ids = email_ids[:count] if count is not None else email_ids
                elif time == "latest":
                    email_ids = email_ids[-count:] if count is not None else email_ids
                elif time == "all":
                    email_ids = email_ids
                else:
                    email_ids = email_ids[-count:] if count is not None else email_ids
            
            deleted_count = 0
            for eid in email_ids:
                result, _ = mail.store(eid, '+FLAGS', r'(\Deleted)')
                if result == "OK":
                    deleted_count += 1
            mail.expunge()
            mail.logout()
            return f"✅ {deleted_count} email(s) deleted from {section}."
    except Exception as e:
        mail.logout()
        return f"❌ Exception during deletion: {str(e)}"

if __name__ == '__main__':
    app.run(debug=True)
