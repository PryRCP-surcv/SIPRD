from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import re
from time import time
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from sqlalchemy import select, text

from backend.app.core.auditoria import registrar_auditoria
from backend.app.core.config import settings
from backend.app.core.tiempo import ahora
from backend.app.models import Cobro, Pedido
from backend.app.modules.cobranzas.saldos import total_pedido


def configurado():
    return settings.MP_MODO != "deshabilitado" and all((
        settings.MP_ACCESS_TOKEN, settings.MP_USUARIO_ID, settings.MP_WEBHOOK_SECRET
    ))


def habilitado(pedido):
    if not configurado():
        return False
    return (settings.MP_MODO == "pruebas") == (pedido.origen == "DEMO_ZONAS")


async def consultar_api(method, path, **kwargs):
    headers = {"Authorization": f"Bearer {settings.MP_ACCESS_TOKEN}", **kwargs.pop("headers", {})}
    try:
        async with httpx.AsyncClient(base_url="https://api.mercadopago.com", timeout=10) as cliente:
            res = await cliente.request(method, path, headers=headers, **kwargs)
            res.raise_for_status()
            data = res.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, "Mercado Pago no confirmó la operación. Vuelva a consultar antes de repetirla.") from exc
    if not isinstance(data, dict):
        raise HTTPException(502, "Respuesta inesperada de Mercado Pago.")
    return data


def id_valido(orden):
    return isinstance(orden, str) and re.fullmatch(r"ORD[A-Z0-9]{1,70}", orden.upper()) is not None


def corresponde(data, pago):
    try:
        importe = Decimal(str(data.get("total_amount")))
    except InvalidOperation:
        return False
    return (
        data.get("external_reference") == str(pago["referencia"])
        and str(data.get("user_id")) == settings.MP_USUARIO_ID
        and data.get("currency") == "PEN"
        and importe == pago["importe"]
    )


async def crear_orden(pago):
    referencia = str(pago["referencia"])
    importe = str(pago["importe"])
    data = await consultar_api(
        "POST", "/v1/orders", headers={"X-Idempotency-Key": referencia},
        json={"type": "online", "processing_mode": "manual", "total_amount": importe,
              "external_reference": referencia, "expiration_time": "P1D",
              "items": [{"title": "Pedido SIPRD", "quantity": 1, "unit_price": importe}]},
    )
    orden, enlace = data.get("id"), data.get("checkout_url", "")
    try:
        url = urlsplit(enlace) if isinstance(enlace, str) else urlsplit("")
    except ValueError as exc:
        raise HTTPException(502, "El proveedor devolvió un enlace inválido.") from exc
    if (not id_valido(orden) or not corresponde(data, pago)
        or orden.startswith("ORDTST") != (pago["modo"] == "pruebas")
        or url.scheme != "https" or url.hostname not in {"www.mercadopago.com.pe", "www.mercadopago.com"}
        or url.username or url.password or not url.path.startswith("/checkout/")):
        raise HTTPException(502, "Mercado Pago devolvió una orden que requiere revisión.")
    return orden.upper(), enlace


def firma_valida(orden, firma, request_id):
    if not request_id or not firma or not id_valido(orden):
        return False
    pares = dict(p.strip().split("=", 1) for p in firma.split(",") if "=" in p)
    ts, digest = pares.get("ts", ""), pares.get("v1", "")
    if not re.fullmatch(r"\d{8,16}", ts) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
        return False
    segundos = int(ts) / (1000 if len(ts) >= 13 else 1)
    if abs(time() - segundos) > 900:
        return False
    mensaje = f"id:{orden.lower()};request-id:{request_id};ts:{ts};"
    esperado = hmac.new(settings.MP_WEBHOOK_SECRET.encode(), mensaje.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, digest.lower())


async def pago_por_referencia(db, referencia):
    return (await db.execute(text("""
        SELECT * FROM siprd.pagos_digitales WHERE referencia = :ref
    """), {"ref": referencia})).mappings().one_or_none()


async def sincronizar(db, pago, aviso=False):
    # Un mismo candado coordina checkout, registro manual y callbacks de un pedido.
    pedido = (await db.scalars(select(Pedido).where(
        Pedido.id_pedido == pago["pedido_id"]
    ).with_for_update())).one()
    pago = await pago_por_referencia(db, pago["referencia"])
    if not pago["orden_proveedor"] or pago["modo"] != settings.MP_MODO:
        raise HTTPException(409, "La orden del proveedor no está disponible en este entorno.")
    if not aviso and pago["consultado"] and (ahora() - pago["consultado"]).total_seconds() < 15:
        return {"estado": pago["estado"], "detalle": pago["detalle"]}
    data = await consultar_api("GET", f"/v1/orders/{pago['orden_proveedor']}")
    estado_mp = str(data.get("status", ""))[:40]
    detalle_mp = str(data.get("status_detail", ""))[:80]
    estado, detalle, cobro_id = pago["estado"], None, pago["cobro_id"]
    await db.execute(text("""
        UPDATE siprd.pagos_digitales SET consultado = now() WHERE referencia = :ref
    """), {"ref": pago["referencia"]})
    if data.get("id") != pago["orden_proveedor"] or not corresponde(data, pago):
        estado, detalle = "OBSERVADO", "La referencia, moneda, importe o cuenta del proveedor no coincide."
    elif estado_mp == "processed" and detalle_mp == "accredited":
        try:
            monto_pagado = Decimal(str(data.get("total_paid_amount")))
        except InvalidOperation:
            monto_pagado = None
        pendiente = pedido.importe_total - await total_pedido(
            db, pedido.id_pedido, ("PENDIENTE", "CONFORME"), excluir=cobro_id
        )
        if monto_pagado != pago["importe"] or pendiente < pago["importe"] or (pedido.cierre_financiero and not cobro_id):
            estado, detalle = "OBSERVADO", "El monto acreditado no coincide con el saldo previsto. Revisar antes de aplicar."
        else:
            if cobro_id:
                cobro = await db.get(Cobro, cobro_id)
                cobro.estado, cobro.observacion = "CONFORME", None
            else:
                prefijo = "SIPRD-MP-PRUEBA-" if pago["modo"] == "pruebas" else "SIPRD-MP-"
                cobro = Cobro(id_parada=pago["parada_id"], monto=pago["importe"],
                              medio_pago="TRANSFERENCIA", numero_operacion=prefijo + pago["orden_proveedor"],
                              estado="CONFORME", fecha_validacion=ahora(), id_usuario_registro=pago["emisor_id"])
                db.add(cobro)
                await db.flush()
                cobro_id = cobro.id_cobro
            estado = "APROBADO"
    elif estado_mp == "refunded" or (estado_mp == "charged_back" and detalle_mp == "reimbursed"):
        estado, detalle = "REVERTIDO", "El proveedor registró un reembolso o contracargo."
        if cobro_id:
            cobro = await db.get(Cobro, cobro_id)
            cobro.estado, cobro.observacion = "OBSERVADO", detalle
            pedido.cierre_financiero = None
    elif estado_mp == "processed" and detalle_mp == "partially_refunded":
        estado, detalle = "OBSERVADO", "Reembolso parcial en el proveedor. Requiere conciliación de Tesorería."
    elif estado_mp == "charged_back":
        estado, detalle = "OBSERVADO", "El proveedor reportó un contracargo. Tesorería debe revisar el resultado."
    elif not cobro_id and estado_mp in {"canceled", "expired"}:
        estado, detalle = "FINALIZADO", "La orden de pago venció o se canceló."
    elif not cobro_id:
        estado = "PENDIENTE"
        if estado_mp in {"failed", "rejected", "action_required"}:
            detalle = "El proveedor reportó un intento fallido o pendiente de acción. Consulte el checkout."
    if (estado, estado_mp, detalle, cobro_id) != (
        pago["estado"], pago["estado_proveedor"], pago["detalle"], pago["cobro_id"]
    ):
        await db.execute(text("""
            UPDATE siprd.pagos_digitales SET estado = :estado, estado_proveedor = :proveedor,
                detalle = :detalle, cobro_id = :cobro, actualizado = now()
            WHERE referencia = :ref
        """), dict(estado=estado, proveedor=estado_mp, detalle=detalle, cobro=cobro_id, ref=pago["referencia"]))
        await registrar_auditoria(db, "VALIDAR", "pago_digital", pedido.id_pedido,
                                 valores_despues={"orden": pago["orden_proveedor"], "estado": estado, "detalle": detalle})
    return {"estado": estado, "detalle": detalle}
