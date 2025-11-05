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

# --- helpery do normalizacji nazw zadań ---
TASK_ALIASES = {
    "lazienki": "Łazienki",
    "lazienka": "Łazienki",
    "łazienki": "Łazienki",
    "kuchnia": "Kuchnia",
    "pranie": "Pranie",
    "podlogi": "Podłogi",
    "podłogi": "Podłogi",
    "podloga": "Podłogi",
    "sprzatanie": "Sprzątanie",
}

def canonical_task_name(raw: str) -> str:
    """Zamienia różne warianty na ujednoliconą formę (z wielką literą i polskimi znakami)."""
    if not raw:
        return raw
    s = str(raw).strip().lower()
    # usuń spacje, kropki itp. -> prosty alias
    s = s.replace(" ", "").replace(".", "").replace("-", "")
    # spróbuj znaleźć alias bez polskich znaków też
    # zamień ł -> l aby dopasować różne wpisy
    s_no_l = s.replace("ł", "l")
    for key in list(TASK_ALIASES.keys()):
        key_no_l = key.replace("ł", "l")
        if s == key or s == key_no_l or s_no_l == key_no_l:
            return TASK_ALIASES[key]
    # fallback: capitalize first letter
    return raw.strip().capitalize()

# --- nowa create_db_and_samples (generuje rotację i normalizuje nazwy) ---
def create_db_and_samples(path: Path, weeks_ahead: int = 8):
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

    # --- dane bazowe (kolejność dzieci i zadań startowego tygodnia) ---
    children = ["Kamil", "Ania", "Dominik", "Mateusz"]
    # lista bazowa (odpowiada tygodniowi startowemu 03.11.2025)
    base_tasks_display = ["Łazienki", "Kuchnia", "Pranie", "Podłogi"]

    # startowy poniedziałek (3 listopada 2025)
    start_date = date(2025, 11, 3)

    def week_index_for_date(d: date):
        return (d - start_date).days // 7

    today = date.today()
    monday_this_week = today - timedelta(days=today.weekday())
    current_week_idx = week_index_for_date(monday_this_week)

    inserts = []
    for w in range(weeks_ahead):
        week_monday = monday_this_week + timedelta(weeks=w)
        week_idx = current_week_idx + w
        for i, child in enumerate(children):
            # rotacja: przesuwamy zadania o week_idx
            task_display = base_tasks_display[(i + week_idx) % len(base_tasks_display)]
            for day in range(7):
                d = (week_monday + timedelta(days=day)).isoformat()
                inserts.append((child, d, task_display))

    # filtrowanie istniejących rekordów (nie nadpisujemy done/photo)
    if not inserts:
        conn.close()
        return

    min_date = min(i[1] for i in inserts)
    max_date = max(i[1] for i in inserts)
    cur.execute(
        "SELECT data, dziecko, dyzor FROM DyzuryDomowe WHERE data BETWEEN ? AND ?",
        (min_date, max_date),
    )
    existing = set(cur.fetchall())

    filtered_inserts = []
    for child, d, task in inserts:
        key = (d, child, task)
        if key not in existing:
            filtered_inserts.append((child, d, task))

    if filtered_inserts:
        cur.executemany(
            "INSERT INTO DyzuryDomowe (dziecko, data, dyzor) VALUES (?, ?, ?)",
            filtered_inserts,
        )
        conn.commit()

    conn.close()

# --- funkcja która wymusza przypisanie zadań dla konkretnego tygodnia ---
def assign_week_tasks(week_monday: date, mapping: dict, conn=None):
    """
    mapping: dict child -> task_display (np. {"Kamil":"Łazienki", ...})
    Ustawia/aktualizuje wpisy w bazie dla dni od week_monday do week_monday+6.
    """
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(str(db_path))
        close_conn = True
    cur = conn.cursor()
    dates = [(week_monday + timedelta(days=d)).isoformat() for d in range(7)]
    for child, task in mapping.items():
        task_can = canonical_task_name(task)
        for d in dates:
            # jeśli istnieje wpis dla child+date -> update dyzor
            cur.execute("SELECT id FROM DyzuryDomowe WHERE TRIM(dziecko)=? AND data=?", (child, d))
            r = cur.fetchone()
            if r:
                cur.execute("UPDATE DyzuryDomowe SET dyzor=? WHERE id=?", (task_can, r[0]))
            else:
                cur.execute("INSERT INTO DyzuryDomowe (dziecko, data, dyzor) VALUES (?, ?, ?)", (child, d, task_can))
    conn.commit()
    if close_conn:
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
                    # --- Panel rodzica: widok tygodniowy + regeneracja ---
                    # --- Panel rodzica: widok tygodniowy + regeneracja ---
                    from datetime import date, timedelta

                    # helper: zwraca poniedziałek dla daty
                    def week_monday(d: date) -> date:
                        return d - timedelta(days=d.weekday())

                    today_date = date.today()
                    monday = week_monday(today_date)
                    week_dates = [(monday + timedelta(days=i)) for i in range(7)]
                    week_strs = [d.isoformat() for d in week_dates]

                    st.markdown(f"**Tydzień:** {monday.isoformat()} — {(monday + timedelta(days=6)).isoformat()}")

                    # pobierz wszystkie dyżury dla tego tygodnia i posortuj po dziecku i dacie
                    cur.execute(
                        "SELECT id, dziecko, data, dyzor, COALESCE(done,0), COALESCE(photo,'') "
                        "FROM DyzuryDomowe WHERE data BETWEEN ? AND ? ORDER BY LOWER(TRIM(dziecko)), data, id",
                        (week_strs[0], week_strs[-1])
                    )
                    week_rows = cur.fetchall()

                    if not week_rows:
                        st.info("Brak wpisów dla tego tygodnia w bazie.")
                    else:
                        # grupuj po dziecku w kolejności zadeklarowanej (Kamil, Ania, Dominik, Mateusz)
                        order = ["Kamil", "Ania", "Dominik", "Mateusz"]
                        by_child = {name: [] for name in order}
                        other = {}
                        for rid, child, d_str, task, done, photo in week_rows:
                            key = (child or "").strip()
                            if key in by_child:
                                by_child[key].append({"id": rid, "date": d_str, "task": task, "done": bool(done), "photo": photo})
                            else:
                                other.setdefault(key or "Inni", []).append({"id": rid, "date": d_str, "task": task, "done": bool(done), "photo": photo})

                        # wyświetl w ustalonej kolejności
                        for child, tasks in by_child.items():
                            st.markdown(f"#### {child}")
                            left, right = st.columns([4,1])

                            # Konsolidacja: pokaż 1 dyżur tygodniowy na dziecko
                            if not tasks:
                                with left:
                                    st.info("Brak dyżurów dla tego dziecka w tym tygodniu.")
                                with right:
                                    st.markdown("**Wykonane**\n0/0")
                            else:
                                from collections import Counter
                                # zbierz nazwy zadań i daty dla danego dziecka (z rows_today)
                                task_names = [t["task"] for t in tasks if t.get("task")]
                                cnt = Counter(task_names)
                                most_common = sorted(cnt.items(), key=lambda x: (-x[1], x[0]))[0][0] if cnt else ""
                                # oblicz status wykonania (ile dni oznaczono jako done)
                                done_count = sum(1 for t in tasks if t.get("done"))
                                total_days = len(tasks)
                                # zakres dat (pokazujemy poniedziałek-niedziela dla wpisów)
                                dates = sorted({t["date"] for t in tasks})
                                week_from = dates[0] if dates else ""
                                week_to = dates[-1] if dates else ""
                                # wyświetlanie
                                with left:
                                    if len(cnt) > 1:
                                        st.warning(f"Uwaga: wykryto różne zadania dla {child} w tym tygodniu. Pokazano najczęstsze: **{most_common}**")
                                    status = "✅" if done_count == total_days and total_days > 0 else "❌"
                                    st.write(f"{status} **{week_from} — {week_to}** — **{most_common}**  (dni zapisane: {total_days})")
                                    # pokaż ewentualne pierwsze zdjęcie
                                    first_photo = next((t.get("photo") for t in tasks if t.get("photo")), None)
                                    if first_photo:
                                        p = Path(first_photo)
                                        if p.exists():
                                            st.image(str(p), width=160)
                                with right:
                                    st.markdown(f"**Wykonane**\n{done_count}/{total_days}")

                            st.markdown("---")

                        # pokaż też dzieci nietypowe
                        for other_name, tasks in other.items():
                            st.markdown(f"#### {other_name}")
                            for t in tasks:
                                          # Konsoliduj zadania: chcemy 1 dyżur (7 dni) na dziecko
                                          if not tasks:
                                              st.info("Brak dyżurów dla tego dziecka w tym tygodniu.")
                                          else:
                                              # zbierz liczniki zadań
                                              from collections import Counter
                                              task_names = [t["task"] for t in tasks]
                                              cnt = Counter(task_names)
                                              # wybierz najczęściej występujące zadanie (deterministycznie: tie -> alfabetycznie)
                                              most_common = sorted(cnt.items(), key=lambda x: (-x[1], x[0]))[0][0]
                                              # zakres daty tygodnia (pokazujemy tydzień jako poniedziałek-niedziela)
                                              dates = sorted({t["date"] for t in tasks})
                                              week_from = dates[0] if dates else ""
                                              week_to = dates[-1] if dates else ""
                                              # jeśli są sprzeczne wpisy (różne zadania) pokaż ostrzeżenie
                                              if len(cnt) > 1:
                                                  st.warning(f"Uwaga: wykryto różne zadania dla {child} w tym tygodniu. Pokazano zadanie najczęstsze: **{most_common}**")
                                              status = "✅" if any(t["done"] for t in tasks) else "❌"
                                              st.write(f"{status} **{week_from} — {week_to}** — **{most_common}**  (liczba wpisów: {len(tasks)})")
                                              # pokaż ewentualne zdjęcie pierwsze dostępne
                                              first_photo = next((t["photo"] for t in tasks if t.get("photo")), None)
                                              if first_photo:
                                                  p = Path(first_photo)
                                                  if p.exists():
                                                      st.image(str(p), width=160)


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




