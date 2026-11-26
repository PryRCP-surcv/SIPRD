from datetime import timedelta
from decimal import Decimal
import secrets

import jwt
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from backend.app.core.auditoria import registrar_auditoria
from backend.app.core.config import settings
from backend.app.core.db import get_db
from backend.app.core.reparto import autorizar_parada
from backend.app.core.seguridad import decodificar_token, require_roles
from backend.app.core.tiempo import ahora
from backend.app.models import Cobro, ParadaRuta, Pedido, Planificacion, Ruta
from backend.app.modules.cobranzas.saldos import total_pedido

router = APIRouter(prefix="/portal", tags=["Portal del cliente"])


class Acceso(BaseModel):
    enlace: str = Field(min_length=1, max_length=2048)


async def contexto(db, parada_id):
    fila = (await db.execute(
        select(ParadaRuta, Pedido, Planificacion)
        .join(Pedido, Pedido.id_pedido == ParadaRuta.id_pedido)
        .join(Ruta, Ruta.id_ruta == ParadaRuta.id_ruta)
        .join(Planificacion, Planificacion.id_planificacion == Ruta.id_planificacion)
        .where(ParadaRuta.id_parada == parada_id, ParadaRuta.estado != "CANCELADA",
               Ruta.estado != "CANCELADA",
               Planificacion.estado.in_(("CONFIRMADA", "REPLANIFICADA")))
    )).one_or_none()
    if fila is None:
        raise HTTPException(404, "El enlace ya no corresponde a una entrega disponible.")
    return fila


def demo_habilitada(pedido):
    return settings.PAGOS_DEMO and settings.ENTORNO == "desarrollo" and pedido.origen == "DEMO_ZONAS"


def credencial(req, response):
    response.headers["Cache-Control"] = "no-store"
    datos = decodificar_token(req.enlace)
    if datos.get("tipo") != "portal_cliente":
        raise HTTPException(401, "Enlace inválido.")
    return datos


@router.post("/enlaces/{parada_id}")
async def emitir(parada_id: int, response: Response, db=Depends(get_db),
                 usuario=Depends(require_roles(["REPARTIDOR"]))):
    await autorizar_parada(db, parada_id, usuario)
    if usuario["rol"] != "REPARTIDOR":
        raise HTTPException(403, "El enlace se genera desde el reparto asignado.")
    parada, pedido, _ = await contexto(db, parada_id)
    vence = ahora() + timedelta(hours=48)
    token = jwt.encode(
        {"tipo": "portal_cliente", "parada": parada.id_parada, "pedido": pedido.id_pedido,
         "emisor": int(usuario["sub"]), "exp": vence, "jti": secrets.token_urlsafe(16)},
        settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM,
    )
    response.headers["Cache-Control"] = "no-store"
    return {"enlace": token, "vence": vence}


@router.post("/consulta")
async def consultar(req: Acceso, response: Response, db=Depends(get_db)):
    datos = credencial(req, response)
    parada, pedido, plan = await contexto(db, datos["parada"])
    if pedido.id_pedido != datos["pedido"]:
        raise HTTPException(404, "El pedido no está disponible.")
    confirmado = await total_pedido(db, pedido.id_pedido, ("CONFORME",))
    reservado = await total_pedido(db, pedido.id_pedido, ("PENDIENTE", "CONFORME"))
    saldo = max(Decimal(0), pedido.importe_total - confirmado)
    return {
        "pedido": pedido.codigo_externo, "estado": parada.estado,
        "fecha": plan.fecha_operacion, "llegada_estimada": parada.eta_estimada,
        "estimacion_actualizada": plan.fecha_confirmacion, "saldo": saldo,
        "pago_demo": demo_habilitada(pedido),
        "puede_pagar": demo_habilitada(pedido) and saldo > 0
        and reservado == confirmado and not pedido.cierre_financiero,
    }


@router.post("/pago-demo")
async def pagar_demo(req: Acceso, response: Response, db=Depends(get_db)):
    datos = credencial(req, response)
    pedido = await db.scalar(
        select(Pedido).where(Pedido.id_pedido == datos["pedido"]).with_for_update()
    )
    parada, _, _ = await contexto(db, datos["parada"])
    if not pedido or parada.id_pedido != pedido.id_pedido or not demo_habilitada(pedido):
        raise HTTPException(404, "Demostración de pago no habilitada.")
    referencia = f"SIPRD-DEMO-{pedido.id_pedido}"
    anterior = await db.scalar(
        select(Cobro.id_cobro).join(ParadaRuta)
        .where(ParadaRuta.id_pedido == pedido.id_pedido, Cobro.numero_operacion == referencia)
    )
    if anterior:
        return {"referencia": referencia, "simulado": True}
    confirmado = await total_pedido(db, pedido.id_pedido, ("CONFORME",))
    reservado = await total_pedido(db, pedido.id_pedido, ("PENDIENTE", "CONFORME"))
    saldo = pedido.importe_total - confirmado
    if pedido.cierre_financiero or saldo <= 0 or reservado != confirmado:
        raise HTTPException(409, "No hay saldo disponible para simular el pago.")
    cobro = Cobro(id_parada=parada.id_parada, monto=saldo, medio_pago="TRANSFERENCIA",
                  numero_operacion=referencia, estado="CONFORME",
                  observacion="Pago simulado. Sin movimiento de dinero.",
                  fecha_validacion=ahora(), id_usuario_registro=datos["emisor"])
    db.add(cobro)
    await db.flush()
    await registrar_auditoria(db, "VALIDAR", "cobro", cobro.id_cobro,
                             valores_despues={"simulado": True, "referencia": referencia})
    return {"referencia": referencia, "simulado": True}
