from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text

from backend.app.core.auditoria import registrar_auditoria
from backend.app.core.db import get_db
from backend.app.core.errores import ErrorReglaNegocio
from backend.app.core.seguridad import require_roles
from backend.app.core.tiempo import ahora
from backend.app.models import Cobro, ParadaRuta, Planificacion, Ruta, Usuario

router = APIRouter(prefix="/liquidaciones", tags=["Liquidaciones"])
lectura = require_roles(["TESORERIA"])


class Conteo(BaseModel):
    recibido: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    observacion: str = Field(default="", max_length=1000)


def efectivo(fecha):
    return (
        select(Cobro)
        .join(ParadaRuta, Cobro.id_parada == ParadaRuta.id_parada)
        .join(Ruta, ParadaRuta.id_ruta == Ruta.id_ruta)
        .join(Planificacion, Ruta.id_planificacion == Planificacion.id_planificacion)
        .where(Cobro.medio_pago == "EFECTIVO", Planificacion.fecha_operacion == fecha)
    )


async def bloquear_caja(db, repartidor_id):
    await db.execute(
        select(Usuario.id_usuario).where(Usuario.id_usuario == repartidor_id).with_for_update(key_share=True)
    )


async def liquidacion_actual(db, fecha, repartidor_id):
    return (await db.execute(text("""
        SELECT * FROM siprd.liquidaciones_efectivo
        WHERE fecha = :fecha AND repartidor_id = :repartidor_id
    """), dict(fecha=fecha, repartidor_id=repartidor_id))).mappings().one_or_none()


async def validar_caja_abierta(db, parada, repartidor_id):
    await bloquear_caja(db, repartidor_id)
    fecha = await db.scalar(
        select(Planificacion.fecha_operacion).join(Ruta).where(Ruta.id_ruta == parada.id_ruta)
    )
    if await liquidacion_actual(db, fecha, repartidor_id):
        raise ErrorReglaNegocio("CAJA_LIQUIDADA", "El efectivo de este responsable y jornada ya se liquidó.")


@router.get("")
async def listar(fecha: date, db=Depends(get_db), usuario=Depends(lectura)):
    base = efectivo(fecha).with_only_columns(
        Cobro.id_usuario_registro.label("repartidor_id"),
        func.sum(Cobro.monto).filter(Cobro.estado != "OBSERVADO").label("esperado"),
    ).group_by(Cobro.id_usuario_registro).subquery()
    filas = (await db.execute(
        select(base, Usuario.nombres, Usuario.apellidos)
        .join(Usuario, Usuario.id_usuario == base.c.repartidor_id)
        .order_by(Usuario.nombres, Usuario.apellidos)
    )).all()
    guardadas = (await db.execute(text(
        "SELECT * FROM siprd.liquidaciones_efectivo WHERE fecha = :fecha"
    ), {"fecha": fecha})).mappings()
    por_usuario = {r["repartidor_id"]: dict(r) for r in guardadas}
    return [{
        "repartidor_id": id_, "repartidor": f"{nombres} {apellidos}",
        "esperado": esperado or Decimal(0), "liquidacion": por_usuario.get(id_),
    } for id_, esperado, nombres, apellidos in filas]


@router.put("/{fecha}/{repartidor_id}")
async def guardar(fecha: date, repartidor_id: int, req: Conteo,
                  db=Depends(get_db), usuario=Depends(lectura)):
    await bloquear_caja(db, repartidor_id)
    previa = await liquidacion_actual(db, fecha, repartidor_id)
    if previa and previa["esperado"] == previa["recibido"]:
        raise ErrorReglaNegocio("CAJA_LIQUIDADA", "La liquidación conforme ya se registró.")
    cobros = (await db.scalars(efectivo(fecha).where(
        Cobro.id_usuario_registro == repartidor_id, Cobro.estado != "OBSERVADO"
    ))).all()
    if not cobros:
        raise ErrorReglaNegocio("SIN_EFECTIVO", "No hay cobros en efectivo para esta jornada y responsable.")
    esperado = sum((c.monto for c in cobros), Decimal(0))
    diferencia = req.recibido - esperado
    observacion = req.observacion.strip() or None
    if diferencia and not observacion:
        raise ErrorReglaNegocio("OBSERVACION_REQUERIDA", "Indique el motivo de la diferencia.")
    await db.execute(text("""
        INSERT INTO siprd.liquidaciones_efectivo
          (fecha, repartidor_id, responsable_id, esperado, recibido, observacion)
        VALUES (:fecha, :repartidor, :responsable, :esperado, :recibido, :observacion)
        ON CONFLICT (fecha, repartidor_id) DO UPDATE SET
          responsable_id = excluded.responsable_id, esperado = excluded.esperado,
          recibido = excluded.recibido, observacion = excluded.observacion, registrado = now()
    """), dict(fecha=fecha, repartidor=repartidor_id, responsable=int(usuario["sub"]),
               esperado=esperado, recibido=req.recibido, observacion=observacion))
    estado = "OBSERVADA" if diferencia else "CONFORME"
    if estado == "CONFORME":
        for cobro in cobros:
            if cobro.estado == "PENDIENTE":
                cobro.estado = "CONFORME"
                cobro.id_usuario_validador = int(usuario["sub"])
                cobro.fecha_validacion = ahora()
    await registrar_auditoria(
        db, "ACTUALIZAR" if previa else "CREAR", "liquidacion_efectivo", repartidor_id,
        valores_antes={"recibido": str(previa["recibido"]), "observacion": previa["observacion"]} if previa else None,
        valores_despues={"fecha": str(fecha), "estado": estado, "esperado": str(esperado),
                         "recibido": str(req.recibido), "observacion": observacion},
        usuario_id=usuario["sub"],
    )
    return {"estado": estado, "diferencia": diferencia}
