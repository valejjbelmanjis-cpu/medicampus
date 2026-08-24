"""
MediCampus - Asistente de medicamentos para estudiantes universitarios
Proyecto transversal IA

Funcionalidades:
1. Registro de estudiante y medicamentos
2. Panel de próximas dosis / recordatorios
3. Verificador básico de interacciones (base local de demostración)
4. Asistente con IA:
   - Extrae datos estructurados de una receta escrita en texto libre
   - Responde preguntas del estudiante sobre sus medicamentos
5. Registro de uso exportable a CSV (evidencia funcional)

IMPORTANTE: Este es un prototipo académico. La base de interacciones es
de demostración y NO reemplaza la validación de un profesional de salud.
"""

import os
import json
import csv
from datetime import datetime, timedelta

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

DATA_FILE = "registro_medicamentos.csv"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-4-6"

# Base local de interacciones de DEMOSTRACIÓN (ampliar con fuentes reales:
# vademécum, OMS, INVIMA, etc. antes de cualquier uso real)
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


# ------------------------------------------------------------------
# ESTADO DE SESIÓN
# ------------------------------------------------------------------

def init_state():
    if "medicamentos" not in st.session_state:
        st.session_state.medicamentos = []
    if "estudiante" not in st.session_state:
        st.session_state.estudiante = {"nombre": "", "programa": "", "edad": None}
    if "chat_historial" not in st.session_state:
        st.session_state.chat_historial = []


# ------------------------------------------------------------------
# LLAMADAS A LA IA (Anthropic API)
# ------------------------------------------------------------------

def _llamar_claude(prompt, system=""):
    """Llama a la API de Claude. Si no hay API key configurada,
    devuelve None para que el resto del código use un modo de respaldo."""
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
    """Usa el modelo para convertir una receta en texto libre a JSON
    estructurado: nombre, dosis, frecuencia. Con respaldo por reglas
    simples si no hay API key disponible."""
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

    # --- Modo de respaldo (sin API key): heurística simple ---
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
        "SIEMPRE aclaras que no reemplazas a un profesional de salud y que "
        "ante dudas serias se debe consultar a un médico o farmacéutico. "
        "No das diagnósticos ni indicaciones de dosis distintas a las recetadas."
    )
    prompt = f"Medicamentos actuales del estudiante: {contexto}.\nPregunta: {pregunta}"
    respuesta = _llamar_claude(prompt, system=system)
    if respuesta:
        return respuesta
    return (
        "⚠️ Modo de respaldo (sin conexión a la IA): no puedo generar una "
        "respuesta personalizada ahora mismo. Por favor consulta a un "
        "profesional de salud o configura la variable ANTHROPIC_API_KEY."
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


def calcular_proxima_dosis(hora_inicio, frecuencia_horas):
    ahora = datetime.now()
    proxima = hora_inicio
    while proxima < ahora:
        proxima += timedelta(hours=frecuencia_horas)
    return proxima


def guardar_registro(estudiante, medicamentos):
    filas = []
    for m in medicamentos:
        filas.append({
            "fecha_registro": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "estudiante": estudiante["nombre"],
            "programa": estudiante["programa"],
            "medicamento": m["nombre"],
            "dosis": m["dosis"],
            "frecuencia_horas": m["frecuencia_horas"],
        })
    df_nuevo = pd.DataFrame(filas)
    if os.path.exists(DATA_FILE):
        df_nuevo.to_csv(DATA_FILE, mode="a", header=False, index=False)
    else:
        df_nuevo.to_csv(DATA_FILE, index=False)


# ------------------------------------------------------------------
# INTERFAZ
# ------------------------------------------------------------------

def sidebar_estudiante():
    st.sidebar.header("👤 Datos del estudiante")
    st.session_state.estudiante["nombre"] = st.sidebar.text_input(
        "Nombre", st.session_state.estudiante["nombre"])
    st.session_state.estudiante["programa"] = st.sidebar.text_input(
        "Programa académico", st.session_state.estudiante["programa"])
    st.session_state.estudiante["edad"] = st.sidebar.number_input(
        "Edad", min_value=15, max_value=99, value=st.session_state.estudiante["edad"] or 20)

    st.sidebar.markdown("---")
    if not ANTHROPIC_API_KEY:
        st.sidebar.warning(
            "No hay ANTHROPIC_API_KEY configurada. La app funcionará en "
            "modo de respaldo (sin IA generativa) para que puedas probarla igual."
        )
    else:
        st.sidebar.success("Asistente IA conectado ✅")


def seccion_registrar_medicamento():
    st.subheader("💊 Registrar medicamento")
    tab_manual, tab_ia = st.tabs(["Formulario manual", "Pegar receta (IA extrae los datos)"])

    with tab_manual:
        col1, col2, col3 = st.columns(3)
        with col1:
            nombre = st.text_input("Nombre del medicamento", key="man_nombre")
        with col2:
            dosis = st.text_input("Dosis (ej. 500 mg)", key="man_dosis")
        with col3:
            frecuencia = st.selectbox("Frecuencia", list(FRECUENCIA_HORAS.keys()), key="man_frecuencia")

        if st.button("Agregar medicamento", key="btn_manual"):
            if nombre:
                st.session_state.medicamentos.append({
                    "nombre": nombre,
                    "dosis": dosis or "No especificada",
                    "frecuencia_horas": FRECUENCIA_HORAS[frecuencia],
                    "hora_inicio": datetime.now(),
                })
                st.success(f"'{nombre}' agregado.")
            else:
                st.error("Escribe al menos el nombre del medicamento.")

    with tab_ia:
        texto = st.text_area(
            "Pega aquí el texto de la receta o indicación médica",
            placeholder="Ej: Ibuprofeno 400mg cada 8 horas por 5 días",
        )
        if st.button("Extraer con IA", key="btn_ia"):
            if texto.strip():
                with st.spinner("Analizando receta..."):
                    datos, modo = extraer_receta_con_ia(texto)
                st.session_state.medicamentos.append({
                    "nombre": datos.get("nombre", "Medicamento"),
                    "dosis": datos.get("dosis", "No especificada"),
                    "frecuencia_horas": int(datos.get("frecuencia_horas", 8)),
                    "hora_inicio": datetime.now(),
                })
                etiqueta = "🤖 IA" if modo == "ia" else "🛟 modo de respaldo"
                st.success(f"Extraído ({etiqueta}): {datos}")
            else:
                st.error("Pega el texto de la receta primero.")


def seccion_panel():
    st.subheader("📋 Mis medicamentos y próximas dosis")
    if not st.session_state.medicamentos:
        st.info("Aún no has registrado medicamentos.")
        return

    filas = []
    for m in st.session_state.medicamentos:
        proxima = calcular_proxima_dosis(m["hora_inicio"], m["frecuencia_horas"])
        filas.append({
            "Medicamento": m["nombre"],
            "Dosis": m["dosis"],
            "Frecuencia": f'cada {m["frecuencia_horas"]}h',
            "Próxima dosis": proxima.strftime("%d/%m/%Y %H:%M"),
        })
    st.dataframe(pd.DataFrame(filas), use_container_width=True)

    alertas = verificar_interacciones(st.session_state.medicamentos)
    if alertas:
        st.error("⚠️ Posibles interacciones detectadas (base de demostración):")
        for a in alertas:
            st.write(f"- **{a['a'].capitalize()} + {a['b'].capitalize()}** — Riesgo {a['riesgo']}: {a['detalle']}")
        st.caption("Esta verificación es una demostración académica. Confirma siempre con un profesional de salud.")
    else:
        st.success("No se detectaron interacciones en la base de demostración.")

    if st.button("💾 Guardar registro (evidencia funcional)"):
        guardar_registro(st.session_state.estudiante, st.session_state.medicamentos)
        st.success(f"Registro guardado en {DATA_FILE}")


def seccion_chat():
    st.subheader("🤖 Asistente MediCampus")
    st.caption("Responde dudas generales. No reemplaza a un profesional de salud.")

    for rol, texto in st.session_state.chat_historial:
        with st.chat_message(rol):
            st.write(texto)

    pregunta = st.chat_input("Escribe tu pregunta...")
    if pregunta:
        st.session_state.chat_historial.append(("user", pregunta))
        with st.spinner("Pensando..."):
            respuesta = responder_pregunta_ia(pregunta, st.session_state.medicamentos)
        st.session_state.chat_historial.append(("assistant", respuesta))
        st.rerun()


# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------

def main():
    init_state()
    sidebar_estudiante()

    st.title("💊 MediCampus")
    st.markdown(
        "Asistente universitario de adherencia a medicamentos. "
        "**Prototipo académico — no constituye consejo médico.**"
    )

    seccion_registrar_medicamento()
    st.markdown("---")
    seccion_panel()
    st.markdown("---")
    seccion_chat()


if __name__ == "__main__":
    main()
