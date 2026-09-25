# MediCampus 2.0 — resumen de cambios

## Cómo desplegar
1. Reemplaza tu `app.py` en el repo de GitHub por este archivo.
2. Actualiza `requirements.txt` con el de este entrega (agrega `apscheduler` y `fpdf2`).
3. En Streamlit Cloud, en **Secrets**, deja igual `ANTHROPIC_API_KEY`, `EMAIL_USER`, `EMAIL_PASSWORD`.
4. Vuelve a desplegar (reboot / push).

## Qué cambió respecto al prototipo anterior
- **Login por correo y contraseña**: cada persona se registra una sola vez y luego
  solo inicia sesión. Sus medicamentos quedan asociados a su cuenta (1 o varios,
  sin volver a registrarse).
- **Persistencia real (SQLite, `medicampus.db`)**: los datos ya no se pierden al
  recargar la página. *Nota honesta*: en Streamlit Community Cloud el disco es
  efímero frente a un **redeploy** (git push); sobrevive a recargas y reinicios
  normales del contenedor mientras esté corriendo. Para persistencia garantizada
  a largo plazo habría que migrar a una base externa (Postgres/Supabase), pero
  la capa de datos ya está separada en funciones, así que ese cambio futuro es
  acotado.
- **Envío automático de recordatorios**: un job en segundo plano (APScheduler)
  revisa cada minuto todos los medicamentos de todos los pacientes y envía el
  correo automáticamente a la hora exacta, sin que nadie tenga que apretar un
  botón. El envío manual ("📧") se mantiene como respaldo.
- **Editar y eliminar** medicamentos y cuentas de paciente.
- **Duración del tratamiento**: "cada 8 horas por 5 días" ahora sí se respeta;
  al vencer, el medicamento pasa automáticamente a "Finalizado".
- **Varias horas fijas al día** para un mismo medicamento (ej. mañana y noche).
- **Confirmación de toma** ("✅ Ya la tomé") que además descuenta stock si lo
  configuraste, con alerta de recompra cuando queda poco.
- **Validación de formato de correo** al registrarse.
- **Exportación de historial en CSV y PDF** (antes solo CSV).

## Limitaciones que siguen siendo honestas de mencionar
- La base de interacciones sigue siendo de demostración (6 combinaciones),
  no es una base clínica real.
- El scheduler de correos automáticos solo corre mientras el proceso de la
  app esté vivo en el servidor; para un sistema 24/7 de nivel productivo lo
  ideal sería un cron externo o una función serverless que llame a
  `revisar_y_enviar_recordatorios()`.
- No hay verificación de correo por link (cualquiera puede registrar un correo
  que no le pertenece); para un despliegue real convendría agregar confirmación
  de correo antes de activar recordatorios.
