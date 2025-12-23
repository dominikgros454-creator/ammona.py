import serial
import time
import re
from przychodnia_apps import (
    rezerwacja_prosta
)
import sqlite3
import struct
from datetime import datetime, timedelta

DB_PATH = "przychodnia.db"

weekdays={0:'Poniedziałek',1:'Wtorek',2:'Środa',3:'Czwartek',4:'Piątek',5:'Sobota',6:'Niedziela'}

def get_suggestions(doktor, data_str):
    conn = sqlite3.connect(DB_PATH); cur = conn.cursor()
    cur.execute("SELECT id, czas_wizyty FROM Lekarze WHERE lower(imie||' '||nazwisko)=?",(doktor.lower(),))
    row = cur.fetchone()
    if not row: conn.close(); return []
    lekarz_id, duration = row; duration = duration or 30
    blob_id = struct.pack('<Q', lekarz_id)

    date0 = datetime.strptime(data_str, "%Y-%m-%d").date()
    suggestions = []
    for day_delta in range(1, 8):              # przeglądamy kolejne dni
        curr_date = date0 + timedelta(days=day_delta)
        dzien = weekdays[curr_date.weekday()]

        cur.execute(
            "SELECT GodzinaOd, GodzinaDo FROM GodzinyPracyLekarzy "
            "WHERE LekarzID=? AND DzienTygodnia=?",
            (blob_id, dzien)
        )
        periods = cur.fetchall()
        if not periods: continue

        cur.execute(
            "SELECT Godzina FROM Wizyty WHERE LekarzID=? AND Data=? AND Status!='Odwołana'",
            (lekarz_id, curr_date.strftime("%Y-%m-%d"))
        )
        occupied = {r[0] for r in cur.fetchall()}

        for od, do in periods:                  # pierwszy wolny slot w danym dniu
            start = datetime.strptime(od, "%H:%M")
            end   = datetime.strptime(do, "%H:%M")
            curr  = start
            while curr + timedelta(minutes=duration) <= end:
                slot = curr.strftime("%H:%M")
                if slot not in occupied:
                    suggestions.append((curr_date.strftime("%Y-%m-%d"), slot))
                    break
                curr += timedelta(minutes=duration)
            if suggestions and suggestions[-1][0]==curr_date.strftime("%Y-%m-%d"):
                break

        if len(suggestions) >= 3:
            break

    conn.close()
    return suggestions




from przychodnia_apps import init_db

# Konfiguracja portu szeregowego
PORT = 'COM3'
BAUDRATE = 115200

SMS_MEMORY = 'ME'  # ME = pamięć modemu, SM = karta SIM

import re

def parse_reservation_request(text):
    parts = [p.strip() for p in text.split(';')]
    if len(parts) != 5:
        return None
    imie_nazwisko, date_str, time_str, doctor_str, opis = parts
    if len(imie_nazwisko.split())<2 or len(doctor_str.split())<2:
        return None
    return imie_nazwisko, date_str, time_str, doctor_str, opis

def setup_modem(ser):
    # 1) tryb tekstowy
    send_at(ser, 'AT+CMGF=1', 0.3)
    # 2) pamięć ME (modem‐internal), by pewnie SMS-y tam lądowały
    send_at(ser, 'AT+CPMS="ME","ME","ME"', 0.3)
    # 3) URC +CMTI przy nowym SMS
    send_at(ser, 'AT+CNMI=2,1,0,0,0', 0.3)
    # 4) kodowanie
    send_at(ser, 'AT+CSCS="GSM"', 0.3)
    send_at(ser, 'AT+CSMP=17,167,0,0', 0.3)
    # 5) wyczyść wszystkie stare SMSy
    send_at(ser, 'AT+CMGD=1,4', 1.0)

def send_at(ser, command, delay=0.5) -> str:
    """
    Wysyła AT-komendę do modemu, czeka `delay` sekundy
    i zwraca pełną odpowiedź jako string.
    """
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    ser.write((command + '\r').encode())
    time.sleep(delay)
    return ser.read_all().decode(errors='ignore')

def poll_new_sms(ser, wait_seconds=5):
    """
    Pobiera wszystkie SMS-y (AT+CMGL="ALL") i zwraca listę krotek:
    (idx:int, status:str, sender:str, text:str).
    Czeka aż w buforze pojawi się 'OK' lub 'ERROR' lub upłynie timeout.
    """
    # ustaw pamięć na SIM (już masz to w init, ale na pewno)
    # w setup_modem i w poll_new_sms
    send_at(ser, 'AT+CPMS="ME","ME","ME"', 0.3)


    # wyczyść buffery i wyślij zapytanie
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    ser.write(b'AT+CMGL="ALL"\r')

    # zbieramy odpowiedź aż do OK/ERROR lub timeout
    buf = b""
    t0 = time.time()
    while time.time() - t0 < wait_seconds:
        time.sleep(0.1)
        n = ser.in_waiting or 0
        if n:
            buf += ser.read(n)
        # zakończ gdy mamy końcowe statusy
        if b"\r\nOK\r\n" in buf or b"\r\nERROR\r\n" in buf:
            break

    raw = buf.decode(errors="ignore")
    print("→ surowe CMGL:", repr(raw))

    # Parsowanie bloków +CMGL
    msgs = []
    # Split po sekwencji "+CMGL:" - ułatwia wyodrębnienie kolejnych wiadomości
    parts = raw.split("+CMGL:")
    for p in parts[1:]:  # pierwszy element to wszystko przed pierwszym +CMGL
        try:
            # nagłówek jest pierwszą linią bloku
            header, rest = p.split("\r\n", 1)
            # header przykładowo: ' 0,"REC UNREAD","+48572687408",,"25/08/30,15:46:28+08"'
            hparts = [h.strip() for h in header.split(",")]
            idx = int(hparts[0])
            status = hparts[1].strip('"')
            sender = hparts[2].strip('"')

            # ciało wiadomości to pierwsza linia rest (ale czasem są dodatkowe CRLF)
            # bierzemy do najbliższego CRLF przed końcem bloku (OK/ERROR)
            body = rest.split("\r\n")[0].strip()

            # jeśli wygląda jak HEX UCS2 — spróbuj zdekodować
            body_clean = body.replace(" ", "").strip()
            if len(body_clean) >= 4 and all(c in "0123456789ABCDEFabcdef" for c in body_clean):
                try:
                    decoded = bytes.fromhex(body_clean).decode("utf-16-be")
                    body = decoded
                except Exception:
                    # nie udało się dekodować — zostaw surowy
                    pass

            msgs.append((idx, status, sender, body))
        except Exception as e:
            # ignoruj parsowanie niepowodzeń dla danego bloku, ale debuguj
            print("⚠️ Błąd parsowania bloku CMGL:", e)
            continue

    return msgs




def send_sms(ser, number, message, wait_prompt=7, wait_send=12):
    """
    Wysyła SMS w trybie tekstowym, czekając na prompt '>' i
    na potwierdzenie wysyłki '+CMGS:'.
    Zwraca True jeśli wysłano (znaleziono +CMGS), False w przeciwnym wypadku.
    """
    # upewnij się, że tryb tekstowy
    send_at(ser, 'AT+CMGF=1', 0.3)

    # wyczyść buffory
    ser.reset_input_buffer()
    ser.reset_output_buffer()

    # uruchom komendę wysyłki
    ser.write(f'AT+CMGS="{number}"\r'.encode())

    # czekamy na prompt '>' (lub ewentualny błąd)
    prompt_buf = b""
    t0 = time.time()
    while time.time() - t0 < wait_prompt:
        time.sleep(0.1)
        n = ser.in_waiting or 0
        if n:
            prompt_buf += ser.read(n)
            if b'>' in prompt_buf:
                break
            if b'ERROR' in prompt_buf:
                print("❌ Błąd podczas oczekiwania na prompt:", prompt_buf.decode(errors='ignore'))
                return False

    if b'>' not in prompt_buf:
        print("❌ Brak promptu '>' od modemu. Buf:", repr(prompt_buf.decode(errors='ignore')))
        return False

    # Wyślij treść i Ctrl+Z
    # Używamy latin-1 dla polskich znaków albo utf-8 jeśli modem akceptuje;
    # dla krótkich "tak"/"dobrze" nie ma problemu.
    ser.write(message.encode('latin-1', errors='ignore'))
    ser.write(b'\x1A')  # Ctrl+Z

    # czekamy na potwierdzenie +CMGS lub ERROR
    resp_buf = b""
    t1 = time.time()
    while time.time() - t1 < wait_send:
        time.sleep(0.2)
        n = ser.in_waiting or 0
        if n:
            resp_buf += ser.read(n)
            if b'+CMGS:' in resp_buf:
                print("→ send_sms resp:", repr(resp_buf.decode(errors='ignore')))
                return True
            if b'ERROR' in resp_buf:
                print("→ send_sms ERROR resp:", repr(resp_buf.decode(errors='ignore')))
                return False

    print("⚠️ Timeout oczekiwania na potwierdzenie wysyłki. Buf:", repr(resp_buf.decode(errors='ignore')))
    return False



def main():
   # 0) Inicjalizacja bazy
   init_db()

    # 1) Otwórz port
   ser = serial.Serial(PORT, BAUDRATE, timeout=0.5)
   time.sleep(1)
   setup_modem(ser)
   print("✅ Modem gotowy — czekam na +CMTI…")

   try:
       while True:
           raw = ser.readline().decode(errors='ignore').strip()
           if not raw:
                continue

            # 2) nowy SMS → +CMTI: "ME",<idx>
           if raw.startswith('+CMTI:'):
               idx = raw.split(',')[1]
               print(f">>> +CMTI slot {idx}")

                # 3) Odczytaj SMS
               resp = send_at(ser, f'AT+CMGR={idx}', 1.0)
               lines = [l for l in resp.splitlines() if l and not l.startswith('OK')]
               header, body = lines[0], (lines[1] if len(lines) > 1 else "")
               sender = header.split(',')[1].strip('"')
               # … w pętli po wykryciu +CMTI i odczytaniu body …
               print("SMS od", sender, ":", repr(body))

# 1) parsujemy body
               # --- ZAMIENIĆ TEN FRAGMENT: parsowanie i natychmiastowa obsługa błędnego formatu ---
               parts = body.strip().split(';')
               if len(parts) < 4:
                   reply = ("❌ Niepoprawny format. Uzyj:\n"
                            "Imie Nazwisko;YYYY-MM-DD;HH:MM;Imie NazwiskoLekarza;opis (opcjonalnie)")
                   print("❌ Niepoprawny format SMS:", body)
                   send_sms(ser, sender, reply)
                   send_at(ser, f'AT+CMGD={idx}', 0.5)
                   continue  # <- WAŻNE: przerwij obsługę tej wiadomości, nie idziemy dalej

               # poprawnie sparsowany
               imie_nazwisko, data_str, time_str, doktor = parts[:4]
               opis = parts[4] if len(parts) > 4 else ""

               # próbujemy zarezerwować — tylko jeśli doszliśmy tutaj (format OK)
               try:
                   ok = rezerwacja_prosta(
                       imie_nazwisko=imie_nazwisko,
                       doktor=doktor,
                       data=data_str,
                       godzina=time_str,
                       opis=opis
                   )
                   if ok:
                       reply = "✅ Twoja wizyta została zarezerwowana."
                   else:
                       # rezerwacja zwróciła False — proponujemy alternatywy
                       sugestie = get_suggestions(doktor, data_str)
                       if sugestie:
                           propozycje = ", ".join(f"{d} {g}" for d, g in sugestie)
                           reply = f"❌ Termin niedostępny. Proponuję: {propozycje}"
                       else:
                           reply = "❌ Termin niedostępny — brak dostępnych terminów."
               except Exception as e:
                   # jeśli rezerwacja rzuci wyjątek — sformatuj sensowny komunikat
                   reply = f"❌ Błąd podczas rezerwacji: {e}"

 
               print("Rezerwacja:", reply)


               # 6) Wyślij odpowiedź SMS i usuń wiadomość
               send_sms(ser, sender, reply)
               send_at(ser, f'AT+CMGD={idx}', 0.5)
               print(f">>> Odpowiedź: {reply}")

           time.sleep(0.1)

   except KeyboardInterrupt:
       pass
   finally:
       ser.close()
       print("Port zamknięty.")

if __name__ == "__main__":
    main()

for _, row in wizyty_df.iterrows():
            cols = st.columns([3,3,2,2,1])
            badge = (
                "<span style='display:inline-block;width:40px;margin-right:8px;margin-left:-50px;"
                "background:orange;color:white;padding:2px 4px;border-radius:6px;font-size:1.0em;'>BOT</span> "
                if row["Zrodlo"]=="Bot_SMS" else ""
            )
            cols[0].markdown(f"{badge}{row['Lekarz']}", unsafe_allow_html=True)

            cols[1].write(row["Pacjent"])
            cols[2].write(row["Data"])
            cols[3].write(row["Godzina"])
            # nic nie przerywamy, tylko ustawiamy stan
            if cols[4].button("Szczegóły", key=f"szcz_{row['ID']}"):
                st.session_state.selected_wizyta = row["ID"]
