# ==================================================================
# PACIENTES FIJOS DE DEMOSTRACIÓN (32 cuentas de la encuesta)
# PEGAR ESTE BLOQUE justo debajo de la función obtener_eventos_df()
# (al final de la sección "eventos / historial").
# ==================================================================

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


def _correo_demo(nombre, usados):
    """primernombre.ultimoapellido@example.com, sin tildes y en minúsculas."""
    import unicodedata
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFD", nombre)
        if unicodedata.category(c) != "Mn"
    )
    partes = sin_tildes.lower().split()
    base = partes[0] if len(partes) == 1 else f"{partes[0]}.{partes[-1]}"
    correo, n = f"{base}@{DOMINIO_DEMO}", 2
    while correo in usados:
        correo, n = f"{base}{n}@{DOMINIO_DEMO}", n + 1
    usados.add(correo)
    return correo


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
            correo = _correo_demo(nombre, set())          # correo "canónico"
            if correo in usados:
                continue                                    # ya existe: no duplicar
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
