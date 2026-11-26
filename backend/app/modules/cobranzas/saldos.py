from sqlalchemy import func, select

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
    if monto > pedido.importe_total - reservado:
        raise ErrorReglaNegocio("MONTO_EXCEDE_SALDO", "El importe supera el saldo disponible. Revise los cobros ya registrados.")
