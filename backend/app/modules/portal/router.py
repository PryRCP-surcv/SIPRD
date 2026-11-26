from datetime import date, timedelta
from decimal import Decimal
import secrets
from uuid import UUID, uuid4

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, text

from backend.app.core.auditoria import registrar_auditoria
from backend.app.core.config import settings
from backend.app.core.db import get_db
from backend.app.core.reparto import autorizar_parada
from backend.app.core.seguridad import decodificar_token, require_roles
from backend.app.core.tiempo import ahora
from backend.app.models import Cobro, ParadaRuta, Pedido, Planificacion, Ruta
from backend.app.modules.cobranzas.saldos import total_pedido, pago_digital_vigente
from backend.app.modules.portal import mercadopago as mp

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
    digital = await pago_digital_vigente(db, pedido.id_pedido)
    return {
        "pedido": pedido.codigo_externo, "estado": parada.estado,
        "fecha": plan.fecha_operacion, "llegada_estimada": parada.eta_estimada,
        "estimacion_actualizada": plan.fecha_confirmacion, "saldo": saldo,
        "pago_demo": demo_habilitada(pedido),
        "pago_digital": mp.habilitado(pedido),
        "pago_digital_pruebas": mp.habilitado(pedido) and settings.MP_MODO == "pruebas",
        "estado_pago_digital": digital["estado"] if digital else None,
        "detalle_pago_digital": digital["detalle"] if digital else None,
        "puede_checkout": mp.habilitado(pedido) and saldo > 0
        and reservado == confirmado and not pedido.cierre_financiero
        and (not digital or digital["estado"] != "OBSERVADO"),
        "puede_pagar": demo_habilitada(pedido) and saldo > 0
        and reservado == confirmado and not pedido.cierre_financiero and not digital
        and not mp.habilitado(pedido),
    }


@router.post("/checkout")
async def iniciar_checkout(req: Acceso, response: Response, db=Depends(get_db)):
    datos = credencial(req, response)
    pedido = await db.scalar(select(Pedido).where(
        Pedido.id_pedido == datos["pedido"]
    ).with_for_update())
    parada, _, _ = await contexto(db, datos["parada"])
    if not pedido or parada.id_pedido != pedido.id_pedido or not mp.habilitado(pedido):
        raise HTTPException(404, "Pago digital no habilitado para este pedido.")
    pago = await pago_digital_vigente(db, pedido.id_pedido)
    if pago and (pago["modo"] != settings.MP_MODO or pago["estado"] == "OBSERVADO"):
        raise HTTPException(409, "Tesorería debe revisar la orden existente.")
    if not pago:
        confirmado = await total_pedido(db, pedido.id_pedido, ("CONFORME",))
        reservado = await total_pedido(db, pedido.id_pedido, ("PENDIENTE", "CONFORME"))
        saldo = pedido.importe_total - confirmado
        if pedido.cierre_financiero or saldo <= 0 or reservado != confirmado:
            raise HTTPException(409, "No hay saldo disponible para iniciar un pago digital.")
        ref = uuid4()
        await db.execute(text("""
            INSERT INTO siprd.pagos_digitales
              (referencia, pedido_id, parada_id, emisor_id, modo, importe, estado)
            VALUES (:ref, :pedido, :parada, :emisor, :modo, :importe, 'CREANDO')
        """), dict(ref=ref, pedido=pedido.id_pedido, parada=parada.id_parada, emisor=datos["emisor"],
                   modo=settings.MP_MODO, importe=saldo))
        pago = await mp.pago_por_referencia(db, ref)
    if pago["enlace_checkout"]:
        return {"url": pago["enlace_checkout"]}
    # Conservar la misma referencia aunque se interrumpa la llamada al proveedor.
    await db.commit()
    orden, url = await mp.crear_orden(pago)
    await db.execute(text("""
        UPDATE siprd.pagos_digitales SET orden_proveedor = :orden, enlace_checkout = :url,
            estado = 'PENDIENTE', actualizado = now()
        WHERE referencia = :ref AND estado = 'CREANDO'
    """), dict(orden=orden, url=url, ref=pago["referencia"]))
    return {"url": url}


@router.post("/estado-checkout")
async def estado_checkout(req: Acceso, response: Response, db=Depends(get_db)):
    datos = credencial(req, response)
    parada, pedido, _ = await contexto(db, datos["parada"])
    if pedido.id_pedido != datos["pedido"] or not mp.habilitado(pedido):
        raise HTTPException(404, "Pago digital no habilitado.")
    pago = await pago_digital_vigente(db, pedido.id_pedido)
    if not pago or not pago["orden_proveedor"]:
        return {"estado": pago["estado"] if pago else None}
    return await mp.sincronizar(db, pago)


@router.post("/notificaciones/mercadopago")
async def notificar(request: Request, db=Depends(get_db)):
    if not mp.configurado():
        raise HTTPException(404, "Pago digital no habilitado.")
    orden = request.query_params.get("data.id")
    if request.query_params.get("type") != "order" or not mp.firma_valida(
        orden, request.headers.get("x-signature"), request.headers.get("x-request-id")
    ):
        raise HTTPException(401, "Notificación no autenticada.")
    pago = (await db.execute(text("""
        SELECT * FROM siprd.pagos_digitales WHERE orden_proveedor = :orden AND modo = :modo
    """), {"orden": orden.upper(), "modo": settings.MP_MODO})).mappings().one_or_none()
    if pago:
        await mp.sincronizar(db, pago, aviso=True)
    return {"recibido": True}


@router.get("/tesoreria")
async def operaciones_digitales(fecha: date, db=Depends(get_db),
                                usuario=Depends(require_roles(["TESORERIA"]))):
    filas = (await db.execute(text("""
        SELECT d.referencia, d.orden_proveedor, d.estado, d.estado_proveedor,
               d.importe, d.modo, d.detalle, d.actualizado, p.codigo_externo AS pedido
        FROM siprd.pagos_digitales d
        JOIN siprd.pedidos p ON p.id_pedido = d.pedido_id
        JOIN siprd.paradas_ruta s ON s.id_parada = d.parada_id
        JOIN siprd.rutas r ON r.id_ruta = s.id_ruta
        JOIN siprd.planificaciones pl ON pl.id_planificacion = r.id_planificacion
        WHERE pl.fecha_operacion = :fecha ORDER BY d.creado DESC
    """), {"fecha": fecha})).mappings().all()
    return [dict(fila) for fila in filas]


@router.post("/tesoreria/{referencia}/sincronizar")
async def sincronizar_pago(referencia: UUID, db=Depends(get_db),
                          usuario=Depends(require_roles(["TESORERIA"]))):
    if not mp.configurado():
        raise HTTPException(409, "La conexión con el proveedor aún no está configurada.")
    pago = await mp.pago_por_referencia(db, referencia)
    if not pago:
        raise HTTPException(404, "Orden de pago no encontrada.")
    return await mp.sincronizar(db, pago)


@router.post("/pago-demo")
async def pagar_demo(req: Acceso, response: Response, db=Depends(get_db)):
    datos = credencial(req, response)
    pedido = await db.scalar(
        select(Pedido).where(Pedido.id_pedido == datos["pedido"]).with_for_update()
    )
    parada, _, _ = await contexto(db, datos["parada"])
    if not pedido or parada.id_pedido != pedido.id_pedido or not demo_habilitada(pedido):
        raise HTTPException(404, "Demostración de pago no habilitada.")
    if mp.habilitado(pedido):
        raise HTTPException(409, "Utilice el checkout de pruebas configurado.")
    referencia = f"SIPRD-DEMO-{pedido.id_pedido}"
    anterior = await db.scalar(
        select(Cobro.id_cobro).join(ParadaRuta)
        .where(ParadaRuta.id_pedido == pedido.id_pedido, Cobro.numero_operacion == referencia)
    )
    if anterior:
        return {"referencia": referencia, "simulado": True}
    if await pago_digital_vigente(db, pedido.id_pedido):
        raise HTTPException(409, "Hay una operación digital pendiente de revisión.")
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
