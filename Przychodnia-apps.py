# import streamlit as st
import sqlite3 
import struct
import pandas as pd
from datetime import datetime, timedelta
from twilio.rest import Client
import pytz
from google.oauth2 import service_account
from googleapiclient.discovery import build
import time
import threading
import base64
import os
import streamlit as st
from streamlit.components.v1 import html
import plotly.graph_objects as go
import serial
from serial import SerialTimeoutException
import threading


class Config:
    DB_FILE = 'przychodnia.db'
    TIMEZONE = pytz.timezone('Europe/Warsaw')
    TWILIO_SID = st.secrets["TWILIO_SID"]
    TWILIO_TOKEN = st.secrets["TWILIO_TOKEN"]
    TWILIO_NUMBER = '+48732126845'
    GOOGLE_CREDS = 'przychodnia-system-api-661462b19bdb.json'
    DEFAULT_DURATION = 30
    BUFFER_TIME = 10
    CHECK_INTERVAL = 15
    LICENSE_KEY = 'AKTYWNA'
    
class ModemConfig:
    PORT = "COM3"           # zmień gdy inny port
    BAUDRATE = 115200       # lub 9600 w zależności od modemu
    TIMEOUT = 5
    LOCK = threading.Lock()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH  = os.path.join(BASE_DIR, Config.DB_FILE)

DB_PATH = r"C:\Users\jacek\Desktop\przychodniaapp\przychodnia.db"
print("DEBUG [przychodnia_apps] DB_PATH =", DB_PATH)

# na samej górze pliku
if 'selected_wizyta' not in st.session_state:
    st.session_state.selected_wizyta = None


def init_db():
    conn = sqlite3.connect(Config.DB_FILE)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS Pacjenci (
        ID INTEGER PRIMARY KEY AUTOINCREMENT,
        Imie TEXT, Nazwisko TEXT, Telefon TEXT, PESEL TEXT
    );""")
    c.execute("""CREATE TABLE IF NOT EXISTS Lekarze (
        ID INTEGER PRIMARY KEY AUTOINCREMENT,
        Imie TEXT, Nazwisko TEXT, Specjalizacja TEXT,
        Czas_Wizyty INTEGER, KalendarzID TEXT
    );""")
    c.execute("""CREATE TABLE IF NOT EXISTS Wizyty (
        ID INTEGER PRIMARY KEY AUTOINCREMENT,
        PacjentID INTEGER, LekarzID INTEGER, Data TEXT,
        Godzina TEXT, Status TEXT, EventID TEXT, Zrodlo TEXT,
        PrzypomnienieWyslane INTEGER DEFAULT 0
    );""")
    c.execute("""CREATE TABLE IF NOT EXISTS GodzinyPracyLekarzy (
        ID INTEGER PRIMARY KEY AUTOINCREMENT,
        LekarzID INTEGER, DzienTygodnia TEXT,
        GodzinaOd TEXT, GodzinaDo TEXT
    );""")
    conn.commit()

    c.execute("PRAGMA table_info(Pacjenci)")
    cols = [row[1] for row in c.fetchall()]
    if "Active" not in cols:
        c.execute("ALTER TABLE Pacjenci ADD COLUMN Active INTEGER DEFAULT 1")

        # Opcjonalnie: to samo dla Lekarze, jeśli chcesz soft-delete też lekarzy
    c.execute("PRAGMA table_info(Lekarze)")
    cols = [row[1] for row in c.fetchall()]
    if "Active" not in cols:
        c.execute("ALTER TABLE Lekarze ADD COLUMN Active INTEGER DEFAULT 1")

    c.execute("PRAGMA table_info(Wizyty)")
    cols = [row[1] for row in c.fetchall()]
    if "Opis" not in cols:
        conn.execute("ALTER TABLE Wizyty ADD COLUMN Opis TEXT;")

    conn.commit()
    conn.close()

from datetime import datetime, timedelta

def get_bot_count(conn):
    """
    Zwraca liczbę rezerwacji ze źródła 'Bot_SMS' w oknie ostatnich 30 minut.
    conn: aktywne sqlite3.Connection lub ścieżka do DB (jeśli nie jest connection).
    """
    close_conn = False
    if isinstance(conn, str):
        conn = sqlite3.connect(conn)
        close_conn = True

    try:
        teraz = datetime.now()
        prog = teraz - timedelta(minutes=30)
        # SQLite expects 'YYYY-MM-DD HH:MM:SS' format
        prog_str = prog.strftime("%Y-%m-%d %H:%M:%S")
        q = """
            SELECT COUNT(*) FROM Wizyty
            WHERE Zrodlo = 'Bot_SMS'
              AND datetime(Data || ' ' || Godzina) >= ?
        """
        cur = conn.cursor()
        cur.execute(q, (prog_str,))
        cnt = cur.fetchone()[0] or 0
        return int(cnt)
    finally:
        if close_conn:
            conn.close()

def get_calendar_service():
    creds = service_account.Credentials.from_service_account_file(
        Config.GOOGLE_CREDS,
        scopes=['https://www.googleapis.com/auth/calendar']
    )
    service = build('calendar', 'v3', credentials=creds)
    return service

init_db()
conn = sqlite3.connect(Config.DB_FILE)

st.set_page_config(page_title="Przychodnia", layout="wide")

st.markdown("""
<style>
/* Ukrywa surowe linie kodu HTML i <pre><code> bloki */
div[data-testid="stMarkdownContainer"] pre,
div[data-testid="stMarkdownContainer"] code {
    display: none !important;
    visibility: hidden !important;
    height: 0 !important;
    margin: 0 !important;
    padding: 0 !important;
}
</style>
""", unsafe_allow_html=True)


def init_modem():
    try:
        with ModemConfig.LOCK:
            ser = serial.Serial(port=ModemConfig.PORT,
                                baudrate=ModemConfig.BAUDRATE,
                                timeout=ModemConfig.TIMEOUT,
                                write_timeout=ModemConfig.TIMEOUT)
            time.sleep(0.5)
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            ser.write(b'ATE0\r')
            time.sleep(0.2)
            _ = ser.read(ser.in_waiting or 64)
            ser.write(b'AT+CMGF=1\r')
            time.sleep(0.2)
            _ = ser.read(ser.in_waiting or 64)
            ser.close()
        return True
    except Exception as e:
        st.warning(f"Nie udało się zainicjalizować modemu: {e}")
        return False


# --- 2️⃣ Inicjalizacja menu w state ---------------------
# 1️⃣ Menu na samej górze (biały header)
import streamlit as st

# Ustawienie query params na starcie
query_params = st.query_params
menu = query_params.get("menu", "start")

import base64
from pathlib import Path

def svg_data_uri(path: str) -> str:
    raw = Path(path).read_bytes()
    b64 = base64.b64encode(raw).decode("utf-8")
    return f"data:image/svg+xml;base64,{b64}"

def wyslij_sms_modem(numer: str, tresc: str, retries: int = 2, timeout_send: int = 30):
    last_exc = None
    for attempt in range(retries + 1):
        try:
            with ModemConfig.LOCK:
                ser = serial.Serial(port=ModemConfig.PORT,
                                    baudrate=ModemConfig.BAUDRATE,
                                    timeout=ModemConfig.TIMEOUT,
                                    write_timeout=ModemConfig.TIMEOUT)
                time.sleep(0.2)
                ser.reset_input_buffer()
                ser.reset_output_buffer()

                ser.write(b'AT+CMGF=1\r')
                time.sleep(0.2)
                _ = ser.read(ser.in_waiting or 64).decode(errors='ignore')

                cmd = f'AT+CMGS="{numer}"\r'.encode()
                ser.write(cmd)
                time.sleep(0.2)
                prompt = ser.read(ser.in_waiting or 64).decode(errors='ignore')
                # niektóre modemy nie zwracają '>' od razu — kontynuujemy

                ser.write(tresc.encode('utf-8', errors='replace'))
                ser.write(bytes([26]))  # CTRL+Z
                ser.flush()

                end_time = time.time() + timeout_send
                buffer = ""
                while time.time() < end_time:
                    chunk = ser.read(ser.in_waiting or 1).decode(errors='ignore')
                    if chunk:
                        buffer += chunk
                        if "+CMGS" in buffer:
                            ser.close()
                            st.success(f"SMS wysłany do {numer}")
                            return True
                        if "ERROR" in buffer:
                            raise Exception(f"Modem zwrócił ERROR: {buffer}")
                    else:
                        time.sleep(0.2)

                ser.close()
                raise TimeoutError(f"Brak potwierdzenia wysyłki z modemu. Odpowiedź: {buffer}")
        except Exception as e:
            last_exc = e
            time.sleep(1)
            continue
    st.warning(f"Nie udało się wysłać SMS do {numer}: {last_exc}")
    return False


def wyslij_sms(numer, tresc):
    # Jeżeli chcesz przetworzyć polskie znaki, dodaj transliterację tutaj
    return wyslij_sms_modem(numer, tresc)

def wyslij_sms_potwierdzenie(pacjent_id, data_str, godzina, conn):
    # 1) Budujemy treść SMS-a
    tresc = f"Twoja wizyta została zaplanowana na {data_str} o godz. {godzina}."

    try:
        # 2) Wywołanie API bramki SMS (przykład)
        wyslij_do_bramki(numer, tresc)
    except Exception as e:
        st.warning(f"(TEST) SMS nie został wysłany: {tresc}")


def wyslij_przypomnienie():
    with sqlite3.connect(Config.DB_FILE) as conn_local:
        c = conn_local.cursor()
        teraz = datetime.now()
        c.execute("SELECT ID, PacjentID, Data, Godzina FROM Wizyty WHERE PrzypomnienieWyslane=0")
        wizyty = c.fetchall()
        for id_wizyty, pacjent_id, data, godzina in wizyty:
            wizyta_czas = datetime.strptime(f"{data} {godzina}", "%Y-%m-%d %H:%M")
            roznica = (wizyta_czas - teraz).total_seconds()
            if 7140 <= roznica <= 7260:
                c.execute("SELECT Telefon FROM Pacjenci WHERE ID=?", (pacjent_id,))
                pacjent = c.fetchone()
                if pacjent:
                    telefon = pacjent[0]
                    tresc = f"⏰ Przypomnienie: Twoja wizyta o {godzina} dnia {data}. Prosimy o punktualność!"
                    wyslij_sms(telefon, tresc)
                    c.execute("UPDATE Wizyty SET PrzypomnienieWyslane=1 WHERE ID=?", (id_wizyty,))
                    conn_local.commit()

def przypomnienia_loop():
    while True:
        wyslij_przypomnienie()
        time.sleep(60)  # sprawdzaj co minutę

import sqlite3
from datetime import datetime, timedelta

DB_PATH = r"C:\Users\jacek\Desktop\przychodniaapp\przychodnia.db"

def rezerwacja_prosta(imie_nazwisko, doktor, data, godzina, opis=""):
    """
    Szuka pacjenta i lekarza, sprawdza dostępne sloty,
    a jeśli się zgadza - rezerwuje wizytę.
    Zwraca True jeśli zarezerwowano pomyślnie,
    ValueError w razie błędu (nieistniejący pacjent/lekarz,
    brak godzin pracy, zajęty termin itp.).
    """
    import sqlite3
    from datetime import datetime, timedelta

    DB_PATH = "przychodnia.db"

    # 1) Normalizacja godziny
    if ":" in godzina:
        h, m = godzina.split(":")
        godzina = f"{h.zfill(2)}:{m.zfill(2)}"
    else:
        godzina = godzina.zfill(2) + ":00"

    # 2) Rozbij imiona i nazwiska
    pac_parts = imie_nazwisko.strip().split(maxsplit=1)
    if len(pac_parts) < 2:
        raise ValueError("Niepoprawne imię i nazwisko pacjenta")
    pac_imie, pac_nazwisko = pac_parts

    doc_parts = doktor.strip().split(maxsplit=1)
    if len(doc_parts) < 2:
        raise ValueError("Niepoprawne imię i nazwisko lekarza")
    dok_imie, dok_nazwisko = doc_parts

    # 3) Połącz z bazą
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # DEBUG krok 2: wypisz wszystkie unikalne wartości DzienTygodnia
    c.execute("SELECT DISTINCT DzienTygodnia FROM GodzinyPracyLekarzy")
    print("DEBUG – dostępne DzienTygodnia w tabeli:", c.fetchall())

    # 4) Szukaj pacjenta
    c.execute(
        "SELECT id FROM pacjenci WHERE lower(imie)=? AND lower(nazwisko)=?",
        (pac_imie.lower(), pac_nazwisko.lower())
    )
    row = c.fetchone()
    if not row:
        conn.close()
        raise ValueError(f"Pacjent '{imie_nazwisko}' nie istnieje")
    pacjent_id = row[0]

    # 5) Szukaj lekarza i pobierz czas wizyty
    c.execute(
        "SELECT id, czas_wizyty FROM Lekarze WHERE lower(imie)=? AND lower(nazwisko)=?",
        (dok_imie.lower(), dok_nazwisko.lower())
    )
    row = c.fetchone()
    if not row:
        conn.close()
        raise ValueError(f"Lekarz '{doktor}' nie istnieje")
    lekarz_id, duration = row
    # zamieniamy int na 8-bajtowy BLOB little-endian
    blob_lekarz_id = struct.pack('<Q', lekarz_id)
    if not duration:
        duration = 30

    # 6) Ustal dzień tygodnia
    weekdays = {
        0: "Poniedziałek", 1: "Wtorek", 2: "Środa",
        3: "Czwartek",   4: "Piątek",  5: "Sobota",
        6: "Niedziela"
    }
    date_obj = datetime.strptime(data, "%Y-%m-%d").date()
    dzien = weekdays[date_obj.weekday()]

    # 7) Pobierz godziny pracy
    c.execute(
        "SELECT GodzinaOd, GodzinaDo FROM GodzinyPracyLekarzy "
        "WHERE LekarzID=? AND DzienTygodnia=?",
        (blob_lekarz_id, dzien)
    )
    work_periods = c.fetchall()

    # DEBUG: pełna tabela dla lekarza
    c.execute(
        "SELECT DzienTygodnia, GodzinaOd, GodzinaDo "
        "FROM GodzinyPracyLekarzy WHERE LekarzID=?",
        (blob_lekarz_id,)
    )
    print(f"DEBUG: pełna tabela GodzinyPracyLekarzy dla {doktor}:", c.fetchall())

    print(f"DEBUG: pełna tabela GodzinyPracyLekarzy dla {doktor}:", c.fetchall())
    # ——————————————————————

    if not work_periods:
        conn.close()
        raise ValueError(
            f"Brak godzin pracy dla {doktor} w dniu {dzien}"
        )

    # 8) Pobierz zajęte godziny
    c.execute(
        "SELECT Godzina FROM Wizyty WHERE LekarzID=? AND Data=? "
        "AND Status!='Odwołana'",
        (lekarz_id, data)
    )
    occupied = {r[0] for r in c.fetchall()}

    # 9) Wygeneruj listę wolnych slotów
    wolne = []
    for godz_od, godz_do in work_periods:
        start = datetime.strptime(godz_od, "%H:%M")
        end   = datetime.strptime(godz_do, "%H:%M")
        delta = timedelta(minutes=duration)
        curr  = start
        while curr + delta <= end:
            slot = curr.strftime("%H:%M")
            if slot not in occupied:
                wolne.append(slot)
            curr += delta

    if godzina not in wolne:
        conn.close()
        raise ValueError(f"Termin {data} {godzina} niedostępny")

    # 10) Zapisz wizytę
    c.execute(
        "INSERT INTO Wizyty "
        "(PacjentID, LekarzID, Data, Godzina, Opis, Status, Zrodlo) "
        "VALUES (?,?,?,?,?,?,?)",
        (pacjent_id, lekarz_id, data, godzina,
         opis, "Zaplanowana", "Bot_SMS")
    )
    conn.commit()
    conn.close()

    return True


# Pasek menu
menu_html = f"""
<style>
header, .stDeployButton, .viewerBadge_link__1S137,
[data-testid="stAppViewContainer"] > header {{
  visibility: hidden !important;
  height: 0 !important;
  margin: 0; padding: 0;
}}

.menu-bar {{
  background-color: white;
  padding: 5px 10px;
  display: flex;
  align-items: center;
  gap: 2px;
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  z-index: 1000;
  height: 48px;  /* 💡 kompaktowa belka */
  box-shadow: 0 1px 5px rgba(0,0,0,0.1);
}}

.menu-logo svg {{
  margin-right: 2px !important;  /* przy logo */
  height: 150px;
  width: auto;
  display: block;
  position: relative;
  top: 3px;
}}


.menu-btn {{
  background: none;
  border: none;
  font-size: 18px;
  cursor: pointer;
  text-decoration: none !important;
  color: #333 !important;
  padding: 6px 10px;
  box-sizing: border-box;
  transition: background 0.1s;
}}

.menu-btn:hover {{
  color: #007acc;
}}

.active {{
  color: #007acc;
}}

.menu-bar a {{
  text-decoration: none !important;
  color: inherit;
}}

.menu-bar a:hover {{
  background-color: #e6f0ff;
  border-radius: 6px;
  padding: 6px 10px;
}}

.stApp {{
  background-color: white !important;
}}

p, div, h1, h2, h3, h4, h5, h6, span {{
  color: black !important;
}}

.block-container {{
  color: black !important;
}}
</style>

<div class="menu-bar">
<div class="menu-logo">
  <svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="500" zoomAndPan="magnify" viewBox="0 0 375 374.999991" height="500" preserveAspectRatio="xMidYMid meet" version="1.2"><defs><linearGradient x1="-1.428592" gradientTransform="matrix(1,0.0000000000412406,-0.0000000000412406,1,0.00000269493,-0.00000250119)" y1="10.037" x2="209.694188" gradientUnits="userSpaceOnUse" y2="10.037" id="31b83d8ecf"><stop style="stop-color:#000000;stop-opacity:1;" offset="0"/><stop style="stop-color:#000000;stop-opacity:1;" offset="0.0078125"/><stop style="stop-color:#010101;stop-opacity:1;" offset="0.0117188"/><stop style="stop-color:#020202;stop-opacity:1;" offset="0.015625"/><stop style="stop-color:#020202;stop-opacity:1;" offset="0.0195313"/><stop style="stop-color:#030303;stop-opacity:1;" offset="0.0234375"/><stop style="stop-color:#040404;stop-opacity:1;" offset="0.0273438"/><stop style="stop-color:#040404;stop-opacity:1;" offset="0.03125"/><stop style="stop-color:#050505;stop-opacity:1;" offset="0.0351562"/><stop style="stop-color:#060606;stop-opacity:1;" offset="0.0390625"/><stop style="stop-color:#060606;stop-opacity:1;" offset="0.0429688"/><stop style="stop-color:#070707;stop-opacity:1;" offset="0.046875"/><stop style="stop-color:#080808;stop-opacity:1;" offset="0.0507813"/><stop style="stop-color:#080808;stop-opacity:1;" offset="0.0546875"/><stop style="stop-color:#090909;stop-opacity:1;" offset="0.0585938"/><stop style="stop-color:#0a0a0a;stop-opacity:1;" offset="0.0625"/><stop style="stop-color:#0a0a0a;stop-opacity:1;" offset="0.0664063"/><stop style="stop-color:#0b0b0b;stop-opacity:1;" offset="0.0703125"/><stop style="stop-color:#0c0c0c;stop-opacity:1;" offset="0.0742188"/><stop style="stop-color:#0c0c0c;stop-opacity:1;" offset="0.078125"/><stop style="stop-color:#0d0d0d;stop-opacity:1;" offset="0.0820313"/><stop style="stop-color:#0e0e0e;stop-opacity:1;" offset="0.0859375"/><stop style="stop-color:#0e0e0e;stop-opacity:1;" offset="0.0898438"/><stop style="stop-color:#0f0f0f;stop-opacity:1;" offset="0.09375"/><stop style="stop-color:#101010;stop-opacity:1;" offset="0.0976563"/><stop style="stop-color:#101010;stop-opacity:1;" offset="0.101563"/><stop style="stop-color:#111111;stop-opacity:1;" offset="0.105469"/><stop style="stop-color:#121212;stop-opacity:1;" offset="0.109375"/><stop style="stop-color:#121212;stop-opacity:1;" offset="0.113281"/><stop style="stop-color:#131313;stop-opacity:1;" offset="0.117188"/><stop style="stop-color:#141414;stop-opacity:1;" offset="0.121094"/><stop style="stop-color:#141414;stop-opacity:1;" offset="0.125"/><stop style="stop-color:#151515;stop-opacity:1;" offset="0.128906"/><stop style="stop-color:#161616;stop-opacity:1;" offset="0.132813"/><stop style="stop-color:#161616;stop-opacity:1;" offset="0.136719"/><stop style="stop-color:#171717;stop-opacity:1;" offset="0.140625"/><stop style="stop-color:#181818;stop-opacity:1;" offset="0.144531"/><stop style="stop-color:#181818;stop-opacity:1;" offset="0.148438"/><stop style="stop-color:#191919;stop-opacity:1;" offset="0.152344"/><stop style="stop-color:#1a1a1a;stop-opacity:1;" offset="0.15625"/><stop style="stop-color:#1a1a1a;stop-opacity:1;" offset="0.160156"/><stop style="stop-color:#1b1b1b;stop-opacity:1;" offset="0.164062"/><stop style="stop-color:#1c1c1c;stop-opacity:1;" offset="0.167969"/><stop style="stop-color:#1c1c1c;stop-opacity:1;" offset="0.171875"/><stop style="stop-color:#1d1d1d;stop-opacity:1;" offset="0.175781"/><stop style="stop-color:#1e1e1e;stop-opacity:1;" offset="0.179688"/><stop style="stop-color:#1e1e1e;stop-opacity:1;" offset="0.183594"/><stop style="stop-color:#1f1f1f;stop-opacity:1;" offset="0.1875"/><stop style="stop-color:#202020;stop-opacity:1;" offset="0.191406"/><stop style="stop-color:#212121;stop-opacity:1;" offset="0.195312"/><stop style="stop-color:#212121;stop-opacity:1;" offset="0.199219"/><stop style="stop-color:#222222;stop-opacity:1;" offset="0.203125"/><stop style="stop-color:#232323;stop-opacity:1;" offset="0.207031"/><stop style="stop-color:#232323;stop-opacity:1;" offset="0.210938"/><stop style="stop-color:#242424;stop-opacity:1;" offset="0.214844"/><stop style="stop-color:#252525;stop-opacity:1;" offset="0.21875"/><stop style="stop-color:#252525;stop-opacity:1;" offset="0.222656"/><stop style="stop-color:#262626;stop-opacity:1;" offset="0.226562"/><stop style="stop-color:#272727;stop-opacity:1;" offset="0.230469"/><stop style="stop-color:#272727;stop-opacity:1;" offset="0.234375"/><stop style="stop-color:#282828;stop-opacity:1;" offset="0.238281"/><stop style="stop-color:#292929;stop-opacity:1;" offset="0.242188"/><stop style="stop-color:#292929;stop-opacity:1;" offset="0.246094"/><stop style="stop-color:#2a2a2a;stop-opacity:1;" offset="0.25"/><stop style="stop-color:#2b2b2b;stop-opacity:1;" offset="0.253906"/><stop style="stop-color:#2b2b2b;stop-opacity:1;" offset="0.257813"/><stop style="stop-color:#2c2c2c;stop-opacity:1;" offset="0.261719"/><stop style="stop-color:#2d2d2d;stop-opacity:1;" offset="0.265625"/><stop style="stop-color:#2d2d2d;stop-opacity:1;" offset="0.269531"/><stop style="stop-color:#2e2e2e;stop-opacity:1;" offset="0.273438"/><stop style="stop-color:#2f2f2f;stop-opacity:1;" offset="0.277344"/><stop style="stop-color:#2f2f2f;stop-opacity:1;" offset="0.28125"/><stop style="stop-color:#303030;stop-opacity:1;" offset="0.285156"/><stop style="stop-color:#313131;stop-opacity:1;" offset="0.289063"/><stop style="stop-color:#313131;stop-opacity:1;" offset="0.292969"/><stop style="stop-color:#323232;stop-opacity:1;" offset="0.296875"/><stop style="stop-color:#333333;stop-opacity:1;" offset="0.300781"/><stop style="stop-color:#333333;stop-opacity:1;" offset="0.304688"/><stop style="stop-color:#343434;stop-opacity:1;" offset="0.308594"/><stop style="stop-color:#353535;stop-opacity:1;" offset="0.3125"/><stop style="stop-color:#353535;stop-opacity:1;" offset="0.316406"/><stop style="stop-color:#363636;stop-opacity:1;" offset="0.320313"/><stop style="stop-color:#373737;stop-opacity:1;" offset="0.324219"/><stop style="stop-color:#373737;stop-opacity:1;" offset="0.328125"/><stop style="stop-color:#383838;stop-opacity:1;" offset="0.332031"/><stop style="stop-color:#393939;stop-opacity:1;" offset="0.335938"/><stop style="stop-color:#393939;stop-opacity:1;" offset="0.339844"/><stop style="stop-color:#3a3a3a;stop-opacity:1;" offset="0.34375"/><stop style="stop-color:#3b3b3b;stop-opacity:1;" offset="0.347656"/><stop style="stop-color:#3b3b3b;stop-opacity:1;" offset="0.351563"/><stop style="stop-color:#3c3c3c;stop-opacity:1;" offset="0.355469"/><stop style="stop-color:#3d3d3d;stop-opacity:1;" offset="0.359375"/><stop style="stop-color:#3d3d3d;stop-opacity:1;" offset="0.363281"/><stop style="stop-color:#3e3e3e;stop-opacity:1;" offset="0.367188"/><stop style="stop-color:#3f3f3f;stop-opacity:1;" offset="0.371094"/><stop style="stop-color:#3f3f3f;stop-opacity:1;" offset="0.375"/><stop style="stop-color:#404040;stop-opacity:1;" offset="0.378906"/><stop style="stop-color:#414141;stop-opacity:1;" offset="0.382813"/><stop style="stop-color:#414141;stop-opacity:1;" offset="0.386719"/><stop style="stop-color:#424242;stop-opacity:1;" offset="0.390625"/><stop style="stop-color:#434343;stop-opacity:1;" offset="0.394531"/><stop style="stop-color:#444444;stop-opacity:1;" offset="0.398438"/><stop style="stop-color:#444444;stop-opacity:1;" offset="0.402344"/><stop style="stop-color:#454545;stop-opacity:1;" offset="0.40625"/><stop style="stop-color:#464646;stop-opacity:1;" offset="0.410156"/><stop style="stop-color:#464646;stop-opacity:1;" offset="0.414063"/><stop style="stop-color:#474747;stop-opacity:1;" offset="0.417969"/><stop style="stop-color:#484848;stop-opacity:1;" offset="0.421875"/><stop style="stop-color:#484848;stop-opacity:1;" offset="0.425781"/><stop style="stop-color:#494949;stop-opacity:1;" offset="0.429688"/><stop style="stop-color:#4a4a4a;stop-opacity:1;" offset="0.433594"/><stop style="stop-color:#4a4a4a;stop-opacity:1;" offset="0.4375"/><stop style="stop-color:#4b4b4b;stop-opacity:1;" offset="0.441406"/><stop style="stop-color:#4c4c4c;stop-opacity:1;" offset="0.445313"/><stop style="stop-color:#4c4c4c;stop-opacity:1;" offset="0.449219"/><stop style="stop-color:#4d4d4d;stop-opacity:1;" offset="0.453125"/><stop style="stop-color:#4e4e4e;stop-opacity:1;" offset="0.457031"/><stop style="stop-color:#4e4e4e;stop-opacity:1;" offset="0.460938"/><stop style="stop-color:#4f4f4f;stop-opacity:1;" offset="0.464844"/><stop style="stop-color:#505050;stop-opacity:1;" offset="0.46875"/><stop style="stop-color:#505050;stop-opacity:1;" offset="0.472656"/><stop style="stop-color:#515151;stop-opacity:1;" offset="0.476563"/><stop style="stop-color:#525252;stop-opacity:1;" offset="0.480469"/><stop style="stop-color:#525252;stop-opacity:1;" offset="0.484375"/><stop style="stop-color:#535353;stop-opacity:1;" offset="0.488281"/><stop style="stop-color:#545454;stop-opacity:1;" offset="0.492188"/><stop style="stop-color:#545454;stop-opacity:1;" offset="0.496094"/><stop style="stop-color:#555555;stop-opacity:1;" offset="0.5"/><stop style="stop-color:#555555;stop-opacity:1;" offset="1"/></linearGradient><clipPath id="8144bce860"><path d="M 52.320312 149 L 334 149 L 334 204.816406 L 52.320312 204.816406 Z M 52.320312 149 "/></clipPath></defs><g id="672c5a81ff"><path style="fill:none;stroke-width:4;stroke-linecap:butt;stroke-linejoin:miter;stroke:url(#31b83d8ecf);stroke-miterlimit:4;" d="M 0.596879 18.162413 C 69.531333 -3.386546 138.465331 -3.387435 207.404032 18.165037 " transform="matrix(-0.747602,0.00962367,-0.00962367,-0.747602,201.824142,223.099848)"/><g style="fill:#ffffff;fill-opacity:1;"><g transform="translate(45.842584, 304.273704)"><path style="stroke:none" d="M 46.5 0 L 43.15625 -26.5625 L 33.203125 0 L 26.5625 0 L 16.609375 -26.5625 L 13.296875 0 L 2.234375 0 L 7.765625 -46.5 L 18.828125 -46.5 L 29.90625 -18.828125 L 40.96875 -46.5 L 52.046875 -46.5 L 57.578125 0 Z M 46.5 0 "/></g></g><g style="fill:#ffffff;fill-opacity:1;"><g transform="translate(108.906049, 304.273704)"><path style="stroke:none" d="M 15.484375 -28.203125 L 30.96875 -28.203125 L 30.96875 -18.25 L 15.484375 -18.25 L 15.484375 -9.953125 L 34.328125 -9.953125 L 34.328125 0 L 4.421875 0 L 4.421875 -46.5 L 34.328125 -46.5 L 34.328125 -36.515625 L 15.484375 -36.515625 Z M 15.484375 -28.203125 "/></g></g><g style="fill:#ffffff;fill-opacity:1;"><g transform="translate(149.832562, 304.273704)"><path style="stroke:none" d="M 22.046875 -46.5 C 27.222656 -46.5 31.648438 -45.472656 35.328125 -43.421875 C 39.003906 -41.367188 41.785156 -38.585938 43.671875 -35.078125 C 45.554688 -31.566406 46.5 -27.625 46.5 -23.25 C 46.5 -18.90625 45.53125 -14.96875 43.59375 -11.4375 C 41.664062 -7.914062 38.847656 -5.128906 35.140625 -3.078125 C 31.441406 -1.023438 27.078125 0 22.046875 0 L 4.421875 0 L 4.421875 -46.5 Z M 22.859375 -9.953125 C 24.992188 -9.953125 27.03125 -10.550781 28.96875 -11.75 C 30.90625 -12.957031 32.46875 -14.582031 33.65625 -16.625 C 34.84375 -18.664062 35.4375 -20.875 35.4375 -23.25 C 35.4375 -25.632812 34.84375 -27.84375 33.65625 -29.875 C 32.46875 -31.914062 30.90625 -33.53125 28.96875 -34.71875 C 27.03125 -35.914062 24.992188 -36.515625 22.859375 -36.515625 L 15.484375 -36.515625 L 15.484375 -9.953125 Z M 22.859375 -9.953125 "/></g></g><g style="fill:#ffffff;fill-opacity:1;"><g transform="translate(201.827552, 304.273704)"><path style="stroke:none" d="M 4.421875 -46.5 L 15.484375 -46.5 L 15.484375 0 L 4.421875 0 Z M 4.421875 -46.5 "/></g></g><g style="fill:#ffffff;fill-opacity:1;"><g transform="translate(225.035581, 304.273704)"><path style="stroke:none" d="M 15.078125 -7.765625 L 12.1875 0 L 0 0 L 19.234375 -46.5 L 29.453125 -46.5 L 48.6875 0 L 36.515625 0 L 33.609375 -7.765625 Z M 24.375 -32.09375 L 18.875 -17.71875 L 29.8125 -17.71875 Z M 24.375 -32.09375 "/></g></g><g style="fill:#ffffff;fill-opacity:1;"><g transform="translate(277.030571, 304.273704)"><path style="stroke:none" d="M 47.578125 0 L 36.515625 0 L 15.484375 -28.78125 L 15.484375 0 L 4.421875 0 L 4.421875 -46.5 L 15.484375 -46.5 L 36.515625 -17.71875 L 36.515625 -46.5 L 47.578125 -46.5 Z M 47.578125 0 "/></g></g><g clip-rule="nonzero" clip-path="url(#8144bce860)"><g mask="url(#4077152b6b)" transform="matrix(0.275114,0,0,0.275114,52.321868,148.691954)"><image width="1024" xlink:href="data:image/png;base64,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" height="204" preserveAspectRatio="xMidYMid meet"/><mask id="4077152b6b"><g><image width="1024" xlink:href="data:image/png;base64,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" height="204" preserveAspectRatio="xMidYMid meet"/></g></mask></g></g></g></svg>
</div>

  <a href="/?menu=start" target="_self" class="menu-btn {{'active' if menu=='start' else ''}}">
    <img
      src="{svg_data_uri('icons/icons8-house.svg')}"
      width="18"
      style="vertical-align: middle; margin-right: 6px;"
    />
    Start
  </a>

  <a href="/?menu=rezerwacja" target="_self" class="menu-btn {{'active' if menu=='rezerwacja' else ''}}">
    <img
      src="{svg_data_uri('icons/icons8-plus.svg')}"
      width="18"
      style="vertical-align: middle; margin-right: 6px;"
    />
    Rezerwacja
  </a>

  <a href="/?menu=wizyty" target="_self" class="menu-btn {{'active' if menu=='wizyty' else ''}}">
    <img
      src="{svg_data_uri('icons/9069758_insert_table_icon.svg')}"
      width="18"
      style="vertical-align: middle; margin-right: 6px;"
    />
    Wizyty
  </a>

  <a href="/?menu=przypomnienia" target="_self" class="menu-btn {{'active' if menu=='przypomnienia' else ''}}">
    <img
      src="{svg_data_uri('icons/1564519_bell_new_note_notifications_icon.svg')}"
      width="18"
      style="vertical-align: middle; margin-right: 6px;"
    />
    Przypomnienia
  </a>

  <a href="/?menu=pacjenci" target="_self" class="menu-btn {{'active' if menu=='pacjenci' else ''}}">
    <img
      src="{svg_data_uri('icons/4265044_community_conversation_friends_group_people_icon.svg')}"
      width="18"
      style="vertical-align: middle; margin-right: 6px;"
    />
    Pacjenci
  </a>

  <a href="/?menu=ustawienia" target="_self" class="menu-btn {{'active' if menu=='ustawienia' else ''}}">
    <img
      src="{svg_data_uri('icons/1564529_mechanism_options_settings_configuration_setting_icon (1).svg')}"
      width="18"
      style="vertical-align: middle; margin-right: 6px;"
    />
    Ustawienia
  </a>

</div>
"""

st.markdown(menu_html, unsafe_allow_html=True)

# Przykład wyświetlenia zawartości zakładki
if menu == "start":
    st.title("Panel główny")
elif menu == "rezerwacja":
    st.title("📅 Rezerwacja wizyty")
elif menu == "wizyty":
    st.title("📖 Lista wizyt")
elif menu == "przypomnienia":
    st.title("⏰ Przypomnienia SMS")
elif menu == "pacjenci":
    st.title("👤 Pacjenci")
elif menu == "ustawienia":
    st.title("⚙️ Ustawienia systemu")



if menu == "start":
    col1, col2 = st.columns([1, 1])

    # 1) liczymy anulowane
    anulowane = pd.read_sql(
        "SELECT COUNT(*) AS cnt FROM Wizyty WHERE Status='Anulowana'", conn
    )["cnt"][0]

    # 2) zaciągamy zaplanowane + czas wizyty lekarza
    wizyty_plan = pd.read_sql("""
        SELECT W.Data, W.Godzina, L.Czas_Wizyty
        FROM Wizyty W
        JOIN Lekarze L ON W.LekarzID=L.ID
        WHERE W.Status='Zaplanowana'
    """, conn)

    # 3) obliczamy start_dt i end_dt
    wizyty_plan["start_dt"] = pd.to_datetime(
        wizyty_plan["Data"] + " " + wizyty_plan["Godzina"]
    )
    wizyty_plan["Czas_Wizyty"] = wizyty_plan["Czas_Wizyty"].astype(int)
    wizyty_plan["end_dt"] = (
        wizyty_plan["start_dt"]
        + pd.to_timedelta(wizyty_plan["Czas_Wizyty"], unit="m")
    )

    teraz = datetime.now()

    # 4) dynamiczne zakończone i w trakcie
    zakończone_din = wizyty_plan[wizyty_plan["end_dt"] <= teraz].shape[0]
    w_trakcie      = wizyty_plan[
                        (wizyty_plan["start_dt"] <= teraz) &
                        (wizyty_plan["end_dt"]   >  teraz)
                     ].shape[0]

    # 5) dodaj historyczne zakończone z bazy
    z_kbazy = pd.read_sql(
        "SELECT COUNT(*) AS cnt FROM Wizyty WHERE Status='Zakończona'", conn
    )["cnt"][0]
    zakończone = z_kbazy + zakończone_din

    def skala(v):
        import math
        MAX_WYSOKOSC = 280  # np. 280px maksymalnej wysokości słupka
        MAX_LOG = math.log(1 + 100)  # zakładamy że 100 wizyt to „górna granica”

        # logarytmiczna skala spowalniająca wzrost słupka
        # POPRAWKA WCIĘĆ: Ta linia musi być wcięta na ten sam poziom, co reszta funkcji!
        return int(MAX_WYSOKOSC * math.log(1 + v) / MAX_LOG)

        target_zak = skala(zakończone)
        target_wtr = skala(w_trakcie)
        target_anul = skala(anulowane)
        anim_dur = 0.6
        start_delay = 80
        stagger_ms = 80
		
    # Zakładam, że ten blok `with col1:` jest na poziomie, z którego został wywołany
    with col1:
        # --- Poprawnie opakowany pierwszy widget (wykres słupkowy) --- #

        import os
        is_streamlit_cloud = os.getenv("STREAMLIT_SERVER_HOST") is not None

        if is_streamlit_cloud:
            st.markdown("""
            <style>
            /* przykładowe selektory: dopasuj do elementów które zasłaniają widżet.
               Nie usuwamy nic z repo — tylko nadpisujemy styl przy uruchomieniu w Cloud */
            /* ukryje surowy blok <pre> / <code> (jeśli to on zasłania) */
            div[data-testid="stMarkdownContainer"] pre,
            div[data-testid="stMarkdownContainer"] code {
                display: none !important;
                visibility: hidden !important;
                height: 0 !important;
                margin: 0 !important;
                padding: 0 !important;
            }

            /* jeśli widzisz konkretne klasy/ID w DevTools, dodaj je tutaj zamiast powyższych */
            /* np. #debug-code { display: none !important; } */

            /* upewnij się, że nie ukrywasz elementów .bar-label etc. */
            </style>
            """, unsafe_allow_html=True)

	
        st.markdown(f"""
        <style> 
        .bar-widget-wrapper {{ position: relative; z-index: 5; }}
        .bar-container {{
          display:flex;
          align-items:flex-end;
          height:320px;
          gap:34px;
          padding:20px;
          border-radius: 8px;
          box-shadow: 0 0 12px rgba(0,0,0,0.12);
          background:linear-gradient(180deg,#fff,#fbfbff);
        }}
        .bar-item {{ text-align:center; width:80px;}}
        .bar-value {{ font-weight:bold; margin-bottom:6px; font-size:16px; color:#222;}}
        .bar {{
          width:50px;
          margin: 0 auto;
          background-image: linear-gradient(to top,#7426ef,#e333dc);
          border-radius:6px;
          height:0px;
          transition: height {{anim_dur}}s cubic-bezier(.2,.9,.2,1);
          will-change: height;
          box-shadow: inset 0 -8px 18px rgba(0,0,0,0.06);
        }}
        .bar-label {{
          margin-top:8px;
          white-space: normal;
          text-overflow: clip;
          overflow: visible;
          font-size:14px;
          color:#333;
        }}
        /* zabezpieczenie: tylko wewnątrz wrappera nadpisujemy overflow, nie globalnie */
        .bar-widget-wrapper * {{
          white-space: normal !important;
          text-overflow: clip !important;
          overflow: visible !important;
        }}
        </style>

        <div class="bar-widget-wrapper">
          <div class="bar-container">
            <div class="bar-item">
              <div class="bar-value">{zakończone}</div>
              <div class="bar" data-target="{target_zak}" id="bar-zak"></div>
              <div class="bar-label">Zakończone</div>
            </div>

            <div class="bar-item">
              <div class="bar-value">{w_trakcie}</div>
              <div class="bar" data-target="{target_wtr}" id="bar-wtr"></div>
              <div class="bar-label">W trakcie</div>
            </div>

            <div class="bar-item">
              <div class="bar-value">{anulowane}</div>
              <div class="bar" data-target="{target_anul}" id="bar-anul"></div>
              <div class="bar-label">Anulowane</div>
            </div>
          </div>
        </div>

        <script>
        (function() {{
          const startDelay = {start_delay};
          const stagger = {stagger_ms};
          function animateBar(id, delay) {{
            const el = document.getElementById(id);
            if (!el) return;
            const target = el.getAttribute('data-target') || '0';
            setTimeout(() => {{ 
            el.style.height = target + 'px'; 
            }}, delay);
          }}

          setTimeout(() => {{
            animateBar('bar-zak', 0);
            animateBar('bar-wtr', stagger);
            animateBar('bar-anul', stagger * 2);
          }}, startDelay);
        }})();
        </script>
        """, unsafe_allow_html=True) # POPRAWKA: Zamknięcie st.markdown

        # --- widget: nowe rezerwacje z bota ---
        bot_count = get_bot_count(conn) # TA LINIA JEST TERAZ POPRAWNIE WCIĘTA
    
        st.markdown(f"""
        <style>
        .bot-widget {{
          width: 180px;
          height: 180px;
          border-radius: 14px;
          background: linear-gradient(180deg, #ffffff 0%, #f7f7ff 100%);
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          margin-top: 18px;
          margin-left: 12px;
          box-shadow: 0 6px 18px rgba(0,0,0,0.06);
          border: 1px solid #eee;
        }}
        .bot-title {{
          font-size: 13px;
          color: #666;
          font-weight: 600;
          margin-bottom: 6px;
        }}
        .bot-number {{
          font-size: 56px;
          font-weight: 700;
          color: #7426ef;
          letter-spacing: -1px;
        }}
        </style>

        <div class="bot-widget">
          <div class="bot-title">Nowe rezerwacje z bota</div>
          <div class="bot-number">{bot_count}</div>
          <div style="font-size:12px;color:#999;margin-top:6px;">ostatnie 30 min</div>
        </div>
        """, unsafe_allow_html=True)
        # --- koniec widgetu ---


    with col2:
        st.markdown("""
        <div style="margin-left: 40px; font-size: 13px; font-weight: 600; max-width: 300px; margin: 0 0 12px 40px;">
          Najbliższe wizyty:
        </div>
        """, unsafe_allow_html=True)


        df = pd.read_sql("""
            SELECT W.Data, W.Godzina, L.Imie, L.Nazwisko
            FROM Wizyty AS W
            JOIN Lekarze AS L ON W.LekarzID=L.ID
            WHERE W.Status='Zaplanowana'
              AND datetime(W.Data || ' ' || W.Godzina) >= datetime('now')
            ORDER BY W.Data, W.Godzina
            LIMIT 3
        """, conn)

        if df.empty:
            st.markdown("""
            <div style="margin-left: 140px; color: #555;">
              Brak zaplanowanych wizyt.
            </div>
            """, unsafe_allow_html=True)
        else:
            teraz = datetime.now()
            for _, r in df.iterrows():
                dt = datetime.strptime(f"{r['Data']} {r['Godzina']}",
                                         "%Y-%m-%d %H:%M")
                mins = int((dt - teraz).total_seconds() // 60)
                st.markdown(f"""
                  <div style="
                      position: relative;
                      padding: 12px;
                      border-radius: 8px;
                      margin-bottom: 8px;
                      background: white;
                      max-width: 300px;
                      margin-left: 40px;
                      box-shadow: 0 0 0 1px #7426ef, 0 0 0 1px #e333dc;
                    ">
                      <strong>Wizyta u dr {r['Imie']} {r['Nazwisko']}</strong><br>
                      <span style="color: grey; font-size: 14px;">
                          Za {mins} minut
                      </span>
                  </div>
              """, unsafe_allow_html=True)
		  
elif menu == "rezerwacja":
    st.header("Rezerwacja wizyty")

    st.checkbox(
        "🔒 Blokuj wysyłkę SMS (tryb testowy)",
        key="blokuj_sms",
        value=True
    )

    pacjenci_df = pd.read_sql(
        "SELECT ID, Imie, Nazwisko FROM Pacjenci WHERE Active=1", conn
)
    lekarze_df = pd.read_sql(
        "SELECT ID, Imie, Nazwisko, Czas_Wizyty, KalendarzID FROM Lekarze WHERE Active=1",
        conn
)
    if pacjenci_df.empty or lekarze_df.empty:
        st.warning("Musisz mieć przynajmniej jednego pacjenta i lekarza w bazie.")
    else:
        pacjent_options = (pacjenci_df["Imie"] + " " + pacjenci_df["Nazwisko"]).tolist()
        lekarz_options = (lekarze_df["Imie"] + " " + lekarze_df["Nazwisko"]).tolist()

        pacjent_selected = st.selectbox("Wybierz pacjenta", pacjent_options)
        lekarz_selected = st.selectbox(
            "Wybierz lekarza",
            lekarz_options,
            key="selectbox_lekarz_rezerwacja"  # unikalny klucz dla selectbox w rezerwacji
        )
        data_input = st.date_input("Data wizyty", value=datetime.today())

        lekarz_id = lekarze_df.loc[
            (lekarze_df["Imie"] + " " + lekarze_df["Nazwisko"]) == lekarz_selected, "ID"
        ].values[0]

        # Zamiana angielskiego dnia na polski
        dni_ang_pl = {
            'Monday': 'Poniedziałek',
            'Tuesday': 'Wtorek',
            'Wednesday': 'Środa',
            'Thursday': 'Czwartek',
            'Friday': 'Piątek',
            'Saturday': 'Sobota',
            'Sunday': 'Niedziela'
        }
        dzien_tygodnia = dni_ang_pl[data_input.strftime('%A')]

        godziny_pracy_df = pd.read_sql("""
            SELECT GodzinaOd, GodzinaDo FROM GodzinyPracyLekarzy
            WHERE LekarzID=? AND DzienTygodnia=?
        """, conn, params=(lekarz_id, dzien_tygodnia))

        if godziny_pracy_df.empty:
            st.warning("Lekarz nie pracuje w tym dniu.")
        else:
            czas_wizyty = int(lekarze_df.loc[lekarze_df["ID"] == lekarz_id, "Czas_Wizyty"].values[0])
            godz_od = datetime.strptime(godziny_pracy_df.iloc[0]["GodzinaOd"], "%H:%M")
            godz_do = datetime.strptime(godziny_pracy_df.iloc[0]["GodzinaDo"], "%H:%M")

            wizyty_tego_dnia = pd.read_sql("""
                SELECT Godzina FROM Wizyty
                WHERE LekarzID=? AND Data=?
            """, conn, params=(lekarz_id, data_input.strftime('%Y-%m-%d')))

            zajete_godziny = wizyty_tego_dnia["Godzina"].tolist()

            available_times = []
            current_time = godz_od
            while current_time + timedelta(minutes=czas_wizyty) <= godz_do:
                godz_str = current_time.strftime("%H:%M")
                if godz_str not in zajete_godziny:
                    available_times.append(godz_str)
                current_time += timedelta(minutes=czas_wizyty)

            if available_times:
                godzina_input = st.selectbox("Dostępne godziny", available_times)
            else:
                godzina_input = st.text_input("Brak wolnych terminów — wpisz ręcznie (HH:MM)")

            opis_input = st.text_area("Opis wizyty (opcjonalny)")

        if st.button("Rezerwuj wizytę"):
            try:
                datetime.strptime(godzina_input, "%H:%M")
            except ValueError:
                st.error("Wpisz poprawną godzinę w formacie HH:MM")
                st.stop()

            if godzina_input in zajete_godziny:
                st.error("❌ Ten termin jest już zajęty. Wybierz inną godzinę.")
                st.stop()

         # 1) Konwertujemy datę na string YYYY-MM-DD
            data_str = data_input.strftime("%Y-%m-%d")

            service = get_calendar_service()

            lekarz = lekarze_df.loc[
                (lekarze_df["Imie"] + " " + lekarze_df["Nazwisko"]) == lekarz_selected
            ].iloc[0]

         # 2) Tworzymy event w Google Calendar i przechwytujemy zwrotkę
            event_body = {
                'summary': f"Wizyta: {pacjent_selected} – {data_str} {godzina_input}",
                'start': {'dateTime': f"{data_str}T{godzina_input}:00", 'timeZone': str(Config.TIMEZONE)},
                'end':   {'dateTime': (datetime.strptime(f"{data_str} {godzina_input}", "%Y-%m-%d %H:%M")
                              + timedelta(minutes=czas_wizyty)).isoformat(),
                          'timeZone': str(Config.TIMEZONE)}
            }
            event = service.events().insert(
                calendarId=lekarz['KalendarzID'],
                body=event_body
            ).execute()

            # 3) Teraz mamy data_str i event['id'], można INSERTować
            pacjent_id = int(
                pacjenci_df.loc[
                    (pacjenci_df["Imie"] + " " + pacjenci_df["Nazwisko"])
                    == pacjent_selected, "ID"
                ].iloc[0]
            )
            lekarz_id = int(
                lekarze_df.loc[
                    (lekarze_df["Imie"] + " " + lekarze_df["Nazwisko"])
                    == lekarz_selected, "ID"
                ].iloc[0]
            )

            conn.execute("""
                INSERT INTO Wizyty
                  (PacjentID, LekarzID, Data, Godzina, Status, EventID, Zrodlo, Opis)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pacjent_id,
                lekarz_id,
                data_str,
                godzina_input,
                "Zaplanowana",
                event["id"],
                "Bot",
                opis_input
            ))

            conn.commit()

            wyslij_sms_potwierdzenie(pacjent_id, data_str, godzina_input, conn)
            st.success("✅ Wizyta zarezerwowana.")
            st.session_state["menu"] = "wizyty"
            st.rerun()


elif menu == "wizyty":
    st.title("📖 Lista wizyt")

    # 0️⃣ Zainicjalizuj stan raz
    if "selected_wizyta" not in st.session_state:
        st.session_state.selected_wizyta = None

    # 1️⃣ Pokaż tabelę z wizytami i ustaw stan przy kliknięciu
    wizyty_df = pd.read_sql("""
        SELECT
            W.ID,
            L.Imie || ' ' || L.Nazwisko AS Lekarz,
            P.Imie || ' ' || P.Nazwisko AS Pacjent,
            W.Data,
            W.Godzina,
            W.Status,
            W.Zrodlo
        FROM Wizyty W
        LEFT JOIN Lekarze  L ON W.LekarzID  = L.ID
        LEFT JOIN Pacjenci P ON W.PacjentID = P.ID
        ORDER BY datetime(W.Data || ' ' || W.Godzina) ASC
    """, conn)

    if wizyty_df.empty:
        st.info("Brak wizyt do wyświetlenia.")
    else:
        for _, row in wizyty_df.iterrows():
            cols = st.columns([3,3,2,2,1])
            if row["Zrodlo"] == "Bot_SMS":
                badge_html = (
                    "<span style='position:relative; display:inline-block;'>"
                    "<span style='position:absolute; top:-2px; left:-28px; "
                    "background:orange; color:white; padding:1px 4px; border-radius:3px; "
                    "font-size:0.5em; font-weight:bold;'>BOT</span>"
                    f"{row['Lekarz']}"
                    "</span>"
                )
                cols[0].markdown(badge_html, unsafe_allow_html=True)
            else:
                cols[0].write(row["Lekarz"])

            cols[1].write(row["Pacjent"])
            cols[2].write(row["Data"])
            cols[3].write(row["Godzina"])
            if cols[4].button("Szczegóły", key=f"szcz_{row['ID']}"):
                st.session_state.selected_wizyta = row["ID"]


    # 2️⃣ Po rerunie, jeśli coś wybrano, wyświetl szczegóły
    vid = st.session_state.selected_wizyta
    if vid is not None:
        detail_df = pd.read_sql(
            """
            SELECT
              W.ID,
              L.Imie || ' ' || L.Nazwisko   AS Lekarz,
              P.Imie || ' ' || P.Nazwisko   AS Pacjent,
              P.Telefon                     AS TelefonPacjenta,
              W.Data,
              W.Godzina,
              W.Status,
              W.Opis
              W.Zrodlo
            FROM Wizyty W
            LEFT JOIN Lekarze  L ON W.LekarzID  = L.ID
            LEFT JOIN Pacjenci P ON W.PacjentID = P.ID
            WHERE W.ID=?
            """,
            conn,
            params=(vid,)
        )

        if not detail_df.empty:
            w = detail_df.iloc[0]

            st.markdown("---")
            st.header("🔍 Szczegóły wizyty")

            w = detail_df.iloc[0]
            badge = (
                "<span style='background:orange;color:white;"
                "padding:2px 4px;border-radius:3px;font-size:0.8em;'>BOT</span> "
                if w["Zrodlo"]=="Bot_SMS" else ""
            )
            st.markdown(f"{badge}{w['Lekarz']}", unsafe_allow_html=True)

            st.write("**Lekarz:**",           w["Lekarz"])
            st.write("**Pacjent:**",          w["Pacjent"])
            st.write("**Telefon pacjenta:**", w["TelefonPacjenta"])
            st.write("**Data i godzina:**",   f"{w['Data']} {w['Godzina']}")
            st.write("**Status:**",           w["Status"])
            opis = w["Opis"]
            st.write("**Opis wizyty:**", opis if opis and opis.strip() else "—")


            # Przycisk ukrycia szczegółów resetuje stan
            if st.button("← Ukryj szczegóły"):
                st.session_state.selected_wizyta = None

            # anulowanie
            if w["Status"] != "Anulowana" and st.button("❌ Anuluj wizytę"):
                conn.execute(
                    "UPDATE Wizyty SET Status='Anulowana' WHERE ID=?", (vid,)
                )
                conn.commit()
                st.success("Wizyta została anulowana.")
                st.session_state.selected_wizyta = None
                st.stop()

            # usuwanie (tylko gdy anulowana)
            if w["Status"] == "Anulowana" and st.button("🗑️ Usuń wizytę"):
                conn.execute("DELETE FROM Wizyty WHERE ID=?", (vid,))
                conn.commit()
                st.success("Wizyta została usunięta.")
                st.session_state.selected_wizyta = None
                st.stop()


elif menu == "przypomnienia":
    st.header("Tabela przypomnień")
    przypomnienia_df = pd.read_sql("SELECT * FROM Wizyty WHERE PrzypomnienieWyslane=1", conn)
    st.dataframe(przypomnienia_df)


elif menu == "pacjenci":
    st.title("Rejestracja pacjenta")

    imie = st.text_input("Imię")
    nazwisko = st.text_input("Nazwisko")
    pesel = st.text_input("PESEL")
    telefon = st.text_input("Telefon")

    if st.button("Zarejestruj pacjenta"):
        if not imie or not nazwisko or not pesel or not telefon:
            st.error("Wypełnij wszystkie pola!")
        else:
            c = conn.cursor()
            c.execute("INSERT INTO Pacjenci (Imie, Nazwisko, Telefon, PESEL) VALUES (?, ?, ?, ?)",
                      (imie, nazwisko, telefon, pesel))
            conn.commit()
            st.success(f"Dodano pacjenta: {imie} {nazwisko}")

    search = st.text_input("Wyszukaj pacjenta po imieniu lub nazwisku")
   # zamiast SELECT * FROM Pacjenci
    df_pacjenci = pd.read_sql(
        "SELECT ID, Imie, Nazwisko, Telefon, PESEL FROM Pacjenci WHERE Active=1",
        conn
    )


    if search:
        df_pacjenci = df_pacjenci[
            df_pacjenci["Imie"].str.contains(search, case=False) |
            df_pacjenci["Nazwisko"].str.contains(search, case=False)
        ]

    if df_pacjenci.empty:
        st.info("Brak pacjentów do wyświetlenia.")
    else:
        for _, row in df_pacjenci.iterrows():
            col1, col2, col3 = st.columns([4, 3, 3])
            with col1:
                link = f"/?menu=pacjent_szczegoly&pacjent_id={row['ID']}"
                st.markdown(f"<a href='{link}' target='_self'>{row['Imie']} {row['Nazwisko']}</a>", unsafe_allow_html=True)
            col2.write(row["Telefon"])
            col3.write(row["PESEL"])

elif menu == "pacjent_szczegoly":
    pacjent_id = st.query_params.get("pacjent_id", None)
    if pacjent_id is None:
        st.error("Brak ID pacjenta.")
        st.stop()

    pacjent_id = int(pacjent_id)

    # Dane pacjenta
    pacjent = pd.read_sql("SELECT * FROM Pacjenci WHERE ID=?", conn, params=(pacjent_id,))
    if pacjent.empty:
        st.error("Nie znaleziono pacjenta.")
        st.stop()
    pacjent = pacjent.iloc[0]
    st.header(f"Szczegóły: {pacjent['Imie']} {pacjent['Nazwisko']}")

    # Wizyty pacjenta
    wizyty = pd.read_sql("""
        SELECT Wizyty.ID, Lekarze.Imie || ' ' || Lekarze.Nazwisko AS Lekarz,
               Wizyty.Data, Wizyty.Godzina, Wizyty.Status
        FROM Wizyty
        JOIN Lekarze ON Wizyty.LekarzID = Lekarze.ID
        WHERE Wizyty.PacjentID=?
        ORDER BY datetime(Data || ' ' || Godzina)
    """, conn, params=(pacjent_id,))
    if wizyty.empty:
        st.info("Pacjent nie ma jeszcze wizyt.")
    else:
        st.subheader("Wizyty pacjenta")
        st.dataframe(wizyty)


    # Powrót do listy pacjentów
    st.markdown(f"<a href='/?menu=pacjenci' target='_self'>⬅️ Powrót</a>", unsafe_allow_html=True)

    st.header(f"Szczegóły pacjenta: {pacjent['Imie']} {pacjent['Nazwisko']}")
    st.write(f"Telefon: {pacjent['Telefon']}")
    st.write(f"PESEL: {pacjent['PESEL']}")

    wizyty_df = pd.read_sql("""
    SELECT
        Wizyty.Data,
        Wizyty.Godzina,
        Lekarze.Imie AS ImieLekarza,
        Lekarze.Nazwisko AS NazwiskoLekarza,
        Wizyty.Status
    FROM Wizyty
    LEFT JOIN Lekarze ON Wizyty.LekarzID = Lekarze.ID
    WHERE Wizyty.PacjentID=?
    ORDER BY datetime(Wizyty.Data || ' ' || Wizyty.Godzina) ASC
    """, conn, params=(pacjent_id,))


    if wizyty_df.empty:
        st.info("Brak wizyt dla tego pacjenta.")
    else:
        st.subheader("Wizyty pacjenta:")
        teraz = datetime.now()

        for _, row in wizyty_df.iterrows():
            wizyta_czas = datetime.strptime(f"{row['Data']} {row['Godzina']}", "%Y-%m-%d %H:%M")

            if wizyta_czas < teraz and row['Status'] == 'Wykonana':
                status = "Wykonana"
                kolor = "#5cb85c"
            elif wizyta_czas >= teraz and row['Status'] == 'Zaplanowana':
                status = "Zaplanowana"
                kolor = "#0275d8"
            elif row['Status'] == 'Anulowana':
                status = "Anulowana"
                kolor = "#d9534f"
            else:
                status = "Do weryfikacji"
                kolor = "#f0ad4e"

            st.markdown(f"""
                <div style='padding: 12px; border-radius: 10px; background-color: {kolor}; color: white; margin-bottom: 10px;'>
                    <strong>{row['Data']} o {row['Godzina']}</strong> - dr {row['ImieLekarza']} {row['NazwiskoLekarza']}<br>
                    <em>Status: {status}</em>
                </div>
            """, unsafe_allow_html=True)

    if st.button("🗑️ Usuń pacjenta"):
        c = conn.cursor()
        conn.execute("UPDATE Pacjenci SET Active=0 WHERE ID=?", (pacjent_id,))
        conn.commit()
        st.success("Pacjent oznaczony jako nieaktywny.")

        conn.commit()
        st.success("Pacjent został usunięty.")
        st.query_params(menu="pacjenci")
        st.rerun()


elif menu == "ustawienia":
    st.header("Ustawienia")

    st.checkbox(
        "🔒 Blokuj wysyłkę SMS (tryb testowy)",
        key="blokuj_sms",
        value=False
    )
    # --- Dodaj lekarza ---
    with st.expander("Dodaj lekarza"):
        imie_lekarza = st.text_input("Imię lekarza", key="dodaj_imie")
        nazwisko_lekarza = st.text_input("Nazwisko lekarza", key="dodaj_nazwisko")
        specjalizacja = st.text_input("Specjalizacja", key="dodaj_specjalizacja")
        czas_wizyty = st.number_input("Czas trwania wizyty (minuty)", min_value=5, max_value=180, value=30, key="dodaj_czas")
        kalendarz_id = st.text_input("ID kalendarza Google", key="dodaj_kalendarz")

        if st.button("Dodaj lekarza"):
            if not imie_lekarza or not nazwisko_lekarza or not specjalizacja or not kalendarz_id:
                st.error("Wypełnij wszystkie pola!")
            else:
                c = conn.cursor()
                c.execute("""
                    INSERT INTO Lekarze (Imie, Nazwisko, Specjalizacja, Czas_Wizyty, KalendarzID)
                    VALUES (?, ?, ?, ?, ?)
                """, (imie_lekarza, nazwisko_lekarza, specjalizacja, czas_wizyty, kalendarz_id))
                conn.commit()
                st.success("Dodano lekarza.")


    with st.expander("Godziny pracy lekarzy"):
        # 1️⃣ wybór lekarza
        lekarze_df = pd.read_sql("SELECT ID, Imie, Nazwisko FROM Lekarze", conn)
        if lekarze_df.empty:
            st.info("Brak lekarzy — dodaj lekarza w zakładce Pacjenci.")
            st.stop()
        lekarz_options = (lekarze_df["Imie"] + " " + lekarze_df["Nazwisko"]).tolist()
        lekarz_selected = st.selectbox("Wybierz lekarza", lekarz_options)
        lekarz_id = lekarze_df.loc[
            (lekarze_df["Imie"] + " " + lekarze_df["Nazwisko"]) == lekarz_selected,
            "ID"
        ].values[0]

        # 2️⃣ pobranie istniejących godzin
        godziny_df = pd.read_sql("""
            SELECT ID, DzienTygodnia, GodzinaOd, GodzinaDo
            FROM GodzinyPracyLekarzy
            WHERE LekarzID=?
            ORDER BY CASE
                WHEN DzienTygodnia='Poniedziałek' THEN 1
                WHEN DzienTygodnia='Wtorek' THEN 2
                WHEN DzienTygodnia='Środa' THEN 3
                WHEN DzienTygodnia='Czwartek' THEN 4
                WHEN DzienTygodnia='Piątek' THEN 5
                WHEN DzienTygodnia='Sobota' THEN 6
                WHEN DzienTygodnia='Niedziela' THEN 7
            END
        """, conn, params=(lekarz_id,))

        # 3️⃣ edycja/usuń w jednej pętli
        st.subheader("🕒 Edytuj lub usuń godziny")
        for _, row in godziny_df.iterrows():
            od_dom = datetime.strptime(row["GodzinaOd"], "%H:%M").time()
            do_dom = datetime.strptime(row["GodzinaDo"], "%H:%M").time()

            cols = st.columns([2,2,1,1])
            cols[0].markdown(f"**{row['DzienTygodnia']}**")
            # time_input na aktualnych wartościach
            new_od = cols[1].time_input("", od_dom, key=f"od_{row['ID']}")
            new_do = cols[2].time_input("", do_dom, key=f"do_{row['ID']}")

            # Zapisz zmiany
            if cols[3].button("💾", key=f"save_{row['ID']}"):
                sod = new_od.strftime("%H:%M")
                sdo = new_do.strftime("%H:%M")
                if sod >= sdo:
                    st.error("Godzina końcowa musi być późniejsza.")
                else:
                    conn.execute("""
                        UPDATE GodzinyPracyLekarzy
                        SET GodzinaOd=?, GodzinaDo=?
                        WHERE ID=?
                    """, (sod, sdo, row["ID"]))
                    conn.commit()
                    st.success("Zaktualizowano.")


            # Usuń wpis
            if cols[3].button("🗑️", key=f"del_{row['ID']}"):
                conn.execute("DELETE FROM GodzinyPracyLekarzy WHERE ID=?", (row["ID"],))
                conn.commit()
                st.success("Usunięto.")


        # 4️⃣ formularz dodawania nowych godzin
        st.markdown("---")
        st.subheader("➕ Dodaj nowe godziny")
        with st.form("form_add_hours", clear_on_submit=True):
            dzien = st.selectbox("Dzień tygodnia", [
                "Poniedziałek","Wtorek","Środa","Czwartek",
                "Piątek","Sobota","Niedziela"
            ])
            godz_od = st.time_input("Godzina od")
            godz_do = st.time_input("Godzina do")
            dodaj = st.form_submit_button("Dodaj godzinę")
            if dodaj:
                sod = godz_od.strftime("%H:%M")
                sdo = godz_do.strftime("%H:%M")
                if sod >= sdo:
                    st.error("Godzina końcowa musi być późniejsza niż początkowa.")
                else:
                    conn.execute("""
                        INSERT INTO GodzinyPracyLekarzy
                        (LekarzID, DzienTygodnia, GodzinaOd, GodzinaDo)
                        VALUES (?,?,?,?)
                    """, (lekarz_id, dzien, sod, sdo))
                    conn.commit()
                    st.success(f"Dodano: {dzien} {sod}–{sdo}")



def wyslij_sms(numer, tresc):
    client = Client(Config.TWILIO_SID, Config.TWILIO_TOKEN)
    message = client.messages.create(
        body=tresc,
        from_=Config.TWILIO_NUMBER,
        to=numer
    )
    st.success(f"SMS wysłany do {numer}")

def wyslij_sms_potwierdzenie(pacjent_id, data, godzina, conn):
    pacjent = pd.read_sql(f"SELECT * FROM Pacjenci WHERE ID={pacjent_id}", conn).iloc[0]
    tresc = f"✅ Potwierdzenie rezerwacji: Wizyta u lekarza o {godzina} dnia {data}.\n😊 Do zobaczenia!"
    wyslij_sms(pacjent['Telefon'], tresc)

def wyslij_przypomnienie():
    with sqlite3.connect(Config.DB_FILE) as conn_local:
        c = conn_local.cursor()
        teraz = datetime.now()
        c.execute("SELECT ID, PacjentID, Data, Godzina FROM Wizyty WHERE PrzypomnienieWyslane=0")
        wizyty = c.fetchall()
        for id_wizyty, pacjent_id, data, godzina in wizyty:
            wizyta_czas = datetime.strptime(f"{data} {godzina}", "%Y-%m-%d %H:%M")
            roznica = (wizyta_czas - teraz).total_seconds()
            if 7140 <= roznica <= 7260:
                c.execute("SELECT Telefon FROM Pacjenci WHERE ID=?", (pacjent_id,))
                pacjent = c.fetchone()
                if pacjent:
                    telefon = pacjent[0]
                    tresc = f"⏰ Przypomnienie: Twoja wizyta o {godzina} dnia {data}. Prosimy o punktualność!"
                    wyslij_sms(telefon, tresc)
                    c.execute("UPDATE Wizyty SET PrzypomnienieWyslane=1 WHERE ID=?", (id_wizyty,))
                    conn_local.commit()

def przypomnienia_loop():
    while True:
        wyslij_przypomnienie()
        time.sleep(60)  # sprawdzaj co minutę



def dodaj_testowe_dane():
    conn = sqlite3.connect(Config.DB_FILE)
    c = conn.cursor()

    # Dodaj pacjenta jeśli brak
    c.execute("SELECT COUNT(*) FROM Pacjenci")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO Pacjenci (Imie, Nazwisko, Telefon, PESEL) VALUES (?, ?, ?, ?)",
                  ("Jan", "Kowalski", "500600700", "12345678901"))

    # Dodaj lekarza jeśli brak
    c.execute("SELECT COUNT(*) FROM Lekarze")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO Lekarze (Imie, Nazwisko, Specjalizacja, Czas_Wizyty, KalendarzID) VALUES (?, ?, ?, ?, ?)",
                  ("Anna", "Nowak", "Dermatolog", 30, "twoj_kalendarz_id"))

    conn.commit()

def zarezerwuj_wizyte(pesel, data, lekarz):
    # Tu Twoja logika — np. sprawdzenie dostępności, zapis do pliku
    print(f"🔔 Rezerwuję wizytę: PESEL={pesel}, DATA={data}, LEKARZ={lekarz}")
    # Tymczasowo zakładamy, że każda rezerwacja się udaje
    return True


init_db()
dodaj_testowe_dane()

threading.Thread(target=przypomnienia_loop, daemon=True).start()
