import streamlit as st
import imaplib
import email
from email.header import decode_header
from openai import OpenAI
import sqlite3
import hashlib
import time
import threading
import json
import os

# ==========================================
# 1. CORE SYSTEM & DATABASE
# ==========================================
def init_db():
    conn = sqlite3.connect("easymail.db", check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY,
            app_password TEXT NOT NULL,
            openai_key TEXT NOT NULL,
            auto_mode BOOLEAN DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS action_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email_user TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            subject TEXT,
            action_taken TEXT,
            is_important BOOLEAN
        )
    """)
    conn.commit()
    return conn

DB_CONN = init_db()

def hash_pass(password):
    return hashlib.sha256(password.encode()).hexdigest()

# ==========================================
# 2. EMAIL ENGINE & AI
# ==========================================
class EmailEngine:
    def __init__(self, user_email, app_password, openai_key):
        self.user_email = user_email
        self.app_password = app_password
        self.client = OpenAI(api_key=openai_key)
        
        domain = user_email.split('@')[-1].lower()
        if "gmail" in domain: self.imap_server = "imap.gmail.com"
        elif "icloud" in domain or "me.com" in domain: self.imap_server = "imap.mail.me.com"
        else: self.imap_server = "imap-mail.outlook.com"

    def decode_header_safe(self, header_value):
        if not header_value: return "Sconosciuto"
        decoded = decode_header(header_value)
        res = []
        for frag, enc in decoded:
            if isinstance(frag, bytes):
                res.append(frag.decode(enc or 'utf-8', errors='ignore'))
            else:
                res.append(str(frag))
        return "".join(res)

    def process_inbox(self):
        try:
            mail = imaplib.IMAP4_SSL(self.imap_server)
            mail.login(self.user_email, self.app_password)
            mail.select("inbox")

            status, messages = mail.search(None, 'UNSEEN')
            if status != 'OK' or not messages[0]:
                mail.logout()
                return 0

            email_ids = messages[0].split()
            processed_count = 0

            for e_id in email_ids[-10:]:
                res, msg_data = mail.fetch(e_id, '(RFC822)')
                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        subject = self.decode_header_safe(msg["Subject"])
                        sender = self.decode_header_safe(msg["From"])
                        
                        body = ""
                        if msg.is_multipart():
                            for part in msg.walk():
                                if part.get_content_type() == "text/plain":
                                    payload = part.get_payload(decode=True)
                                    if payload: body = payload.decode(errors='ignore')
                                    break
                        else:
                            payload = msg.get_payload(decode=True)
                            if payload: body = payload.decode(errors='ignore')

                        sys_prompt = """
                        Sei EASYMAIL, un assistente email. Analizza l'email e rispondi SOLO in JSON:
                        {
                            "categoria": "Spam_Pericoloso", "Importante", o "Normale",
                            "motivo": "Breve motivo",
                            "azione_immediata": "Cestinare" o "Avvisare_Utente" o "Lasciare_In_Inbox"
                        }
                        """
                        response = self.client.chat.completions.create(
                            model="gpt-4o-mini",
                            response_format={"type": "json_object"},
                            messages=[
                                {"role": "system", "content": sys_prompt},
                                {"role": "user", "content": f"Da: {sender}\nOgg: {subject}\nTesto: {body[:1000]}"}
                            ]
                        )
                        
                        ai_decision = json.loads(response.choices[0].message.content)
                        cursor = DB_CONN.cursor()

                        if ai_decision["categoria"] == "Spam_Pericoloso":
                            mail.store(e_id, '+FLAGS', '\\Deleted')
                            cursor.execute("INSERT INTO action_logs (email_user, subject, action_taken, is_important) VALUES (?, ?, ?, ?)",
                                           (self.user_email, subject, "🗑️ Eliminata (Spam/Pericolo)", 0))
                        
                        elif ai_decision["categoria"] == "Importante":
                            mail.store(e_id, '+FLAGS', '\\Flagged')
                            cursor.execute("INSERT INTO action_logs (email_user, subject, action_taken, is_important) VALUES (?, ?, ?, ?)",
                                           (self.user_email, subject, "🚨 " + ai_decision["motivo"], 1))
                        
                        else:
                            cursor.execute("INSERT INTO action_logs (email_user, subject, action_taken, is_important) VALUES (?, ?, ?, ?)",
                                           (self.user_email, subject, "📥 Lasciata in Inbox", 0))
                        
                        DB_CONN.commit()
                        processed_count += 1

            mail.expunge()
            mail.logout()
            return processed_count

        except Exception as e:
            print(f"Errore Motore Email: {e}")
            return 0

# ==========================================
# 3. BACKGROUND WORKER
# ==========================================
def background_task():
    while True:
        cursor = DB_CONN.cursor()
        cursor.execute("SELECT email, app_password, openai_key FROM users WHERE auto_mode = 1")
        active_users = cursor.fetchall()
        
        for user in active_users:
            engine = EmailEngine(user[0], user[1], user[2])
            engine.process_inbox()
            
        time.sleep(900)

if "bg_thread_started" not in st.session_state:
    thread = threading.Thread(target=background_task, daemon=True)
    thread.start()
    st.session_state.bg_thread_started = True


# ==========================================
# 4. USER INTERFACE (EASYMAIL Brand)
# ==========================================
st.set_page_config(page_title="EASYMAIL", page_icon="📨", layout="wide")

if "logged_in" not in st.session_state: st.session_state.logged_in = False

if not st.session_state.logged_in:
    col1, col2, col3 = st.columns([1,2,1])
    with col2:
        st.title("📨 EASYMAIL")
        st.markdown("La tua posta, pulita e automatizzata.")
        
        with st.container(border=True):
            l_email = st.text_input("Email", placeholder="es. mario@icloud.com")
            l_pass = st.text_input("Password App", type="password", placeholder="xxxx-xxxx-xxxx-xxxx")
            l_key = st.text_input("OpenAI API Key (Solo prima volta)", type="password", placeholder="sk-...")
            
            if st.button("Accedi / Registrati", type="primary", use_container_width=True):
                if l_email and l_pass:
                    cursor = DB_CONN.cursor()
                    cursor.execute("SELECT app_password, openai_key FROM users WHERE email = ?", (l_email,))
                    user = cursor.fetchone()
                    
                    if user:
                        if user[0] == hash_pass(l_pass):
                            st.session_state.logged_in = True
                            st.session_state.email = l_email
                            st.session_state.key = user[1]
                            st.session_state.password = l_pass
                            st.rerun()
                        else:
                            st.error("Password errata.")
                    else:
                        if l_key:
                            cursor.execute("INSERT INTO users (email, app_password, openai_key) VALUES (?, ?, ?)", 
                                         (l_email, hash_pass(l_pass), l_key))
                            DB_CONN.commit()
                            st.success("Registrato! Clicca di nuovo per entrare.")
                        else:
                            st.warning("Nuovo utente: Inserisci la chiave API OpenAI per registrarti.")
                else:
                    st.warning("Compila tutti i campi obbligatori.")

else:
    # --- DASHBOARD EASYMAIL ---
    with st.sidebar:
        st.title("📨 EASYMAIL")
        st.write(f"👤 **{st.session_state.email}**")
        cursor = DB_CONN.cursor()
        cursor.execute("SELECT auto_mode FROM users WHERE email = ?", (st.session_state.email,))
        auto_mode = cursor.fetchone()[0]
        
        new_mode = st.toggle("🤖 Modalità Automatica (15 min)", value=bool(auto_mode))
        if new_mode != bool(auto_mode):
            cursor.execute("UPDATE users SET auto_mode = ? WHERE email = ?", (int(new_mode), st.session_state.email))
            DB_CONN.commit()
            st.toast("Impostazioni salvate!")
            
        st.divider()
        if st.button("Logout"):
            st.session_state.logged_in = False
            st.rerun()

    st.title("📊 Pannello di Controllo EASYMAIL")
    if new_mode:
        st.success("EASYMAIL è in esecuzione in background. Eliminerà lo spam e ti avviserà qui delle email importanti.")
    else:
        st.warning("Modalità automatica spenta. Nessuna azione in background.")
    
    col_importanti, col_log = st.columns(2)
    
    with col_importanti:
        st.subheader("🚨 Email Importanti")
        cursor.execute("SELECT timestamp, subject, action_taken FROM action_logs WHERE email_user = ? AND is_important = 1 ORDER BY timestamp DESC LIMIT 10", (st.session_state.email,))
        importanti = cursor.fetchall()
        
        if not importanti:
            st.info("Nessun avviso critico al momento.")
        else:
            for imp in importanti:
                with st.container(border=True):
                    st.markdown(f"**{imp[1]}**")
                    st.caption(f"Ricevuta: {imp[0]}")
                    st.markdown(f"*{imp[2]}*")
            
    with col_log:
        st.subheader("🗑️ Azioni Recenti (Spam/Normale)")
        cursor.execute("SELECT timestamp, subject, action_taken FROM action_logs WHERE email_user = ? AND is_important = 0 ORDER BY timestamp DESC LIMIT 10", (st.session_state.email,))
        normali = cursor.fetchall()
        
        if not normali:
            st.info("Nessuna azione recente.")
        else:
            for norm in normali:
                with st.container(border=True):
                    st.markdown(f"**{norm[1]}**")
                    st.caption(f"Ricevuta: {norm[0]}")
                    st.markdown(f"*{norm[2]}*")