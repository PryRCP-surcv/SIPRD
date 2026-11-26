# Portal de SIPRD y pagos digitales

Aplica en orden las migraciones SQL de la rama hasta `006_pagos_digitales.sql` antes de iniciar el servicio actualizado. Comprueba la conexión y el esquema con `/listo`.

Para publicar desde un mismo servicio, compila el frontend con `npm ci` y `npm run build`. Configura `FRONTEND_DIST=dist` en FastAPI y sirve la aplicación detrás de un proxy HTTPS. Si Reparto usa una dirección distinta de la pública, define `VITE_PORTAL_PUBLICO_URL` con la dirección pública del frontend antes de compilar. Conserva el secreto JWT entre reinicios para mantener la vigencia de los enlaces compartidos.

Para probar Mercado Pago, configura en el servidor `MP_MODO=pruebas`, `MP_ACCESS_TOKEN`, `MP_USUARIO_ID` y `MP_WEBHOOK_SECRET`. En la aplicación del proveedor habilita los eventos **Orders** y registra la URL HTTPS del endpoint `/api/v1/portal/notificaciones/mercadopago`. Usa pedidos sintéticos y una cuenta compradora de pruebas. Prueba el resultado desde el portal y desde **Tesorería → Operaciones de Mercado Pago**. Cambia a `MP_MODO=real` después de verificar la cuenta receptora, los webhooks y el recorrido con el proveedor. Esta modalidad queda disponible para pedidos operativos.

Al lanzar un checkout, el pedido conserva una referencia y una reserva de saldo hasta conocer el resultado. Si el proveedor no responde al crearlo, reabre la opción de preparar el pago desde el enlace para reintentar con la misma referencia. Tesorería puede volver a consultar una orden y revisar discrepancias, devoluciones y contracargos. Un reembolso parcial queda observado para conciliación.
