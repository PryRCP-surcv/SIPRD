BEGIN;

CREATE TABLE IF NOT EXISTS siprd.pagos_digitales (
    referencia uuid PRIMARY KEY,
    pedido_id bigint NOT NULL REFERENCES siprd.pedidos(id_pedido),
    parada_id bigint NOT NULL REFERENCES siprd.paradas_ruta(id_parada),
    emisor_id bigint NOT NULL REFERENCES siprd.usuarios(id_usuario),
    modo varchar(12) NOT NULL CHECK (modo IN ('pruebas', 'real')),
    importe numeric(12,2) NOT NULL CHECK (importe > 0),
    orden_proveedor varchar(80) UNIQUE,
    enlace_checkout text,
    cobro_id bigint UNIQUE REFERENCES siprd.cobros(id_cobro),
    estado varchar(20) NOT NULL CHECK (
        estado IN ('CREANDO','PENDIENTE','APROBADO','FINALIZADO','OBSERVADO','REVERTIDO')
    ),
    estado_proveedor varchar(40),
    detalle varchar(300),
    consultado timestamptz,
    actualizado timestamptz NOT NULL DEFAULT now(),
    creado timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_pago_digital_vigente
    ON siprd.pagos_digitales(pedido_id)
    WHERE estado IN ('CREANDO','PENDIENTE');

ALTER TABLE siprd.cobros DROP CONSTRAINT IF EXISTS ck_cobro_validacion;
ALTER TABLE siprd.cobros ADD CONSTRAINT ck_cobro_validacion CHECK (
    estado = 'PENDIENTE'
    OR (fecha_validacion IS NOT NULL AND (
        id_usuario_validador IS NOT NULL
        OR (estado = 'CONFORME' AND coalesce(numero_operacion, '') LIKE 'SIPRD-DEMO-%')
        OR coalesce(numero_operacion, '') LIKE 'SIPRD-MP-%'
    ))
);

COMMIT;
