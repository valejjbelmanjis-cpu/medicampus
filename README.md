# MediCampus — Prototipo académico

Asistente de adherencia a medicamentos para estudiantes universitarios.
Proyecto transversal de IA.

## Cómo ejecutarlo

1. Instala dependencias:
   ```
   pip install -r requirements.txt
   ```

2. (Opcional pero recomendado) Configura tu API key de Anthropic para
   activar el asistente de IA real:
   ```
   export ANTHROPIC_API_KEY="tu-api-key-aqui"
   ```
   Si no la configuras, la app funciona igual en **modo de respaldo**
   (reglas simples) para que puedas hacer la demo sin depender de la API.

3. Corre la app:
   ```
   streamlit run app.py
   ```

4. Se abrirá en tu navegador en `http://localhost:8501`.

## Qué evidencia genera para la rúbrica

- **Criterio 1 (problema real):** complementa con tu encuesta/entrevista
  a estudiantes sobre adherencia a medicamentos.
- **Criterio 2 (IA integrada):** la pestaña "Pegar receta" usa la API de
  Claude para extraer nombre/dosis/frecuencia de texto libre (extracción
  de información con IA); el chat usa el mismo modelo para responder
  preguntas del estudiante.
- **Criterio 3 (funcionalidad):** la app corre de principio a fin, genera
  un CSV (`registro_medicamentos.csv`) como evidencia de uso real, y
  detecta interacciones básicas entre medicamentos registrados.

## Nota importante

La base de interacciones (`INTERACCIONES_DEMO` en `app.py`) es de
demostración académica. No debe usarse como fuente médica real. El
prototipo incluye disclaimers para dejar esto claro en la interfaz.
