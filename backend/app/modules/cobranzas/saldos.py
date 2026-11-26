from sqlalchemy import func, select, text

from backend.app.core.errores import ErrorReglaNegocio
from backend.app.models import Cobro, ParadaRuta


async def total_pedido(db, pedido_id, estados, excluir=None):
    consulta = (
        select(func.coalesce(func.sum(Cobro.monto), 0))
        .join(ParadaRuta, ParadaRuta.id_parada == Cobro.id_parada)
        .where(ParadaRuta.id_pedido == pedido_id, Cobro.estado.in_(estados))
    )
    if excluir is not None:
        consulta = consulta.where(Cobro.id_cobro != excluir)
    return await db.scalar(consulta)


async def validar_importe(db, pedido, monto, excluir=None):
    reservado = await total_pedido(db, pedido.id_pedido, ("PENDIENTE", "CONFORME"), excluir)
    if await pago_digital_vigente(db, pedido.id_pedido):
        raise ErrorReglaNegocio("PAGO_DIGITAL_VIGENTE", "Hay un pago digital en proceso. Consulte el resultado antes de registrar otro cobro.")
    if monto > pedido.importe_total - reservado:
        raise ErrorReglaNegocio("MONTO_EXCEDE_SALDO", "El importe supera el saldo disponible. Revise los cobros ya registrados.")


async def pago_digital_vigente(db, pedido_id):
    return (await db.execute(text("""
        SELECT * FROM siprd.pagos_digitales
        WHERE pedido_id = :pedido AND estado IN ('CREANDO','PENDIENTE','OBSERVADO')
        ORDER BY CASE WHEN estado = 'OBSERVADO' THEN 0 ELSE 1 END, creado DESC LIMIT 1
    """), {"pedido": pedido_id})).mappings().one_or_none()
