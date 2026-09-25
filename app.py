"""
MediCampus - Asistente de medicamentos para estudiantes universitarios
Proyecto transversal IA

Funcionalidades:
1. Registro de VARIOS estudiantes/pacientes, cada uno con su propio correo
2. Registro manual o asistido por IA de medicamentos por paciente
3. Horarios de toma flexibles por medicamento:
   - Cada cierto número de horas (frecuencia fija)
   - A una hora fija del día (ej. "en la noche")
   - Antes/después de una comida (desayuno, almuerzo, cena), con
     minutos de offset, usando los horarios de comida propios de
     cada paciente
4. Panel de próximas dosis por paciente
5. Verificador básico de interacciones (base local de demostración)
6. Envío de recordatorio real por correo a CADA paciente individualmente
7. Asistente de chat con IA
8. Registro de uso exportable a CSV (evidencia funcional)

IMPORTANTE: Este es un prototipo académico. La base de interacciones es
de demostración y NO reemplaza la validación de un profesional de salud.
"""

import os
import json
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta, time as dtime

import streamlit as st
import pandas as pd
import requests

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


DATA_FILE = "registro_medicamentos.csv"
ANTHROPIC_API_KEY = get_secret("ANTHROPIC_API_KEY")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-4-6"

EMAIL_USER = get_secret("EMAIL_USER")
EMAIL_PASSWORD = get_secret("EMAIL_PASSWORD")

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
    "A una hora fija cada día",
    "Relacionado con una comida",
]

COMIDAS = ["Desayuno", "Almuerzo", "Cena"]
MOMENTO_COMIDA = ["Antes", "Después"]

HORARIOS_COMIDA_DEFECTO = {
    "Desayuno": dtime(7, 0),
    "Almuerzo": dtime(12, 30),
    "Cena": dtime(19, 0),
}


# ------------------------------------------------------------------
# ESTADO DE SESIÓN — lista de pacientes
# ------------------------------------------------------------------

def init_state():
    if "pacientes" not in st.session_state:
        st.session_state.pacientes = []  # cada uno: {nombre, correo, programa, medicamentos: [], horarios_comida: {}}
    if "paciente_activo" not in st.session_state:
        st.session_state.paciente_activo = None
    if "chat_historial" not in st.session_state:
        st.session_state.chat_historial = []


def obtener_paciente_activo():
    for p in st.session_state.pacientes:
        if p["nombre"] == st.session_state.paciente_activo:
            return p
    return None


# ------------------------------------------------------------------
# LÓGICA DE HORARIOS
# ------------------------------------------------------------------

def calcular_proxima_dosis(medicamento, paciente):
    """Calcula el próximo datetime de toma según el tipo de horario
    configurado para el medicamento."""
    ahora = datetime.now()
    tipo = medicamento.get("tipo_horario", "frecuencia")

    if tipo == "frecuencia":
        proxima = medicamento["hora_inicio"]
        frecuencia = medicamento["frecuencia_horas"]
        while proxima < ahora:
            proxima += timedelta(hours=frecuencia)
        return proxima

    if tipo == "fijo":
        hora_fija = medicamento["hora_fija"]
        proxima = datetime.combine(ahora.date(), hora_fija)
        if proxima < ahora:
            proxima += timedelta(days=1)
        return proxima

    if tipo == "comida":
        comida = medicamento["comida"]
        momento = medicamento["momento"]
        offset = medicamento["offset_min"]
        horarios_comida = paciente.get("horarios_comida", HORARIOS_COMIDA_DEFECTO)
        hora_comida = horarios_comida.get(comida, HORARIOS_COMIDA_DEFECTO[comida])
        base = datetime.combine(ahora.date(), hora_comida)
        if momento == "Antes":
            objetivo = base - timedelta(minutes=offset)
        else:
            objetivo = base + timedelta(minutes=offset)
        if objetivo < ahora:
            objetivo += timedelta(days=1)
        return objetivo

    # Respaldo por si algún medicamento antiguo no tiene tipo_horario
    return calcular_proxima_dosis(
        {**medicamento, "tipo_horario": "frecuencia",
         "frecuencia_horas": medicamento.get("frecuencia_horas", 8)},
        paciente,
    )


def descripcion_horario(medicamento):
    """Texto legible del horario, para mostrar en el panel y en el correo."""
    tipo = medicamento.get("tipo_horario", "frecuencia")
    if tipo == "frecuencia":
        return f"cada {medicamento['frecuencia_horas']} horas"
    if tipo == "fijo":
        return f"todos los días a las {medicamento['hora_fija'].strftime('%H:%M')}"
    if tipo == "comida":
        return (
            f"{medicamento['momento'].lower()} de {medicamento['comida'].lower()} "
            f"({medicamento['offset_min']} min)"
        )
    return "horario no especificado"


# ------------------------------------------------------------------
# ENVÍO DE RECORDATORIOS POR CORREO (a cada paciente, individualmente)
# ------------------------------------------------------------------

def enviar_recordatorio_email(paciente, medicamento, hora_toma):
    """Envía un correo real al correo PROPIO del paciente, con el
    medicamento y la hora exacta a la que debe tomarlo."""
    if not EMAIL_USER or not EMAIL_PASSWORD:
        return False, (
            "No hay EMAIL_USER / EMAIL_PASSWORD configurados en Secrets. "
            "Configúralos para activar el envío real."
        )
    destinatario = paciente.get("correo", "")
    if not destinatario:
        return False, f"{paciente['nombre']} no tiene correo registrado."

    asunto = f"⏰ MediCampus — Recordatorio para {paciente['nombre']}: {medicamento['nombre']}"
    cuerpo = (
        f"Hola {paciente['nombre']},\n\n"
        f"Este es tu recordatorio de MediCampus.\n\n"
        f"Medicamento: {medicamento['nombre']}\n"
        f"Dosis: {medicamento['dosis']}\n"
        f"Debes tomarlo a las: {hora_toma.strftime('%d/%m/%Y %H:%M')}\n"
        f"Horario: {descripcion_horario(medicamento)}\n\n"
        f"Este recordatorio quedó registrado en tu historial de MediCampus.\n\n"
        f"— MediCampus (prototipo académico, no reemplaza indicación médica)"
    )

    try:
        msg = MIMEText(cuerpo)
        msg["Subject"] = asunto
        msg["From"] = EMAIL_USER
        msg["To"] = destinatario

        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_USER, [destinatario], msg.as_string())

        registrar_envio(paciente, medicamento, hora_toma)
        return True, f"Correo enviado a {paciente['nombre']} ({destinatario}) ✅"
    except Exception as e:
        return False, f"Error enviando correo: {e}"


def registrar_envio(paciente, medicamento, hora_toma):
    """Guarda evidencia del envío en el CSV (funcionalidad + evidencia)."""
    fila = pd.DataFrame([{
        "fecha_envio": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "paciente": paciente["nombre"],
        "correo": paciente.get("correo", ""),
        "medicamento": medicamento["nombre"],
        "dosis": medicamento["dosis"],
        "horario": descripcion_horario(medicamento),
        "hora_toma_programada": hora_toma.strftime("%Y-%m-%d %H:%M:%S"),
    }])
    if os.path.exists(DATA_FILE):
        fila.to_csv(DATA_FILE, mode="a", header=False, index=False)
    else:
        fila.to_csv(DATA_FILE, index=False)


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
        "Extraes datos de recetas médicas en español y respondes SOLO "
        "con JSON válido, sin texto adicional, con este formato: "
        '{"nombre": "...", "dosis": "...", "frecuencia_horas": numero}'
    )
    respuesta = _llamar_claude(texto_libre, system=system)

    if respuesta and not respuesta.startswith("[Error"):
        try:
            limpio = respuesta.strip().strip("`").replace("json\n", "")
            return json.loads(limpio), "ia"
        except Exception:
            pass

    texto = texto_libre.lower()
    horas = 8
    for frase, h in [("cada 6", 6), ("cada 8", 8), ("cada 12", 12), ("cada 24", 24), ("diari", 24)]:
        if frase in texto:
            horas = h
            break
    primera_palabra = texto_libre.strip().split(" ")[0] if texto_libre.strip() else "Medicamento"
    return (
        {"nombre": primera_palabra.capitalize(), "dosis": "Ver receta original", "frecuencia_horas": horas},
        "respaldo",
    )


def responder_pregunta_ia(pregunta, medicamentos):
    contexto = ", ".join(m["nombre"] for m in medicamentos) or "ninguno registrado"
    system = (
        "Eres un asistente educativo de MediCampus, un prototipo estudiantil. "
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


# ------------------------------------------------------------------
# INTERFAZ
# ------------------------------------------------------------------

def sidebar_gestion_pacientes():
    st.sidebar.header("👥 Pacientes / estudiantes")

    with st.sidebar.form("form_nuevo_paciente", clear_on_submit=True):
        st.write("**Agregar nuevo paciente**")
        nombre = st.text_input("Nombre")
        correo = st.text_input("Correo (aquí le llegarán SUS recordatorios)")
        programa = st.text_input("Programa académico (opcional)")
        agregar = st.form_submit_button("➕ Agregar paciente")
        if agregar:
            if nombre and correo:
                if any(p["nombre"] == nombre for p in st.session_state.pacientes):
                    st.sidebar.error("Ya existe un paciente con ese nombre.")
                else:
                    st.session_state.pacientes.append({
                        "nombre": nombre, "correo": correo, "programa": programa,
                        "medicamentos": [],
                        "horarios_comida": dict(HORARIOS_COMIDA_DEFECTO),
                    })
                    st.session_state.paciente_activo = nombre
                    st.sidebar.success(f"{nombre} agregado.")
            else:
                st.sidebar.error("Nombre y correo son obligatorios.")

    st.sidebar.markdown("---")

    if st.session_state.pacientes:
        nombres = [p["nombre"] for p in st.session_state.pacientes]
        idx_actual = nombres.index(st.session_state.paciente_activo) if st.session_state.paciente_activo in nombres else 0
        seleccion = st.sidebar.selectbox("Paciente activo", nombres, index=idx_actual)
        st.session_state.paciente_activo = seleccion
    else:
        st.sidebar.info("Agrega al menos un paciente para empezar.")

    st.sidebar.markdown("---")
    if not EMAIL_USER or not EMAIL_PASSWORD:
        st.sidebar.warning("Recordatorios por correo: no configurados (ver Secrets).")
    else:
        st.sidebar.success("Recordatorios por correo activados ✅")

    if not ANTHROPIC_API_KEY:
        st.sidebar.warning("Sin ANTHROPIC_API_KEY: modo de respaldo activo para la IA.")
    else:
        st.sidebar.success("Asistente IA conectado ✅")


def seccion_horarios_comida(paciente):
    """Permite configurar las horas de comida propias de cada paciente,
    usadas para calcular tomas 'antes/después de' una comida."""
    if "horarios_comida" not in paciente:
        paciente["horarios_comida"] = dict(HORARIOS_COMIDA_DEFECTO)

    with st.expander(f"⚙️ Horarios de comida de {paciente['nombre']} (para tomas antes/después de comer)"):
        horarios = paciente["horarios_comida"]
        col1, col2, col3 = st.columns(3)
        with col1:
            horarios["Desayuno"] = st.time_input(
                "Desayuno", value=horarios.get("Desayuno", HORARIOS_COMIDA_DEFECTO["Desayuno"]),
                key=f"h_desayuno_{paciente['nombre']}",
            )
        with col2:
            horarios["Almuerzo"] = st.time_input(
                "Almuerzo", value=horarios.get("Almuerzo", HORARIOS_COMIDA_DEFECTO["Almuerzo"]),
                key=f"h_almuerzo_{paciente['nombre']}",
            )
        with col3:
            horarios["Cena"] = st.time_input(
                "Cena", value=horarios.get("Cena", HORARIOS_COMIDA_DEFECTO["Cena"]),
                key=f"h_cena_{paciente['nombre']}",
            )


def seccion_registrar_medicamento(paciente):
    st.subheader(f"💊 Registrar medicamento para {paciente['nombre']}")
    tab_manual, tab_ia = st.tabs(["Formulario manual", "Pegar receta (IA extrae los datos)"])

    with tab_manual:
        tipo_horario = st.radio(
            "¿Cómo se debe tomar este medicamento?",
            TIPOS_HORARIO,
            key="man_tipo_horario",
            horizontal=True,
        )

        col1, col2 = st.columns(2)
        with col1:
            nombre = st.text_input("Nombre del medicamento", key="man_nombre")
        with col2:
            dosis = st.text_input("Dosis (ej. 500 mg)", key="man_dosis")

        frecuencia_horas = None
        hora_fija = None
        comida = momento = None
        offset_min = 0

        if tipo_horario == "Cada cierto número de horas":
            frecuencia = st.selectbox("Frecuencia", list(FRECUENCIA_HORAS.keys()), key="man_frecuencia")
            frecuencia_horas = FRECUENCIA_HORAS[frecuencia]

        elif tipo_horario == "A una hora fija cada día":
            hora_fija = st.time_input(
                "Hora de la toma (ej. 22:00 para la noche)", value=dtime(21, 0), key="man_hora_fija",
            )

        else:  # Relacionado con una comida
            colc1, colc2, colc3 = st.columns(3)
            with colc1:
                comida = st.selectbox("Comida", COMIDAS, key="man_comida")
            with colc2:
                momento = st.selectbox("Momento", MOMENTO_COMIDA, key="man_momento")
            with colc3:
                offset_min = st.number_input(
                    "Minutos antes/después", min_value=0, max_value=180, value=30, step=5, key="man_offset",
                )
            st.caption(
                "Se calculará usando el horario de comida configurado para "
                f"{paciente['nombre']} (ver '⚙️ Horarios de comida' arriba)."
            )

        if st.button("Agregar medicamento", key="btn_manual"):
            if nombre:
                nuevo = {
                    "nombre": nombre,
                    "dosis": dosis or "No especificada",
                    "hora_inicio": datetime.now(),
                }
                if tipo_horario == "Cada cierto número de horas":
                    nuevo["tipo_horario"] = "frecuencia"
                    nuevo["frecuencia_horas"] = frecuencia_horas
                elif tipo_horario == "A una hora fija cada día":
                    nuevo["tipo_horario"] = "fijo"
                    nuevo["hora_fija"] = hora_fija
                else:
                    nuevo["tipo_horario"] = "comida"
                    nuevo["comida"] = comida
                    nuevo["momento"] = momento
                    nuevo["offset_min"] = offset_min

                paciente["medicamentos"].append(nuevo)
                st.success(f"'{nombre}' agregado a {paciente['nombre']} ({descripcion_horario(nuevo)}).")
            else:
                st.error("Escribe al menos el nombre del medicamento.")

    with tab_ia:
        texto = st.text_area(
            "Pega aquí el texto de la receta o indicación médica",
            placeholder="Ej: Ibuprofeno 400mg cada 8 horas por 5 días",
        )
        st.caption(
            "La IA extrae nombre, dosis y frecuencia en horas. Si el medicamento "
            "debe tomarse a una hora fija o antes/después de una comida, agrégalo "
            "o ajústalo con el formulario manual."
        )
        if st.button("Extraer con IA", key="btn_ia"):
            if texto.strip():
                with st.spinner("Analizando receta..."):
                    datos, modo = extraer_receta_con_ia(texto)
                nuevo = {
                    "nombre": datos.get("nombre", "Medicamento"),
                    "dosis": datos.get("dosis", "No especificada"),
                    "tipo_horario": "frecuencia",
                    "frecuencia_horas": int(datos.get("frecuencia_horas", 8)),
                    "hora_inicio": datetime.now(),
                }
                paciente["medicamentos"].append(nuevo)
                etiqueta = "🤖 IA" if modo == "ia" else "🛟 modo de respaldo"
                st.success(f"Extraído ({etiqueta}): {datos}")
            else:
                st.error("Pega el texto de la receta primero.")


def seccion_panel(paciente):
    st.subheader(f"📋 Medicamentos de {paciente['nombre']} ({paciente.get('correo', 'sin correo')})")
    medicamentos = paciente["medicamentos"]

    if not medicamentos:
        st.info("Este paciente aún no tiene medicamentos registrados.")
        return

    filas = []
    for m in medicamentos:
        proxima = calcular_proxima_dosis(m, paciente)
        filas.append({
            "Medicamento": m["nombre"],
            "Dosis": m["dosis"],
            "Horario": descripcion_horario(m),
            "Próxima dosis": proxima.strftime("%d/%m/%Y %H:%M"),
        })
    st.dataframe(pd.DataFrame(filas), use_container_width=True)

    st.markdown(
        "**📧 Enviar recordatorio ahora** — le llega directo al correo "
        f"de **{paciente['nombre']}**, con el medicamento y la hora exacta "
        "de la próxima toma (en producción esto lo dispara un programador "
        "de tareas automáticamente a esa hora):"
    )
    for i, m in enumerate(medicamentos):
        proxima = calcular_proxima_dosis(m, paciente)
        col_a, col_b = st.columns([3, 1])
        with col_a:
            st.write(
                f"{m['nombre']} — {m['dosis']} — {descripcion_horario(m)} — "
                f"próxima toma: {proxima.strftime('%d/%m/%Y %H:%M')}"
            )
        with col_b:
            if st.button("Enviar", key=f"enviar_{paciente['nombre']}_{i}"):
                ok, mensaje = enviar_recordatorio_email(paciente, m, proxima)
                if ok:
                    st.success(mensaje)
                else:
                    st.error(mensaje)

    alertas = verificar_interacciones(medicamentos)
    if alertas:
        st.error("⚠️ Posibles interacciones detectadas (base de demostración):")
        for a in alertas:
            st.write(f"- **{a['a'].capitalize()} + {a['b'].capitalize()}** — Riesgo {a['riesgo']}: {a['detalle']}")
        st.caption("Verificación de demostración académica. Confirma siempre con un profesional de salud.")
    else:
        st.success("No se detectaron interacciones en la base de demostración.")


def seccion_registro_general():
    st.subheader("🗂️ Registro histórico de recordatorios enviados")
    if os.path.exists(DATA_FILE):
        df = pd.read_csv(DATA_FILE)
        st.dataframe(df, use_container_width=True)
        st.caption(f"Evidencia funcional guardada en `{DATA_FILE}`.")
    else:
        st.info("Aún no se ha enviado ningún recordatorio.")


def seccion_chat(paciente):
    st.subheader(f"🤖 Asistente MediCampus — dudas de {paciente['nombre']}")
    st.caption("Responde dudas generales. No reemplaza a un profesional de salud.")

    for rol, texto in st.session_state.chat_historial:
        with st.chat_message(rol):
            st.write(texto)

    pregunta = st.chat_input("Escribe tu pregunta...")
    if pregunta:
        st.session_state.chat_historial.append(("user", pregunta))
        with st.spinner("Pensando..."):
            respuesta = responder_pregunta_ia(pregunta, paciente["medicamentos"])
        st.session_state.chat_historial.append(("assistant", respuesta))
        st.rerun()


# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------

def main():
    init_state()
    sidebar_gestion_pacientes()

    st.title("💊 MediCampus")
    st.markdown(
        "Asistente universitario de adherencia a medicamentos, con "
        "recordatorios individuales por correo para cada paciente, "
        "respetando horarios fijos, frecuencias y relación con las comidas. "
        "**Prototipo académico — no constituye consejo médico.**"
    )

    paciente = obtener_paciente_activo()
    if paciente is None:
        st.warning("Agrega un paciente en la barra lateral izquierda para comenzar.")
        return

    seccion_horarios_comida(paciente)
    seccion_registrar_medicamento(paciente)
    st.markdown("---")
    seccion_panel(paciente)
    st.markdown("---")
    seccion_registro_general()
    st.markdown("---")
    seccion_chat(paciente)


if __name__ == "__main__":
    main()
