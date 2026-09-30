"""
MediCampus - Asistente de medicamentos multi-paciente
======================================================

Versión 2.5 - Pacientes fijos de demostración

Cambios frente a la v2.4:
  - Se agregan 32 cuentas de demostración (las de la encuesta) que se crean
    solas al arrancar la app si todavía no existen: cargar_pacientes_demo().
    Si Streamlit Cloud reinicia y borra medicampus.db, se vuelven a crear.
    Correos tipo nombre.apellido@example.com, contraseña: demo1234.
    Para desactivar la carga, define CARGAR_DEMO = "0" en Secrets.
  - El scheduler de recordatorios ignora las cuentas @example.com para no
    intentar enviar correos a direcciones de demostración.

Versión 2.4 - Gestión de pacientes desde la tabla del panel de admin

Cambios frente a la v2.3:
  - En el panel de administrador, la tabla "Pacientes registrados" ahora
    tiene botones de acción (🔑 restablecer contraseña, 🗑️ eliminar cuenta)
    directamente en cada fila, en vez de tener que usar un selector aparte.
    Como st.dataframe no soporta botones dentro de las celdas, esa tabla
    se reemplazó por una tabla "manual" construida con columnas.

Cambios frente a la v2.2 (ya incluidos):
  - Nueva pestaña "¿Olvidaste tu contraseña?" en el login: permite a
    cualquier paciente (incluido el administrador) restablecer su propia
    contraseña usando un código de recuperación (secreto RECOVERY_CODE),
    sin necesitar iniciar sesión primero. Esto evita el candado circular
    de perder la contraseña de admin y no poder entrar al panel de admin
    para resetearla.
  - IMPORTANTE: agrega RECOVERY_CODE en Secrets (una cadena que solo tú
    conozcas) para que esta pestaña funcione. Sin ese secreto configurado,
    la pestaña muestra una advertencia y no permite restablecer nada.
  - El panel de administrador permite ACTUAR, no solo ver:
      * Eliminar medicamentos de cualquier paciente.
      * Eliminar cuentas de pacientes (con confirmación explícita).
      * Restablecer la contraseña de un paciente (útil si te escribe porque
        no puede entrar) — puedes escribir una nueva o dejar el campo vacío
        para que se genere una aleatoria segura, mostrada una sola vez.
  - Se agregó la función resetear_password_admin() en la capa de BD.

NOTA DE SEGURIDAD: el panel de admin se activa comparando el correo de
inicio de sesión con el secreto ADMIN_EMAIL. Como el correo es único por
cuenta, si otra persona conoce esa dirección y se registra con ella ANTES
que tú, esa cuenta quedará con el panel de admin. Regístrate tú mismo con
ese correo apenas despliegues la app para "reclamarlo".

IMPORTANTE: Este es un prototipo académico. La base de interacciones es
de demostración y NO reemplaza la validación de un profesional de salud.
"""

import os
import re
import json
import sqlite3
import hashlib
import secrets
import contextlib
from datetime import datetime, timedelta, date, time as dtime
from zoneinfo import ZoneInfo

import streamlit as st
import pandas as pd
import requests
import smtplib
from email.mime.text import MIMEText

from apscheduler.schedulers.background import BackgroundScheduler
from fpdf import FPDF

# ------------------------------------------------------------------
# CONFIGURACIÓN GENERAL
# ------------------------------------------------------------------

st.set_page_config(
    page_title="MediCampus",
    page_icon="💊",
    layout="wide",
)


def get_secret(name, default=""):
    """Lee un valor desde st.secrets (Streamlit Cloud) o variables de
    entorno (ejecución local), lo que esté disponible."""
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name, default)


DB_FILE = "medicampus.db"
ANTHROPIC_API_KEY = get_secret("ANTHROPIC_API_KEY")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-4-6"

EMAIL_USER = get_secret("EMAIL_USER")
EMAIL_PASSWORD = get_secret("EMAIL_PASSWORD")

# Correo del paciente que debe ver el panel de administrador. Independiente
# de EMAIL_USER (esa es solo la cuenta SMTP de envío). Puede ser el mismo
# correo o uno distinto — quien inicie sesión con este correo ve el panel.
ADMIN_EMAIL = get_secret("ADMIN_EMAIL")

# Código de recuperación de contraseña. Sirve para restablecer tu propia
# contraseña desde la pantalla de login sin necesitar entrar primero
# (soluciona el candado circular: si pierdes tu contraseña de admin, ya no
# puedes entrar al panel de admin para resetearla). Configúralo en Secrets
# y NO lo compartas — quien lo tenga puede resetear cualquier contraseña.
RECOVERY_CODE = get_secret("RECOVERY_CODE")

# Zona horaria para calcular "próxima dosis" y enviar recordatorios a la
# hora local correcta, sin importar en qué región esté el servidor.
NOMBRE_ZONA_HORARIA = get_secret("TIMEZONE", "America/Bogota")
try:
    ZONA_HORARIA = ZoneInfo(NOMBRE_ZONA_HORARIA)
except Exception:
    ZONA_HORARIA = ZoneInfo("America/Bogota")

CORREO_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Iteraciones de PBKDF2 para el hash de contraseñas.
PBKDF2_ITERACIONES = 200_000

INTERACCIONES_DEMO = [
    {"a": "ibuprofeno", "b": "warfarina", "riesgo": "ALTO",
     "detalle": "Puede aumentar el riesgo de sangrado."},
    {"a": "ibuprofeno", "b": "metotrexato", "riesgo": "ALTO",
     "detalle": "Puede aumentar la toxicidad del metotrexato."},
    {"a": "acetaminofen", "b": "alcohol", "riesgo": "MEDIO",
     "detalle": "Puede aumentar el riesgo de daño hepático."},
    {"a": "anticonceptivos", "b": "antibioticos", "riesgo": "MEDIO",
     "detalle": "Algunos antibióticos pueden reducir la eficacia anticonceptiva."},
    {"a": "ansioliticos", "b": "alcohol", "riesgo": "ALTO",
     "detalle": "Riesgo de sedación excesiva y depresión respiratoria."},
    {"a": "ibuprofeno", "b": "losartan", "riesgo": "MEDIO",
     "detalle": "Puede reducir el efecto antihipertensivo y afectar el riñón."},
]

FRECUENCIA_HORAS = {
    "Cada 6 horas": 6,
    "Cada 8 horas": 8,
    "Cada 12 horas": 12,
    "Cada 24 horas": 24,
}

TIPOS_HORARIO = [
    "Cada cierto número de horas",
    "A una o varias horas fijas cada día",
    "Relacionado con una comida",
]

COMIDAS = ["Desayuno", "Almuerzo", "Cena"]
MOMENTO_COMIDA = ["Antes", "Después"]

HORARIOS_COMIDA_DEFECTO = {
    "Desayuno": "07:00",
    "Almuerzo": "12:30",
    "Cena": "19:00",
}

UMBRAL_STOCK_BAJO = 3  # dosis restantes para alertar recompra


def ahora_local():
    """Hora actual, consciente de la zona horaria configurada (TIMEZONE)."""
    return datetime.now(ZONA_HORARIA)


def parsear_fecha(texto_iso):
    """Convierte un ISO guardado en la BD a datetime consciente de zona
    horaria. Soporta valores antiguos guardados sin zona (naive), a los
    que les asigna la zona configurada."""
    dt = datetime.fromisoformat(texto_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZONA_HORARIA)
    return dt.astimezone(ZONA_HORARIA)


# ------------------------------------------------------------------
# CAPA DE BASE DE DATOS (SQLite)
# ------------------------------------------------------------------

@contextlib.contextmanager
def get_conn():
    conn = sqlite3.connect(DB_FILE, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL + busy_timeout reducen los bloqueos "database is locked" cuando
    # el scheduler de recordatorios escribe al mismo tiempo que un usuario.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 8000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pacientes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT NOT NULL,
                correo TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                pbkdf2_iteraciones INTEGER NOT NULL DEFAULT 200000,
                horarios_comida TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        # Migración suave para bases creadas con la v2.0 (sin la columna).
        columnas = [r["name"] for r in conn.execute("PRAGMA table_info(pacientes)").fetchall()]
        if "pbkdf2_iteraciones" not in columnas:
            conn.execute("ALTER TABLE pacientes ADD COLUMN pbkdf2_iteraciones INTEGER NOT NULL DEFAULT 200000")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS medicamentos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id INTEGER NOT NULL,
                nombre TEXT NOT NULL,
                dosis TEXT,
                tipo_horario TEXT NOT NULL,
                frecuencia_horas INTEGER,
                horas_fijas TEXT,
                comida TEXT,
                momento TEXT,
                offset_min INTEGER,
                hora_inicio TEXT NOT NULL,
                duracion_dias INTEGER,
                cantidad_total REAL,
                cantidad_actual REAL,
                cantidad_por_toma REAL DEFAULT 1,
                activo INTEGER DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY (patient_id) REFERENCES pacientes(id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS eventos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id INTEGER NOT NULL,
                medicamento_id INTEGER,
                medicamento_nombre TEXT,
                tipo TEXT NOT NULL,
                hora_programada TEXT,
                hora_evento TEXT NOT NULL,
                canal TEXT
            )
        """)


# ---------- utilidades de contraseña ----------

def _hash_password(password, salt, iteraciones=PBKDF2_ITERACIONES):
    derivado = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), iteraciones
    )
    return derivado.hex()


def crear_paciente(nombre, correo, password):
    salt = secrets.token_hex(16)
    pw_hash = _hash_password(password, salt)
    with get_conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO pacientes (nombre, correo, password_hash, salt, "
                "pbkdf2_iteraciones, horarios_comida, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (nombre, correo.lower().strip(), pw_hash, salt, PBKDF2_ITERACIONES,
                 json.dumps(HORARIOS_COMIDA_DEFECTO), ahora_local().isoformat()),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            raise ValueError("Ya existe una cuenta registrada con ese correo.")


def autenticar_paciente(correo, password):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM pacientes WHERE correo = ?", (correo.lower().strip(),)
        ).fetchone()
        if row is None:
            return None
        iteraciones = row["pbkdf2_iteraciones"] or PBKDF2_ITERACIONES
        if _hash_password(password, row["salt"], iteraciones) != row["password_hash"]:
            return None
        return dict(row)


def resetear_password_admin(patient_id, nueva_password):
    """Establece una nueva contraseña para un paciente. Pensado para que el
    administrador la use cuando un paciente no puede entrar a su cuenta."""
    salt = secrets.token_hex(16)
    pw_hash = _hash_password(nueva_password, salt)
    with get_conn() as conn:
        conn.execute(
            "UPDATE pacientes SET password_hash=?, salt=?, pbkdf2_iteraciones=? WHERE id=?",
            (pw_hash, salt, PBKDF2_ITERACIONES, patient_id),
        )


def obtener_paciente(patient_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM pacientes WHERE id = ?", (patient_id,)).fetchone()
        return dict(row) if row else None


def obtener_paciente_por_correo(correo):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM pacientes WHERE correo = ?", (correo.lower().strip(),)
        ).fetchone()
        return dict(row) if row else None


def actualizar_horarios_comida(patient_id, horarios_dict):
    with get_conn() as conn:
        conn.execute(
            "UPDATE pacientes SET horarios_comida = ? WHERE id = ?",
            (json.dumps(horarios_dict), patient_id),
        )


def eliminar_paciente(patient_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM medicamentos WHERE patient_id = ?", (patient_id,))
        conn.execute("DELETE FROM eventos WHERE patient_id = ?", (patient_id,))
        conn.execute("DELETE FROM pacientes WHERE id = ?", (patient_id,))


# ---------- medicamentos ----------

def crear_medicamento(patient_id, data):
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO medicamentos
               (patient_id, nombre, dosis, tipo_horario, frecuencia_horas, horas_fijas,
                comida, momento, offset_min, hora_inicio, duracion_dias,
                cantidad_total, cantidad_actual, cantidad_por_toma, activo, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
            (
                patient_id, data["nombre"], data.get("dosis"), data["tipo_horario"],
                data.get("frecuencia_horas"), json.dumps(data.get("horas_fijas", [])),
                data.get("comida"), data.get("momento"), data.get("offset_min"),
                data["hora_inicio"].isoformat(), data.get("duracion_dias"),
                data.get("cantidad_total"), data.get("cantidad_total"),
                data.get("cantidad_por_toma", 1), ahora_local().isoformat(),
            ),
        )
        return cur.lastrowid


def actualizar_medicamento(med_id, data):
    with get_conn() as conn:
        conn.execute(
            """UPDATE medicamentos SET
               nombre=?, dosis=?, tipo_horario=?, frecuencia_horas=?, horas_fijas=?,
               comida=?, momento=?, offset_min=?, duracion_dias=?,
               cantidad_total=?, cantidad_por_toma=?
               WHERE id=?""",
            (
                data["nombre"], data.get("dosis"), data["tipo_horario"],
                data.get("frecuencia_horas"), json.dumps(data.get("horas_fijas", [])),
                data.get("comida"), data.get("momento"), data.get("offset_min"),
                data.get("duracion_dias"), data.get("cantidad_total"),
                data.get("cantidad_por_toma", 1), med_id,
            ),
        )


def eliminar_medicamento(med_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM medicamentos WHERE id = ?", (med_id,))


def obtener_medicamentos(patient_id):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM medicamentos WHERE patient_id = ? AND activo = 1 ORDER BY id",
            (patient_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def descontar_stock(med_id, cantidad_por_toma):
    with get_conn() as conn:
        row = conn.execute("SELECT cantidad_actual FROM medicamentos WHERE id=?", (med_id,)).fetchone()
        if row is None or row["cantidad_actual"] is None:
            return
        nuevo = max(0, row["cantidad_actual"] - cantidad_por_toma)
        conn.execute("UPDATE medicamentos SET cantidad_actual=? WHERE id=?", (nuevo, med_id))


# ---------- eventos / historial ----------

def registrar_evento(patient_id, medicamento_id, medicamento_nombre, tipo, hora_programada, canal="email"):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO eventos (patient_id, medicamento_id, medicamento_nombre, tipo,
               hora_programada, hora_evento, canal) VALUES (?,?,?,?,?,?,?)""",
            (
                patient_id, medicamento_id, medicamento_nombre, tipo,
                hora_programada.isoformat() if hora_programada else None,
                ahora_local().isoformat(), canal,
            ),
        )


def ya_enviado_en_este_minuto(medicamento_id, minuto_dt):
    inicio = minuto_dt.replace(second=0, microsecond=0)
    fin = inicio + timedelta(minutes=1)
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM eventos
               WHERE medicamento_id=? AND tipo='recordatorio_enviado'
               AND hora_programada >= ? AND hora_programada < ?""",
            (medicamento_id, inicio.isoformat(), fin.isoformat()),
        ).fetchone()
        return row["n"] > 0


def obtener_eventos_df(patient_id):
    with get_conn() as conn:
        df = pd.read_sql_query(
            "SELECT hora_evento, tipo, medicamento_nombre, hora_programada, canal "
            "FROM eventos WHERE patient_id = ? ORDER BY hora_evento DESC",
            conn, params=(patient_id,),
        )
    return df


# ---------- pacientes fijos de demostración (v2.5) ----------

DOMINIO_DEMO = "example.com"   # dominio reservado: no pertenece a nadie
PASSWORD_DEMO = "demo1234"     # contraseña de todas las cuentas demo

# (nombre, medicamentos, frecuencia, recordatorio, hora de registro)
# frecuencia: 0 = 1 vez al día, 1 = 2 veces, 2 = 3 o más, 3 = varía
# recordatorio: 0 = a tiempo, 1 = llegó tarde, 2 = no llegó
PACIENTES_DEMO = [
    ("María Fernanda Gómez López", "Losartán", 0, 1, "12:01:11"),
    ("Carlos Andrés Rodríguez Pérez", "Metformina", 1, 0, "12:10:29"),
    ("Luisa Alejandra Martínez Ruiz", "Levotiroxina", 0, 2, "13:42:47"),
    ("Juan Sebastián Torres Díaz", "Atorvastatina", 0, 0, "13:44:27"),
    ("Ana Sofía Ramírez Castro", "Omeprazol", 0, 0, "13:48:33"),
    ("Diego Fernando Herrera Vargas", "Enalapril", 1, 1, "13:53:24"),
    ("Camila Andrea Suárez Morales", "Sertralina", 0, 0, "14:03:21"),
    ("Sara Aldana Gamba", "Ibuprofeno", 0, 0, "14:59:13"),
    ("Andrés Felipe Ortiz Guzmán", "Salbutamol", 3, 1, "14:59:31"),
    ("Fernanda Bravo", "Acetaminofén", 0, 0, "15:00:49"),
    ("María Paula", "Loratadina", 0, 0, "15:01:22"),
    ("Andrés Beltrán", "Metocarbamol", 0, 0, "15:02:22"),
    ("Tomás Nafat Betancur", "Antigripales", 3, 0, "15:02:35"),
    ("Valentina Isabel Cárdenas Rojas", "Metformina", 1, 0, "15:02:36"),
    ("Mariana García Rojas", "Acetaminofén", 1, 0, "15:04:11"),
    ("Jorge Iván Mendoza Cruz", "Losartán", 0, 1, "15:18:35"),
    ("Paula Ximena Salazar Peña", "Loratadina", 0, 0, "15:27:34"),
    ("Nikol Sofía Cortes Cubides", "Ibuprofeno", 3, 0, "15:58:15"),
    ("Karol Sofía Téllez", "Alegra", 0, 0, "16:07:29"),
    ("Miguel Ángel Vega Restrepo", "Captopril", 2, 0, "18:17:46"),
    ("Daniela Patricia Acosta Reyes", "Levotiroxina", 0, 0, "18:18:34"),
    ("Yamile Andrea Gamba Roa", "Levotiroxina", 0, 0, "18:18:50"),
    ("Laura Valentina Araque Cruz", "Atorvastatina", 3, 0, "18:19:09"),
    ("Santiago Alberto Muñoz Cárdenas", "Amoxicilina", 2, 1, "18:20:55"),
    ("Laura Cristina Jiménez Lozano", "Atorvastatina", 0, 1, "18:22:50"),
    ("Natalia Marcela Beltrán Duque", "Metformina", 1, 0, "18:23:28"),
    ("Santiago Salgado Sarmiento", "Symbicort", 0, 1, "18:24:14"),
    ("David Torres Sicua", "Atorvastatina y Esomeprazol", 1, 0, "18:24:48"),
    ("Óscar Iván Peña Villalobos", "Warfarina", 0, 0, "18:24:51"),
    ("Carolina Andrea Rincón Fajardo", "Alprazolam", 0, 0, "18:25:34"),
    ("Mariela Beltrán", "Ibuprofeno", 1, 0, "18:30:57"),
    ("Greis Katherin Aldana Pineda", "Loratadina", 0, 0, "18:42:54"),
]

HORAS_POR_FRECUENCIA_DEMO = {0: 24, 1: 12, 2: 8, 3: 8}


def _correo_demo(nombre):
    """primernombre.ultimoapellido@example.com, sin tildes y en minúsculas."""
    import unicodedata
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFD", nombre)
        if unicodedata.category(c) != "Mn"
    )
    partes = sin_tildes.lower().split()
    base = partes[0] if len(partes) == 1 else f"{partes[0]}.{partes[-1]}"
    return f"{base}@{DOMINIO_DEMO}"


@st.cache_resource
def cargar_pacientes_demo():
    """Crea las 32 cuentas demo si todavía no existen. Se ejecuta una vez
    por arranque de la app (cache_resource), así que si Streamlit Cloud
    reinicia y borra medicampus.db, las cuentas se vuelven a crear solas.
    Para desactivarlo, define CARGAR_DEMO = "0" en Secrets."""
    if str(get_secret("CARGAR_DEMO", "1")) == "0":
        return 0
    creados = 0
    with get_conn() as conn:
        usados = {r["correo"] for r in conn.execute("SELECT correo FROM pacientes")}
        for nombre, meds, frecuencia, recordatorio, hora in PACIENTES_DEMO:
            correo = _correo_demo(nombre)
            if correo in usados:
                continue  # ya existe: no duplicar
            usados.add(correo)
            registro = datetime.fromisoformat(f"2026-09-25T{hora}").replace(
                tzinfo=ZONA_HORARIA).isoformat()
            salt = secrets.token_hex(16)
            pid = conn.execute(
                "INSERT INTO pacientes (nombre, correo, password_hash, salt, "
                "pbkdf2_iteraciones, horarios_comida, created_at) VALUES (?,?,?,?,?,?,?)",
                (nombre, correo, _hash_password(PASSWORD_DEMO, salt), salt,
                 PBKDF2_ITERACIONES, json.dumps(HORARIOS_COMIDA_DEFECTO), registro),
            ).lastrowid

            for med in [m.strip() for m in meds.split(" y ")]:
                mid = conn.execute(
                    """INSERT INTO medicamentos (patient_id, nombre, dosis, tipo_horario,
                       frecuencia_horas, horas_fijas, hora_inicio, cantidad_por_toma,
                       activo, created_at) VALUES (?,?,?,?,?,?,?,1,1,?)""",
                    (pid, med, "Según receta", "frecuencia",
                     HORAS_POR_FRECUENCIA_DEMO[frecuencia], "[]", registro, registro),
                ).lastrowid

                if recordatorio != 2:  # 2 = "no me llegó ninguna notificación"
                    programada = datetime.fromisoformat(registro) + timedelta(hours=1)
                    enviado = programada + timedelta(minutes=8 if recordatorio == 1 else 0)
                    for tipo, momento, canal in (
                        ("recordatorio_enviado", enviado, "email"),
                        ("confirmado", enviado + timedelta(minutes=5), "app"),
                    ):
                        conn.execute(
                            "INSERT INTO eventos (patient_id, medicamento_id, medicamento_nombre, "
                            "tipo, hora_programada, hora_evento, canal) VALUES (?,?,?,?,?,?,?)",
                            (pid, mid, med, tipo, programada.isoformat(),
                             momento.isoformat(), canal),
                        )
            creados += 1
    return creados


# ------------------------------------------------------------------
# LÓGICA DE HORARIOS
# ------------------------------------------------------------------

def esta_finalizado(med):
    if med.get("duracion_dias"):
        inicio = parsear_fecha(med["hora_inicio"])
        fin = inicio.date() + timedelta(days=int(med["duracion_dias"]))
        return ahora_local().date() > fin
    return False


def calcular_proxima_dosis(med, horarios_comida):
    """Calcula el próximo datetime de toma (consciente de zona horaria).
    Devuelve None si el tratamiento ya finalizó según su duración."""
    if esta_finalizado(med):
        return None

    ahora = ahora_local()
    tipo = med["tipo_horario"]

    if tipo == "frecuencia":
        proxima = parsear_fecha(med["hora_inicio"])
        frecuencia = med["frecuencia_horas"] or 8
        while proxima < ahora:
            proxima += timedelta(hours=frecuencia)
        return proxima

    if tipo == "fijo":
        horas = json.loads(med["horas_fijas"] or "[]") or ["08:00"]
        candidatos = []
        for h in horas:
            hh, mm = [int(x) for x in h.split(":")]
            candidato = datetime.combine(ahora.date(), dtime(hh, mm), tzinfo=ZONA_HORARIA)
            if candidato < ahora:
                candidato += timedelta(days=1)
            candidatos.append(candidato)
        return min(candidatos)

    if tipo == "comida":
        comida = med["comida"]
        momento = med["momento"]
        offset = med["offset_min"] or 0
        hora_comida_str = horarios_comida.get(comida, HORARIOS_COMIDA_DEFECTO[comida])
        hh, mm = [int(x) for x in hora_comida_str.split(":")]
        base = datetime.combine(ahora.date(), dtime(hh, mm), tzinfo=ZONA_HORARIA)
        objetivo = base - timedelta(minutes=offset) if momento == "Antes" else base + timedelta(minutes=offset)
        if objetivo < ahora:
            objetivo += timedelta(days=1)
        return objetivo

    return None


def descripcion_horario(med):
    tipo = med["tipo_horario"]
    if tipo == "frecuencia":
        return f"cada {med['frecuencia_horas']} horas"
    if tipo == "fijo":
        horas = json.loads(med["horas_fijas"] or "[]")
        return "todos los días a las " + ", ".join(horas) if horas else "hora fija no especificada"
    if tipo == "comida":
        return f"{med['momento'].lower()} de {med['comida'].lower()} ({med['offset_min']} min)"
    return "horario no especificado"


# ------------------------------------------------------------------
# ENVÍO DE CORREOS
# ------------------------------------------------------------------

def _enviar_smtp(destinatario, asunto, cuerpo):
    msg = MIMEText(cuerpo)
    msg["Subject"] = asunto
    msg["From"] = EMAIL_USER
    msg["To"] = destinatario
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
        server.starttls()
        server.login(EMAIL_USER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_USER, [destinatario], msg.as_string())


def enviar_recordatorio_email(paciente, med, hora_toma, manual=True):
    if not EMAIL_USER or not EMAIL_PASSWORD:
        return False, "No hay EMAIL_USER / EMAIL_PASSWORD configurados en Secrets."
    destinatario = paciente.get("correo", "")
    if not destinatario:
        return False, "El paciente no tiene correo registrado."

    asunto = f"⏰ MediCampus — Recordatorio: {med['nombre']}"
    cuerpo = (
        f"Hola {paciente['nombre']},\n\n"
        f"Este es tu recordatorio de MediCampus.\n\n"
        f"Medicamento: {med['nombre']}\n"
        f"Dosis: {med['dosis']}\n"
        f"Debes tomarlo a las: {hora_toma.strftime('%d/%m/%Y %H:%M')} ({NOMBRE_ZONA_HORARIA})\n"
        f"Horario: {descripcion_horario(med)}\n\n"
        f"Ingresa a MediCampus y marca 'Ya la tomé' cuando la tomes, "
        f"para llevar tu registro de adherencia.\n\n"
        f"— MediCampus (prototipo académico, no reemplaza indicación médica)"
    )
    try:
        _enviar_smtp(destinatario, asunto, cuerpo)
        registrar_evento(paciente["id"], med["id"], med["nombre"], "recordatorio_enviado", hora_toma)
        return True, f"Correo enviado a {paciente['nombre']} ({destinatario}) ✅"
    except Exception as e:
        return False, f"Error enviando correo: {e}"


def revisar_y_enviar_recordatorios():
    """Job de fondo (APScheduler): revisa TODOS los pacientes y medicamentos
    activos y envía un correo si la hora actual coincide (mismo minuto) con
    la próxima dosis calculada y aún no se ha enviado ese recordatorio.

    Limitación conocida: este job solo corre mientras el proceso de
    Streamlit esté activo. En Streamlit Community Cloud, si la app se
    "duerme" por inactividad, el scheduler se detiene con ella y no se
    enviarán recordatorios hasta que alguien vuelva a abrir la app."""
    if not EMAIL_USER or not EMAIL_PASSWORD:
        return
    try:
        with get_conn() as conn:
            pacientes = [dict(r) for r in conn.execute("SELECT * FROM pacientes").fetchall()]
        ahora = ahora_local()
        for pac in pacientes:
            # v2.5: las cuentas de demostración no reciben correos reales.
            if pac["correo"].endswith("@" + DOMINIO_DEMO):
                continue
            horarios_comida = json.loads(pac["horarios_comida"])
            meds = obtener_medicamentos(pac["id"])
            for med in meds:
                proxima = calcular_proxima_dosis(med, horarios_comida)
                if proxima is None:
                    continue
                if proxima.replace(second=0, microsecond=0) == ahora.replace(second=0, microsecond=0):
                    if not ya_enviado_en_este_minuto(med["id"], proxima):
                        enviar_recordatorio_email(pac, med, proxima, manual=False)
    except Exception:
        pass  # el job de fondo nunca debe tumbar la app


@st.cache_resource
def iniciar_scheduler():
    if not EMAIL_USER or not EMAIL_PASSWORD:
        return None
    sched = BackgroundScheduler(daemon=True, timezone=str(ZONA_HORARIA))
    sched.add_job(revisar_y_enviar_recordatorios, "interval", minutes=1,
                  id="job_recordatorios", replace_existing=True)
    sched.start()
    return sched


# ------------------------------------------------------------------
# LLAMADAS A LA IA (Anthropic API)
# ------------------------------------------------------------------

def _llamar_claude(prompt, system=""):
    if not ANTHROPIC_API_KEY:
        return None
    try:
        resp = requests.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 500,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        return "".join(b.get("text", "") for b in data.get("content", []))
    except Exception as e:
        return f"[Error llamando a la IA: {e}]"


def extraer_receta_con_ia(texto_libre):
    system = (
        "Extraes datos de recetas médicas en español y respondes SOLO con JSON "
        "válido, sin texto adicional, con este formato exacto: "
        '{"nombre": "...", "dosis": "...", "frecuencia_horas": numero, '
        '"duracion_dias": numero_o_null}'
    )
    respuesta = _llamar_claude(texto_libre, system=system)

    if respuesta and not respuesta.startswith("[Error"):
        try:
            limpio = respuesta.strip().strip("`").replace("json\n", "")
            datos = json.loads(limpio)
            return datos, "ia"
        except Exception:
            pass

    texto = texto_libre.lower()
    horas = 8
    for frase, h in [("cada 6", 6), ("cada 8", 8), ("cada 12", 12), ("cada 24", 24), ("diari", 24)]:
        if frase in texto:
            horas = h
            break
    duracion = None
    m = re.search(r"por\s+(\d+)\s*d[ií]as", texto)
    if m:
        duracion = int(m.group(1))
    primera_palabra = texto_libre.strip().split(" ")[0] if texto_libre.strip() else "Medicamento"
    return (
        {"nombre": primera_palabra.capitalize(), "dosis": "Ver receta original",
         "frecuencia_horas": horas, "duracion_dias": duracion},
        "respaldo",
    )


def responder_pregunta_ia(pregunta, medicamentos):
    contexto = ", ".join(m["nombre"] for m in medicamentos) or "ninguno registrado"
    system = (
        "Eres un asistente educativo de MediCampus, un prototipo académico. "
        "Respondes de forma breve y clara sobre uso general de medicamentos. "
        "SIEMPRE aclaras que no reemplazas a un profesional de salud. "
        "No das diagnósticos ni indicaciones de dosis distintas a las recetadas."
    )
    prompt = f"Medicamentos actuales del paciente: {contexto}.\nPregunta: {pregunta}"
    respuesta = _llamar_claude(prompt, system=system)
    if respuesta:
        return respuesta
    return (
        "⚠️ Modo de respaldo (sin conexión a la IA): configura ANTHROPIC_API_KEY "
        "para respuestas generadas, o consulta a un profesional de salud."
    )


# ------------------------------------------------------------------
# LÓGICA DE NEGOCIO
# ------------------------------------------------------------------

def verificar_interacciones(medicamentos):
    nombres = [m["nombre"].strip().lower() for m in medicamentos]
    alertas = []
    for combo in INTERACCIONES_DEMO:
        if combo["a"] in nombres and combo["b"] in nombres:
            alertas.append(combo)
    return alertas


def generar_pdf_historial(paciente, medicamentos, eventos_df):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "MediCampus - Historial del paciente", ln=True)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 8, f"Paciente: {paciente['nombre']}", ln=True)
    pdf.cell(0, 8, f"Correo: {paciente['correo']}", ln=True)
    pdf.cell(0, 8, f"Generado: {ahora_local().strftime('%d/%m/%Y %H:%M')} ({NOMBRE_ZONA_HORARIA})", ln=True)
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "Medicamentos registrados", ln=True)
    pdf.set_font("Helvetica", "", 10)
    if medicamentos:
        for m in medicamentos:
            estado = "Finalizado" if esta_finalizado(m) else "Activo"
            linea = f"- {m['nombre']} | {m['dosis']} | {descripcion_horario(m)} | Estado: {estado}"
            pdf.multi_cell(0, 6, linea)
    else:
        pdf.multi_cell(0, 6, "Sin medicamentos registrados.")
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "Historial de eventos", ln=True)
    pdf.set_font("Helvetica", "", 9)
    if eventos_df.empty:
        pdf.multi_cell(0, 6, "Sin eventos registrados.")
    else:
        for _, row in eventos_df.iterrows():
            linea = f"{row['hora_evento']} | {row['tipo']} | {row['medicamento_nombre']}"
            pdf.multi_cell(0, 5, linea)

    pdf.ln(6)
    pdf.set_font("Helvetica", "I", 8)
    pdf.multi_cell(0, 5, "Prototipo académico. La base de interacciones es de demostración "
                         "y no reemplaza la validación de un profesional de salud.")

    return bytes(pdf.output())


# ------------------------------------------------------------------
# ESTADO DE SESIÓN
# ------------------------------------------------------------------

def init_state():
    if "patient_id" not in st.session_state:
        st.session_state.patient_id = None
    if "chat_historial" not in st.session_state:
        st.session_state.chat_historial = []
    if "confirmar_borrado_cuenta" not in st.session_state:
        st.session_state.confirmar_borrado_cuenta = False


# ------------------------------------------------------------------
# PANTALLA DE LOGIN / REGISTRO
# ------------------------------------------------------------------

def pantalla_login():
    st.title("💊 MediCampus")
    st.caption(
        "Recordatorios de medicamentos por correo, para cualquier persona. "
        "Regístrate una sola vez: tus medicamentos quedan guardados en tu cuenta."
    )

    tab_login, tab_registro, tab_recuperar = st.tabs(
        ["Iniciar sesión", "Crear cuenta", "¿Olvidaste tu contraseña?"]
    )

    with tab_login:
        with st.form("form_login"):
            correo = st.text_input("Correo")
            password = st.text_input("Contraseña", type="password")
            entrar = st.form_submit_button("Iniciar sesión")
            if entrar:
                if not correo or not password:
                    st.error("Ingresa correo y contraseña.")
                else:
                    paciente = autenticar_paciente(correo, password)
                    if paciente:
                        st.session_state.patient_id = paciente["id"]
                        st.rerun()
                    else:
                        st.error("Correo o contraseña incorrectos.")

    with tab_registro:
        with st.form("form_registro"):
            nombre = st.text_input("Nombre completo")
            correo_r = st.text_input("Correo (aquí te llegarán tus recordatorios)")
            password_r = st.text_input("Contraseña", type="password")
            password_r2 = st.text_input("Confirmar contraseña", type="password")
            crear = st.form_submit_button("Crear cuenta")
            if crear:
                errores = []
                if not nombre.strip():
                    errores.append("El nombre es obligatorio.")
                if not CORREO_REGEX.match(correo_r or ""):
                    errores.append("El correo no tiene un formato válido.")
                if len(password_r or "") < 4:
                    errores.append("La contraseña debe tener al menos 4 caracteres.")
                if password_r != password_r2:
                    errores.append("Las contraseñas no coinciden.")
                if errores:
                    for e in errores:
                        st.error(e)
                else:
                    try:
                        pid = crear_paciente(nombre.strip(), correo_r, password_r)
                        st.session_state.patient_id = pid
                        st.success("Cuenta creada. ¡Bienvenido/a!")
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))

    with tab_recuperar:
        st.caption(
            "Restablece tu contraseña si la olvidaste, usando el código de "
            "recuperación configurado en Secrets (RECOVERY_CODE). Si tú eres "
            "quien administra la app, es el mismo código que pusiste ahí."
        )
        if not RECOVERY_CODE:
            st.warning(
                "RECOVERY_CODE no está configurado en Secrets. Agrégalo (una "
                "cadena que solo tú conozcas) para poder usar esta opción."
            )
        else:
            with st.form("form_recuperar"):
                correo_rec = st.text_input("Tu correo", key="rec_correo")
                codigo = st.text_input("Código de recuperación", type="password", key="rec_codigo")
                nueva1 = st.text_input("Nueva contraseña", type="password", key="rec_pw1")
                nueva2 = st.text_input("Confirmar nueva contraseña", type="password", key="rec_pw2")
                enviar_rec = st.form_submit_button("Restablecer contraseña")
                if enviar_rec:
                    errores = []
                    if codigo != RECOVERY_CODE:
                        errores.append("Código de recuperación incorrecto.")
                    if len(nueva1 or "") < 4:
                        errores.append("La nueva contraseña debe tener al menos 4 caracteres.")
                    if nueva1 != nueva2:
                        errores.append("Las contraseñas no coinciden.")
                    paciente_rec = obtener_paciente_por_correo(correo_rec) if correo_rec else None
                    if not paciente_rec:
                        errores.append("No existe ninguna cuenta registrada con ese correo.")
                    if errores:
                        for e in errores:
                            st.error(e)
                    else:
                        resetear_password_admin(paciente_rec["id"], nueva1)
                        st.success(
                            "Contraseña restablecida. Ya puedes iniciar sesión con "
                            "tu correo y la nueva contraseña, en la pestaña de al lado."
                        )


# ------------------------------------------------------------------
# PANEL DE ADMINISTRADOR
# ------------------------------------------------------------------

def es_admin(paciente):
    """True si el correo del paciente logueado coincide con ADMIN_EMAIL.
    ADMIN_EMAIL es un secreto independiente de EMAIL_USER: define quién
    administra la app, no desde dónde se envían los correos."""
    if not ADMIN_EMAIL:
        return False
    return paciente["correo"].lower().strip() == ADMIN_EMAIL.lower().strip()


def _fila_paciente_admin(p):
    """Dibuja una fila de la tabla de pacientes con botones de acción
    (🔑 resetear contraseña, 🗑️ eliminar cuenta) directamente en la fila."""
    c1, c2, c3, c4, c5 = st.columns([2.2, 3, 2, 0.8, 0.8])
    c1.write(p["nombre"])
    c2.write(p["correo"])
    c3.write(p["created_at"][:16].replace("T", " "))

    with c4:
        if st.button("🔑", key=f"admin_reset_btn_{p['id']}", help="Restablecer contraseña"):
            st.session_state[f"admin_mostrar_reset_{p['id']}"] = True

    with c5:
        if st.button("🗑️", key=f"admin_del_btn_{p['id']}", help="Eliminar cuenta"):
            st.session_state[f"admin_mostrar_borrar_{p['id']}"] = True

    if st.session_state.get(f"admin_mostrar_reset_{p['id']}"):
        with st.form(f"form_admin_reset_row_{p['id']}"):
            nueva_pw = st.text_input(
                "Nueva contraseña (vacío = se genera una aleatoria)",
                type="password", key=f"admin_nueva_pw_row_{p['id']}",
            )
            colf1, colf2 = st.columns(2)
            confirmar = colf1.form_submit_button("Confirmar")
            cancelar = colf2.form_submit_button("Cancelar")
            if confirmar:
                pw_final = nueva_pw.strip() if nueva_pw.strip() else secrets.token_urlsafe(9)
                if len(pw_final) < 4:
                    st.error("La contraseña debe tener al menos 4 caracteres.")
                else:
                    resetear_password_admin(p["id"], pw_final)
                    st.session_state[f"admin_mostrar_reset_{p['id']}"] = False
                    st.success(
                        f"Nueva contraseña para {p['nombre']}: `{pw_final}` — "
                        "cómunicasela de forma segura, no se envía sola por correo."
                    )
            if cancelar:
                st.session_state[f"admin_mostrar_reset_{p['id']}"] = False
                st.rerun()

    if st.session_state.get(f"admin_mostrar_borrar_{p['id']}"):
        st.warning(f"¿Eliminar la cuenta de **{p['nombre']}** ({p['correo']}) de forma permanente? "
                   "Esto borra también sus medicamentos y su historial.")
        colb1, colb2 = st.columns(2)
        with colb1:
            if st.button("Sí, eliminar", key=f"admin_del_si_{p['id']}"):
                eliminar_paciente(p["id"])
                st.session_state[f"admin_mostrar_borrar_{p['id']}"] = False
                st.success(f"Cuenta de {p['nombre']} eliminada.")
                st.rerun()
        with colb2:
            if st.button("Cancelar", key=f"admin_del_no_{p['id']}"):
                st.session_state[f"admin_mostrar_borrar_{p['id']}"] = False
                st.rerun()

    st.divider()


def seccion_admin():
    st.subheader("🛠️ Panel de administrador")
    st.caption(
        "Visible solo para la cuenta cuyo correo coincide con ADMIN_EMAIL "
        "(configurado en Secrets). Los demás pacientes no ven esta sección."
    )

    with get_conn() as conn:
        n_pacientes = conn.execute("SELECT COUNT(*) AS n FROM pacientes").fetchone()["n"]
        n_meds_activos = conn.execute(
            "SELECT COUNT(*) AS n FROM medicamentos WHERE activo = 1"
        ).fetchone()["n"]
        n_eventos = conn.execute("SELECT COUNT(*) AS n FROM eventos").fetchone()["n"]
        n_recordatorios = conn.execute(
            "SELECT COUNT(*) AS n FROM eventos WHERE tipo = 'recordatorio_enviado'"
        ).fetchone()["n"]
        todos_pacientes = [dict(r) for r in conn.execute(
            "SELECT id, nombre, correo, created_at FROM pacientes ORDER BY created_at DESC"
        ).fetchall()]
        eventos_recientes_df = pd.read_sql_query(
            """SELECT e.hora_evento, p.nombre AS paciente, e.tipo, e.medicamento_nombre
               FROM eventos e JOIN pacientes p ON p.id = e.patient_id
               ORDER BY e.hora_evento DESC LIMIT 25""",
            conn,
        )

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Pacientes registrados", n_pacientes)
    col2.metric("Medicamentos activos", n_meds_activos)
    col3.metric("Eventos totales", n_eventos)
    col4.metric("Recordatorios enviados", n_recordatorios)

    # ---------------------------------------------------------------
    # TABLA DE PACIENTES CON ACCIONES (v2.4): cada fila tiene sus
    # propios botones de 🔑 resetear contraseña y 🗑️ eliminar cuenta,
    # en vez de usar un selector aparte para elegir el paciente.
    # ---------------------------------------------------------------
    st.markdown("**Pacientes registrados**")
    if not todos_pacientes:
        st.info("Aún no hay pacientes registrados.")
    else:
        h1, h2, h3, h4, h5 = st.columns([2.2, 3, 2, 0.8, 0.8])
        h1.markdown("**Nombre**")
        h2.markdown("**Correo**")
        h3.markdown("**Registrado el**")
        h4.markdown("**🔑**")
        h5.markdown("**🗑️**")
        for p in todos_pacientes:
            _fila_paciente_admin(p)

        pacientes_df = pd.DataFrame(todos_pacientes)
        csv_bytes = pacientes_df.to_csv(index=False).encode("utf-8")
        st.download_button("⬇️ Descargar listado de pacientes (CSV)", data=csv_bytes,
                            file_name="pacientes_medicampus.csv", mime="text/csv")

    st.markdown("**Últimos 25 eventos (todos los pacientes)**")
    if eventos_recientes_df.empty:
        st.info("Aún no hay eventos registrados en toda la app.")
    else:
        st.dataframe(eventos_recientes_df, use_container_width=True)

    # ---------------------------------------------------------------
    # GESTIÓN DE MEDICAMENTOS POR PACIENTE: se mantiene aparte porque
    # cada paciente puede tener varios medicamentos (no cabe en una fila).
    # ---------------------------------------------------------------
    if not todos_pacientes:
        return

    st.markdown("---")
    st.markdown("**Medicamentos de un paciente**")
    st.caption("Elige un paciente para ver y, si hace falta, eliminar alguno de sus medicamentos.")

    opciones = {f"{p['nombre']} ({p['correo']})": p["id"] for p in todos_pacientes}
    seleccion = st.selectbox("Selecciona un paciente", list(opciones.keys()), key="admin_sel_paciente")
    pid_sel = opciones[seleccion]
    paciente_sel = obtener_paciente(pid_sel)

    if paciente_sel is None:
        st.warning("Ese paciente ya no existe (puede que acabes de eliminarlo).")
        return

    meds_sel = obtener_medicamentos(pid_sel)
    if meds_sel:
        st.write(f"Medicamentos activos de **{paciente_sel['nombre']}**:")
        for m in meds_sel:
            colm1, colm2 = st.columns([4, 1])
            with colm1:
                st.write(f"- {m['nombre']} ({m['dosis']}) — {descripcion_horario(m)}")
            with colm2:
                if st.button("🗑️ Eliminar", key=f"admin_borrar_med_{m['id']}"):
                    eliminar_medicamento(m["id"])
                    st.success(f"Medicamento '{m['nombre']}' eliminado.")
                    st.rerun()
    else:
        st.caption(f"{paciente_sel['nombre']} no tiene medicamentos activos.")


# ------------------------------------------------------------------
# SECCIONES DE LA APP (usuario autenticado)
# ------------------------------------------------------------------

def sidebar_cuenta(paciente):
    st.sidebar.header(f"👤 {paciente['nombre']}")
    st.sidebar.caption(paciente["correo"])
    if es_admin(paciente):
        st.sidebar.caption("🛠️ Cuenta de administrador")

    if st.sidebar.button("🚪 Cerrar sesión"):
        st.session_state.patient_id = None
        st.session_state.chat_historial = []
        st.rerun()

    st.sidebar.markdown("---")
    if not EMAIL_USER or not EMAIL_PASSWORD:
        st.sidebar.warning("Recordatorios por correo: no configurados (ver Secrets).")
    else:
        st.sidebar.success("Recordatorios automáticos activos ✅")
        st.sidebar.caption(
            f"Se revisan cada minuto (zona horaria: {NOMBRE_ZONA_HORARIA}) mientras "
            "la app esté activa en el servidor."
        )
        st.sidebar.caption(
            "⚠️ En Streamlit Community Cloud, si la app se 'duerme' por "
            "inactividad, los recordatorios automáticos se pausan hasta que "
            "alguien vuelva a abrirla. El envío manual (botón 📧) siempre funciona."
        )
    if not ANTHROPIC_API_KEY:
        st.sidebar.warning("Sin ANTHROPIC_API_KEY: modo de respaldo activo para la IA.")
    else:
        st.sidebar.success("Asistente IA conectado ✅")

    st.sidebar.markdown("---")
    with st.sidebar.expander("⚠️ Eliminar mi cuenta"):
        st.caption("Esto borra tu cuenta, medicamentos e historial de forma permanente.")
        pw_confirm = st.text_input("Confirma tu contraseña", type="password", key="pw_borrar")
        if st.button("Eliminar cuenta definitivamente"):
            if autenticar_paciente(paciente["correo"], pw_confirm):
                eliminar_paciente(paciente["id"])
                st.session_state.patient_id = None
                st.success("Cuenta eliminada.")
                st.rerun()
            else:
                st.error("Contraseña incorrecta.")


def seccion_horarios_comida(paciente):
    horarios = json.loads(paciente["horarios_comida"])
    with st.expander("⚙️ Tus horarios de comida (para tomas antes/después de comer)"):
        with st.form("form_horarios_comida"):
            col1, col2, col3 = st.columns(3)
            hh, mm = [int(x) for x in horarios.get("Desayuno", HORARIOS_COMIDA_DEFECTO["Desayuno"]).split(":")]
            with col1:
                t1 = st.time_input("Desayuno", value=dtime(hh, mm))
            hh, mm = [int(x) for x in horarios.get("Almuerzo", HORARIOS_COMIDA_DEFECTO["Almuerzo"]).split(":")]
            with col2:
                t2 = st.time_input("Almuerzo", value=dtime(hh, mm))
            hh, mm = [int(x) for x in horarios.get("Cena", HORARIOS_COMIDA_DEFECTO["Cena"]).split(":")]
            with col3:
                t3 = st.time_input("Cena", value=dtime(hh, mm))
            if st.form_submit_button("Guardar horarios"):
                nuevos = {
                    "Desayuno": t1.strftime("%H:%M"),
                    "Almuerzo": t2.strftime("%H:%M"),
                    "Cena": t3.strftime("%H:%M"),
                }
                actualizar_horarios_comida(paciente["id"], nuevos)
                st.success("Horarios de comida actualizados.")
                st.rerun()


def _form_campos_horario(prefix, valores=None):
    """Dibuja los campos de horario y devuelve un dict parcial con lo elegido.
    `valores` permite precargar datos al editar."""
    valores = valores or {}
    tipo_horario = st.radio(
        "¿Cómo se debe tomar?", TIPOS_HORARIO, key=f"{prefix}_tipo", horizontal=True,
        index=TIPOS_HORARIO.index(valores.get("tipo_horario_label", TIPOS_HORARIO[0])),
    )

    resultado = {}
    if tipo_horario == "Cada cierto número de horas":
        opciones = list(FRECUENCIA_HORAS.keys())
        idx = 0
        if valores.get("frecuencia_horas"):
            inv = {v: k for k, v in FRECUENCIA_HORAS.items()}
            idx = opciones.index(inv.get(valores["frecuencia_horas"], opciones[0]))
        frecuencia = st.selectbox("Frecuencia", opciones, index=idx, key=f"{prefix}_frecuencia")
        resultado["tipo_horario"] = "frecuencia"
        resultado["frecuencia_horas"] = FRECUENCIA_HORAS[frecuencia]

    elif tipo_horario == "A una o varias horas fijas cada día":
        n = st.number_input("¿Cuántas veces al día?", min_value=1, max_value=4,
                             value=max(1, len(valores.get("horas_fijas", [])) or 1), key=f"{prefix}_n")
        horas_existentes = valores.get("horas_fijas", [])
        horas = []
        cols = st.columns(int(n))
        for i in range(int(n)):
            default = dtime(21, 0)
            if i < len(horas_existentes):
                hh, mm = [int(x) for x in horas_existentes[i].split(":")]
                default = dtime(hh, mm)
            with cols[i]:
                t = st.time_input(f"Hora {i + 1}", value=default, key=f"{prefix}_hora_{i}")
            horas.append(t.strftime("%H:%M"))
        resultado["tipo_horario"] = "fijo"
        resultado["horas_fijas"] = horas

    else:
        colc1, colc2, colc3 = st.columns(3)
        with colc1:
            comida = st.selectbox("Comida", COMIDAS, key=f"{prefix}_comida",
                                   index=COMIDAS.index(valores.get("comida", COMIDAS[0])) if valores.get("comida") in COMIDAS else 0)
        with colc2:
            momento = st.selectbox("Momento", MOMENTO_COMIDA, key=f"{prefix}_momento",
                                    index=MOMENTO_COMIDA.index(valores.get("momento", MOMENTO_COMIDA[0])) if valores.get("momento") in MOMENTO_COMIDA else 0)
        with colc3:
            offset_min = st.number_input("Minutos antes/después", min_value=0, max_value=180,
                                          value=int(valores.get("offset_min") or 30), step=5, key=f"{prefix}_offset")
        resultado["tipo_horario"] = "comida"
        resultado["comida"] = comida
        resultado["momento"] = momento
        resultado["offset_min"] = offset_min

    return resultado


def seccion_registrar_medicamento(paciente):
    st.subheader("💊 Registrar nuevo medicamento")
    tab_manual, tab_ia = st.tabs(["Formulario manual", "Pegar receta (IA extrae los datos)"])

    with tab_manual:
        col1, col2 = st.columns(2)
        with col1:
            nombre = st.text_input("Nombre del medicamento", key="man_nombre")
        with col2:
            dosis = st.text_input("Dosis (ej. 500 mg)", key="man_dosis")

        horario = _form_campos_horario("man")

        st.markdown("**Duración del tratamiento (opcional)**")
        col3, col4 = st.columns(2)
        with col3:
            indefinido = st.checkbox("Tratamiento indefinido / continuo", value=True, key="man_indef")
        with col4:
            duracion_dias = None
            if not indefinido:
                duracion_dias = st.number_input("Duración en días", min_value=1, max_value=365,
                                                 value=5, key="man_duracion")

        st.markdown("**Control de stock (opcional, para alertar recompra)**")
        col5, col6 = st.columns(2)
        with col5:
            llevar_stock = st.checkbox("Llevar control de stock", value=False, key="man_stock_check")
        cantidad_total = None
        cantidad_por_toma = 1
        if llevar_stock:
            with col5:
                cantidad_total = st.number_input("Unidades totales disponibles (ej. pastillas)",
                                                  min_value=1, value=20, key="man_cant_total")
            with col6:
                cantidad_por_toma = st.number_input("Unidades por toma", min_value=1, value=1,
                                                      key="man_cant_toma")

        if st.button("➕ Agregar medicamento", key="btn_manual"):
            if nombre:
                data = {
                    "nombre": nombre,
                    "dosis": dosis or "No especificada",
                    "hora_inicio": ahora_local(),
                    "duracion_dias": duracion_dias,
                    "cantidad_total": cantidad_total,
                    "cantidad_por_toma": cantidad_por_toma,
                    **horario,
                }
                crear_medicamento(paciente["id"], data)
                st.success(f"'{nombre}' agregado correctamente.")
                st.rerun()
            else:
                st.error("Escribe al menos el nombre del medicamento.")

    with tab_ia:
        texto = st.text_area(
            "Pega aquí el texto de la receta o indicación médica",
            placeholder="Ej: Ibuprofeno 400mg cada 8 horas por 5 días",
        )
        st.caption(
            "La IA extrae nombre, dosis, frecuencia y duración si se menciona. "
            "Si necesita hora fija, relación con comidas o stock, ajústalo luego con "
            "'Editar' en el panel de medicamentos."
        )
        if st.button("Extraer con IA", key="btn_ia"):
            if texto.strip():
                with st.spinner("Analizando receta..."):
                    datos, modo = extraer_receta_con_ia(texto)
                data = {
                    "nombre": datos.get("nombre", "Medicamento"),
                    "dosis": datos.get("dosis", "No especificada"),
                    "tipo_horario": "frecuencia",
                    "frecuencia_horas": int(datos.get("frecuencia_horas") or 8),
                    "hora_inicio": ahora_local(),
                    "duracion_dias": datos.get("duracion_dias"),
                }
                crear_medicamento(paciente["id"], data)
                etiqueta = "🤖 IA" if modo == "ia" else "🛟 modo de respaldo"
                st.success(f"Agregado ({etiqueta}): {datos}")
                st.rerun()
            else:
                st.error("Pega el texto de la receta primero.")


def seccion_editar_medicamento(med):
    with st.expander(f"✏️ Editar {med['nombre']}"):
        with st.form(f"form_editar_{med['id']}"):
            nombre = st.text_input("Nombre", value=med["nombre"])
            dosis = st.text_input("Dosis", value=med["dosis"] or "")

            valores_precarga = dict(med)
            tipo_a_label = {
                "frecuencia": TIPOS_HORARIO[0],
                "fijo": TIPOS_HORARIO[1],
                "comida": TIPOS_HORARIO[2],
            }
            valores_precarga["tipo_horario_label"] = tipo_a_label.get(med["tipo_horario"], TIPOS_HORARIO[0])
            if med["tipo_horario"] == "fijo":
                valores_precarga["horas_fijas"] = json.loads(med["horas_fijas"] or "[]")

            horario = _form_campos_horario(f"edit_{med['id']}", valores_precarga)

            indefinido = st.checkbox("Tratamiento indefinido / continuo",
                                      value=not bool(med["duracion_dias"]), key=f"edit_indef_{med['id']}")
            duracion_dias = None
            if not indefinido:
                duracion_dias = st.number_input("Duración en días", min_value=1, max_value=365,
                                                  value=int(med["duracion_dias"] or 5), key=f"edit_dur_{med['id']}")

            llevar_stock = st.checkbox("Llevar control de stock", value=med["cantidad_total"] is not None,
                                        key=f"edit_stock_{med['id']}")
            cantidad_total = None
            cantidad_por_toma = med["cantidad_por_toma"] or 1
            if llevar_stock:
                cantidad_total = st.number_input("Unidades totales (reinicia el stock actual)", min_value=1,
                                                  value=int(med["cantidad_total"] or 20), key=f"edit_ct_{med['id']}")
                cantidad_por_toma = st.number_input("Unidades por toma", min_value=1,
                                                      value=int(med["cantidad_por_toma"] or 1), key=f"edit_cpt_{med['id']}")

            guardar = st.form_submit_button("Guardar cambios")
            if guardar:
                if not nombre.strip():
                    st.error("El nombre no puede estar vacío.")
                else:
                    data = {
                        "nombre": nombre, "dosis": dosis, "duracion_dias": duracion_dias,
                        "cantidad_total": cantidad_total, "cantidad_por_toma": cantidad_por_toma,
                        **horario,
                    }
                    actualizar_medicamento(med["id"], data)
                    if cantidad_total is not None:
                        with get_conn() as conn:
                            conn.execute("UPDATE medicamentos SET cantidad_actual=? WHERE id=?",
                                         (cantidad_total, med["id"]))
                    st.success("Medicamento actualizado.")
                    st.rerun()


def seccion_panel(paciente):
    st.subheader(f"📋 Tus medicamentos ({paciente['correo']})")
    horarios_comida = json.loads(paciente["horarios_comida"])
    medicamentos = obtener_medicamentos(paciente["id"])

    if not medicamentos:
        st.info("Aún no tienes medicamentos registrados. Agrega el primero arriba.")
        return

    activos = [m for m in medicamentos if not esta_finalizado(m)]
    finalizados = [m for m in medicamentos if esta_finalizado(m)]

    filas = []
    for m in activos:
        proxima = calcular_proxima_dosis(m, horarios_comida)
        filas.append({
            "Medicamento": m["nombre"],
            "Dosis": m["dosis"],
            "Horario": descripcion_horario(m),
            "Próxima dosis": proxima.strftime("%d/%m/%Y %H:%M") if proxima else "-",
        })
    if filas:
        st.dataframe(pd.DataFrame(filas), use_container_width=True)

    for m in activos:
        proxima = calcular_proxima_dosis(m, horarios_comida)
        with st.container(border=True):
            col_info, col_acciones = st.columns([3, 2])
            with col_info:
                st.markdown(f"**{m['nombre']}** — {m['dosis']}")
                st.caption(f"{descripcion_horario(m)} · próxima toma: "
                           f"{proxima.strftime('%d/%m/%Y %H:%M') if proxima else '-'}")
                if m["cantidad_total"] is not None:
                    restante = m["cantidad_actual"]
                    st.caption(f"Stock: {restante:g} unidades restantes")
                    if restante is not None and restante <= (m["cantidad_por_toma"] or 1) * UMBRAL_STOCK_BAJO:
                        st.warning("⚠️ Stock bajo — considera recomprar pronto.")

            with col_acciones:
                b1, b2, b3, b4 = st.columns(4)
                with b1:
                    if st.button("📧", key=f"enviar_{m['id']}", help="Enviar recordatorio ahora"):
                        ok, mensaje = enviar_recordatorio_email(paciente, m, proxima or ahora_local())
                        st.success(mensaje) if ok else st.error(mensaje)
                with b2:
                    if st.button("✅", key=f"tomado_{m['id']}", help="Marcar como tomado ahora"):
                        registrar_evento(paciente["id"], m["id"], m["nombre"], "confirmado", ahora_local())
                        if m["cantidad_total"] is not None:
                            descontar_stock(m["id"], m["cantidad_por_toma"] or 1)
                        st.success("Toma registrada ✅")
                        st.rerun()
                with b3:
                    if st.button("🗑️", key=f"borrar_{m['id']}", help="Eliminar medicamento"):
                        st.session_state[f"confirmar_borrar_{m['id']}"] = True
                with b4:
                    st.caption("✏️ abajo")

            if st.session_state.get(f"confirmar_borrar_{m['id']}"):
                st.warning(f"¿Eliminar '{m['nombre']}' definitivamente?")
                cc1, cc2 = st.columns(2)
                with cc1:
                    if st.button("Sí, eliminar", key=f"confirma_si_{m['id']}"):
                        eliminar_medicamento(m["id"])
                        st.session_state[f"confirmar_borrar_{m['id']}"] = False
                        st.rerun()
                with cc2:
                    if st.button("Cancelar", key=f"confirma_no_{m['id']}"):
                        st.session_state[f"confirmar_borrar_{m['id']}"] = False
                        st.rerun()

            seccion_editar_medicamento(m)

    if finalizados:
        with st.expander(f"✔️ Tratamientos finalizados ({len(finalizados)})"):
            for m in finalizados:
                st.write(f"- {m['nombre']} — {m['dosis']} ({descripcion_horario(m)})")
                if st.button("🗑️ Eliminar", key=f"borrar_fin_{m['id']}"):
                    eliminar_medicamento(m["id"])
                    st.rerun()

    alertas = verificar_interacciones(activos)
    if alertas:
        st.error("⚠️ Posibles interacciones detectadas (base de demostración):")
        for a in alertas:
            st.write(f"- **{a['a'].capitalize()} + {a['b'].capitalize()}** — Riesgo {a['riesgo']}: {a['detalle']}")
        st.caption("Verificación de demostración académica. Confirma siempre con un profesional de salud.")
    elif activos:
        st.success("No se detectaron interacciones en la base de demostración.")


def seccion_historial(paciente):
    st.subheader("🗂️ Tu historial de recordatorios y tomas")
    df = obtener_eventos_df(paciente["id"])
    if df.empty:
        st.info("Aún no hay eventos registrados.")
        return

    st.dataframe(df, use_container_width=True)

    col1, col2 = st.columns(2)
    with col1:
        csv_bytes = df.to_csv(index=False).encode("utf-8")
        st.download_button("⬇️ Descargar CSV", data=csv_bytes,
                            file_name=f"historial_{paciente['nombre']}.csv", mime="text/csv")
    with col2:
        medicamentos = obtener_medicamentos(paciente["id"])
        pdf_bytes = generar_pdf_historial(paciente, medicamentos, df)
        st.download_button("⬇️ Descargar PDF", data=pdf_bytes,
                            file_name=f"historial_{paciente['nombre']}.pdf", mime="application/pdf")


def seccion_chat(paciente):
    st.subheader("🤖 Asistente MediCampus")
    st.caption("Responde dudas generales. No reemplaza a un profesional de salud.")

    for rol, texto in st.session_state.chat_historial:
        with st.chat_message(rol):
            st.write(texto)

    pregunta = st.chat_input("Escribe tu pregunta...")
    if pregunta:
        st.session_state.chat_historial.append(("user", pregunta))
        with st.spinner("Pensando..."):
            respuesta = responder_pregunta_ia(pregunta, obtener_medicamentos(paciente["id"]))
        st.session_state.chat_historial.append(("assistant", respuesta))
        st.rerun()


# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------

def main():
    init_db()
    cargar_pacientes_demo()   # v2.5: crea los 32 pacientes fijos si faltan
    init_state()
    iniciar_scheduler()

    if not st.session_state.patient_id:
        pantalla_login()
        return

    paciente = obtener_paciente(st.session_state.patient_id)
    if paciente is None:
        st.session_state.patient_id = None
        st.rerun()
        return

    sidebar_cuenta(paciente)

    st.title("💊 MediCampus")
    st.markdown(
        "Recordatorios de medicamentos por correo, con horarios fijos, frecuencias, "
        "relación con las comidas y duración del tratamiento. "
        "**Prototipo académico — no constituye consejo médico.**"
    )

    if es_admin(paciente):
        seccion_admin()
        st.markdown("---")

    seccion_horarios_comida(paciente)
    seccion_registrar_medicamento(paciente)
    st.markdown("---")
    seccion_panel(paciente)
    st.markdown("---")
    seccion_historial(paciente)
    st.markdown("---")
    seccion_chat(paciente)


if __name__ == "__main__":
    main()
