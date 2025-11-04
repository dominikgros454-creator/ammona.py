# app.py
import streamlit as st
from pathlib import Path
from datetime import date, datetime, timedelta
import sqlite3

st.set_page_config(page_title="Dyżury domowe", layout="centered")

# safe rerun helper
def safe_rerun():
    try:
        if hasattr(st, "rerun"):
            st.rerun()
            return
    except Exception:
        pass
    try:
        if hasattr(st, "experimental_rerun"):
            st.experimental_rerun()
            return
    except Exception:
        pass
    st.session_state["_force_refresh"] = not st.session_state.get("_force_refresh", False)

# minimal CSS
st.markdown(
    """
    <style>
      #MainMenu { visibility: hidden !important; }
      header { visibility: hidden !important; }
      footer { visibility: hidden !important; }
      section.main > div.block-container { padding-top: 0rem !important; margin-top: 0rem !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

DB_FILENAME = "dyzury_local.db"
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

# save uploaded file helper
def save_uploaded_image_simple(uploaded_file, prefix: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    orig_name = Path(uploaded_file.name).stem
    ext = Path(uploaded_file.name).suffix.lstrip(".").lower() or "jpg"
    filename = f"{prefix}_{orig_name}_{ts}.{ext}"
    out_path = UPLOAD_DIR / filename
    with out_path.open("wb") as f:
        f.write(uploaded_file.getvalue())
    return str(out_path)

# render expander content (uploader + buttons)
def render_expander_uploader(rid: int, cur, conn):
    file_key = f"exp_file_{rid}"
    comm_key = f"exp_comm_{rid}"
    done_key = f"exp_done_{rid}"
    cancel_key = f"exp_cancel_{rid}"

    uploaded = st.file_uploader("Wybierz zdjęcie z telefonu (opcjonalne)", type=["png","jpg","jpeg","webp"], key=file_key)
    if uploaded is not None:
        try:
            st.image(uploaded, use_container_width=True)
        except Exception:
            st.write(f"Wybrano plik: {uploaded.name}")

    comment = st.text_input("Komentarz (opcjonalnie)", key=comm_key)
    c1, c2 = st.columns([1,1])
    saved = False
    saved_path = None

    if c1.button("Wykonane", key=done_key):
        if uploaded is not None:
            saved_path = save_uploaded_image_simple(uploaded, f"rid{rid}")
            cur.execute("UPDATE DyzuryDomowe SET photo=?, done=1 WHERE id=?", (saved_path, rid))
            conn.commit()
            st.success("Zapisano zdjęcie i oznaczono dyżur jako wykonany.")
        else:
            cur.execute("UPDATE DyzuryDomowe SET done=1 WHERE id=?", (rid,))
            conn.commit()
            st.success("Dyżur oznaczony jako wykonany (bez zdjęcia).")
        saved = True

    if c2.button("Anuluj", key=cancel_key):
        st.info("Anulowano weryfikację.")

    return saved, saved_path

# create DB and sample data
def create_db_and_samples(path: Path, weeks_ahead: int = 4):
    conn = sqlite3.connect(str(path))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS DyzuryDomowe (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dziecko TEXT NOT NULL,
            data TEXT NOT NULL,
            dyzor TEXT NOT NULL,
            done INTEGER DEFAULT 0,
            photo TEXT DEFAULT NULL
        )
    """)
    conn.commit()

    cur.execute("SELECT COUNT(1) FROM DyzuryDomowe")
    if cur.fetchone()[0] > 0:
        conn.close()
        return

        # --- nowe: deterministyczna rotacja tygodniowa (1 zadanie na dziecko na cały tydzień) ---
    children = ["Kamil", "Ania", "Dominik", "Mateusz"]
    task_cycle = ["Kuchnia", "Podlogi", "Pranie", "Lazienki"]

    # oblicz poniedziałek bieżącego tygodnia
    today = date.today()
    monday_this_week = today - timedelta(days=today.weekday())

    # funkcja zwracająca numer tygodnia względem stałej epoki (poniedziałek)
    def week_index_for_date(d: date, epoch: date = date(2020, 1, 6)):
        return (d - epoch).days // 7

    start_week_idx = week_index_for_date(monday_this_week)

    inserts = []
    for w in range(weeks_ahead):
        this_week_idx = start_week_idx + w
        week_monday = monday_this_week + timedelta(weeks=w)
        # dla każdego dziecka obliczamy jedno zadanie na cały tydzień
        for base_idx, child in enumerate(children):
            task_index = (base_idx + this_week_idx) % len(task_cycle)
            task = task_cycle[task_index]
            # wstaw wpisy na każdy dzień tygodnia z tym samym zadaniem
            for weekday in range(7):
                this_date = week_monday + timedelta(days=weekday)
                inserts.append((child, this_date.isoformat(), task))


    cur.executemany("INSERT INTO DyzuryDomowe (dziecko, data, dyzor) VALUES (?, ?, ?)", inserts)
    conn.commit()
    conn.close()

# reseed control (set False after first run)
# reseed control (set False after first run)
FORCE_RESEED = False
db_path = Path(DB_FILENAME)
if FORCE_RESEED:
    conn_tmp = sqlite3.connect(str(db_path))
    cur_tmp = conn_tmp.cursor()
    cur_tmp.execute("""
        CREATE TABLE IF NOT EXISTS DyzuryDomowe (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dziecko TEXT NOT NULL,
            data TEXT NOT NULL,
            dyzor TEXT NOT NULL,
            done INTEGER DEFAULT 0,
            photo TEXT DEFAULT NULL
        )
    """)
    conn_tmp.commit()
    cur_tmp.execute("DELETE FROM DyzuryDomowe")
    conn_tmp.commit()
    conn_tmp.close()
    create_db_and_samples(db_path, weeks_ahead=8)
else:
    if not db_path.exists():
        create_db_and_samples(db_path, weeks_ahead=8)

# connect DB
conn = sqlite3.connect(str(db_path), check_same_thread=False)
cur = conn.cursor()

# ensure columns
cur.execute("PRAGMA table_info(DyzuryDomowe)")
cols = [r[1] for r in cur.fetchall()]
if "done" not in cols:
    cur.execute("ALTER TABLE DyzuryDomowe ADD COLUMN done INTEGER DEFAULT 0")
    conn.commit()
if "photo" not in cols:
    cur.execute("ALTER TABLE DyzuryDomowe ADD COLUMN photo TEXT DEFAULT NULL")
    conn.commit()


# prepare tabs
cur.execute("SELECT DISTINCT TRIM(dziecko) FROM DyzuryDomowe WHERE TRIM(dziecko)<>'' ORDER BY LOWER(TRIM(dziecko))")
rows = cur.fetchall()
display_names = [r[0].capitalize() for r in rows]
name_map = {r[0].capitalize(): r[0] for r in rows}

display_names.append("Panel rodzica")

today_str = date.today().isoformat()

# Render UI: use expander per not-done entry (no separate button)
if display_names:
    tabs = st.tabs(display_names)
    for i, disp_name in enumerate(display_names):
        with tabs[i]:
            import streamlit.components.v1 as components

            if disp_name == "Panel rodzica":
                   # --- INICJALIZACJA ---
                PARENT_PIN = "1234"  # <- ustaw swój PIN tutaj
                pin_state_key = "parent_pin_unlocked"
                input_key = "parent_pin_input"

                if input_key not in st.session_state:
                    st.session_state[input_key] = ""
                if pin_state_key not in st.session_state:
                    st.session_state[pin_state_key] = False  # <-- DODANE
  
                st.markdown("### Panel rodzica")
                
              
                if st.session_state[pin_state_key]:
                    st.markdown("### ")
                    st.success("Panel rodzica odblokowany")
                    st.info("Tu w przyszłości dodasz ustawienia i statystyki.")
                    # <-- wklej poniżej tej linii
                    # Panel rodzica — lista dyżurów na dziś (statusy dzieci)
                    from datetime import date
                    today = date.today().isoformat()
                    st.markdown(f"**Dzisiaj:** {today}")

                    with st.spinner("Ładuję listę dyżurów na dziś…"):
                        cur.execute(
                            "SELECT id, dziecko, dyzor, COALESCE(done,0), COALESCE(photo,'') "
                            "FROM DyzuryDomowe WHERE data=? ORDER BY LOWER(TRIM(dziecko)), id",
                            (today,)
                        )
                        rows_today = cur.fetchall()


                    # tymczasowy debug (usuń gdy działa)
                    # st.write("DEBUG rows_today:", rows_today)

                    if not rows_today:
                        st.info("Na dziś brak dyżurów w bazie.")
                    else:
                        by_child = {}
                        for rid, child, task, done, photo in rows_today:
                            key = (child or "Nieznany").strip()
                            by_child.setdefault(key, []).append({"id": rid, "task": task, "done": bool(done), "photo": photo})

                        for child, tasks in by_child.items():
                            st.markdown(f"#### {child}")
                            left, right = st.columns([4,1])
                            with left:
                                for t in tasks:
                                    status = "✅" if t["done"] else "❌"
                                    st.write(f"{status} **{t['task']}**  —  id:{t['id']}")
                                    if t["photo"]:
                                        p = Path(t["photo"])
                                        if p.exists():
                                            st.image(str(p), width=160)
                            with right:
                                done_count = sum(1 for t in tasks if t["done"])
                                st.markdown(f"**Wykonane**\n{done_count}/{len(tasks)}")
                            st.markdown("---")


     # nieodblokowany — proste pole i przycisk
                st.write("Wprowadź 4-cyfrowy PIN i kliknij 'Otwórz panel'.")
     
                col1, col2 = st.columns([2, 1])
                with col1:
                    pin_val = st.text_input(
                        "",
                        value=st.session_state.get(input_key, ""),
                        max_chars=4,
                        key=input_key,
                        placeholder="••••",
                        type="password",
                    )
                def try_unlock():
                    pin_s = "".join(ch for ch in (st.session_state.get(input_key,"") or "") if ch.isdigit())
                    if len(pin_s) != 4:
                        st.error("PIN musi zawierać 4 cyfry.")
                        return
                    if pin_s == PARENT_PIN:
                        st.session_state[pin_state_key] = True
                        st.session_state[input_key] = ""   # bezpieczne — wykonywane w callbackie
                    else:
                        st.error("Nieprawidłowy PIN")

                with col2:
                    st.button("Otwórz panel rodzica", on_click=try_unlock)

 
                continue


                # nieodblokowany — pokaż opis i widget z automatycznym wpisywaniem PINu
                st.markdown("### Panel rodzica")
                st.write("Wprowadź 4-cyfrowy PIN. Zacznij pisać — fokus będzie się przesuwać automatycznie.")

                html_code = """
                <div style="display:flex;justify-content:center;margin-top:8px;">
                  <style>
                    .pin-input{width:48px;height:48px;font-size:24px;text-align:center;border-radius:6px;border:1px solid #ccc;margin:0 6px;}
                    .pin-input:focus{outline:2px solid #6ea0ff;}
                  </style>
                  <input id="p1" class="pin-input" type="tel" maxlength="1" inputmode="numeric" pattern="[0-9]*" autofocus />
                  <input id="p2" class="pin-input" type="tel" maxlength="1" inputmode="numeric" pattern="[0-9]*" />
                  <input id="p3" class="pin-input" type="tel" maxlength="1" inputmode="numeric" pattern="[0-9]*" />
                  <input id="p4" class="pin-input" type="tel" maxlength="1" inputmode="numeric" pattern="[0-9]*" />
                </div>
                <script>
                  const inputs = [p1,p2,p3,p4];
                  inputs.forEach((input, idx) => {
                    input.addEventListener('input', (e) => {
                      e.target.value = e.target.value.replace(/\\D/g,'');
                      if (e.target.value && idx < inputs.length-1) inputs[idx+1].focus();
                      const pin = inputs.map(i=>i.value).join('');
                      if (pin.length===4){
                        const url = new URL(window.location.href);
                        url.searchParams.set('pin', pin);
                        window.parent.postMessage({type: 'setPin', value: pin}, '*');
                      }
                    });
                    input.addEventListener('keydown', (e) => {
                      if (e.key === 'Backspace' && !e.target.value && idx>0) inputs[idx-1].focus();
                    });
                  });
                </script>
                """
                components.html(html_code, height=140)
                continue

            orig = name_map[disp_name] 
            st.markdown(f"### Dyżury dla {disp_name} — {today_str}")

            with st.spinner(f"Ładuję dyżury dla {disp_name}…"):
                cur.execute(
                    "SELECT id, data, dyzor, COALESCE(done,0), COALESCE(photo,'') "
                    "FROM DyzuryDomowe WHERE TRIM(dziecko)=? AND data=? ORDER BY id",
                    (orig, today_str)
                )
                wpisy = cur.fetchall()

            if not wpisy:
                st.info("Dziś brak dyżurów dla tego dziecka.")
            else:
                for rid, d_str, dyzor, done, photo in wpisy:
                    col_left, col_right = st.columns([5,1])

                    # tytuł wpisu — przekreślony gdy wykonane
                    if done:
                        col_left.markdown(
                            f"📅 <span style='text-decoration:line-through; color:gray;'>**{d_str} — {dyzor}**</span>",
                            unsafe_allow_html=True,
                        )
                    else:
                        col_left.markdown(f"📅 **{d_str}** — {dyzor}")

                    # zdjęcie (jeśli istnieje)
                    if photo:
                        p = Path(photo)
                        if p.exists():
                            col_left.image(str(p), use_container_width=True)

                    # --- opis dyżuru: zawsze pod zdjęciem (edytowalny szablon) ---
                    task = (dyzor or "").strip().lower()
                    task_sentences = {
                        "kuchnia": "Sprzątanie ze stołu, blaty i zmywarka. Dodaj zdjęcia wykonengo stołu blatów i pustego kosza na naczynia.",
                        "podłogi": "Odkurzanie schodów i podłogi na parterze oraz mycie podłogi na parterze. Dodaj zdjęcia rezultatów (czystej podłogi).",
                        "pranie": "zrobienie 2x prania dowolnego koloru i wysuszenie go w suszarce lub na wieszakach. Następnie poskładanie i rozdzielenie do pokojów. Dodaj zdjecia poskładanego prania.",
                        "lazienki": "Mycie kafli, umywalki, kibla i prysznica szmatką lub gąbką oraz mycie mopem podłogi łazienkowej. Dodaj zdjęcie łązienki z podłogą.",
                        "sprzątanie": "",
                    }

                    if task in task_sentences:
                        text = task_sentences[task]
                    else:
                        text = f"Zadanie: {dyzor}. Wykonaj czynności związane z tym dyżurem." if task else "Brak szczegółowego opisu dyżuru."

                    status = "Wykonane" if done else "Do zrobienia"
                    caption_text = f"{status} — {d_str}\n{text}"
                    col_left.caption(caption_text)
                    # --- koniec opisu ---

                    # prawa kolumna: podsumowanie / licznik
                    done_text = "✅ Wykonane" if done else "❌ Do zrobienia"
                    col_right.markdown(done_text)
                    
                    if not done:
                        with st.expander("📸 Zweryfikuj / dodaj zdjęcie", expanded=False):
                            # pokaż istniejące zdjęcie w expanderze (jeśli jest)
                            if photo:
                                p = Path(photo)
                                if p.exists():
                                    st.image(str(p), use_container_width=True)

                            # render uploader i przyciski zawsze
                            saved, saved_path = render_expander_uploader(rid, cur, conn)
                            if saved:
                                # po zapisie wymuszamy odświeżenie, żeby zobaczyć nowe zdjęcie w UI
                                safe_rerun()

                    st.markdown("---")




