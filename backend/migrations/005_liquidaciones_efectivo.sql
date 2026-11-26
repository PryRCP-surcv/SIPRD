BEGIN;

CREATE TABLE IF NOT EXISTS siprd.liquidaciones_efectivo (
    fecha date NOT NULL,
    repartidor_id bigint REFERENCES siprd.usuarios(id_usuario),
    responsable_id bigint NOT NULL REFERENCES siprd.usuarios(id_usuario),
    esperado numeric(14,2) NOT NULL CHECK (esperado >= 0),
    recibido numeric(14,2) NOT NULL CHECK (recibido >= 0),
    observacion varchar(1000),
    registrado timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (fecha, repartidor_id),
    CHECK (esperado = recibido OR length(trim(coalesce(observacion, ''))) > 0)
);

-- La simulación de pedidos de demostración queda identificada por su referencia.
ALTER TABLE siprd.cobros DROP CONSTRAINT IF EXISTS ck_cobro_validacion;
ALTER TABLE siprd.cobros ADD CONSTRAINT ck_cobro_validacion CHECK (
    estado = 'PENDIENTE'
    OR (fecha_validacion IS NOT NULL AND (
        id_usuario_validador IS NOT NULL
        OR (estado = 'CONFORME' AND coalesce(numero_operacion, '') LIKE 'SIPRD-DEMO-%')
    ))
);

COMMIT;
