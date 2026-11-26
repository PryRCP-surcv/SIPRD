from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from fastapi import APIRouter, Depends, UploadFile, File
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import String, cast, func, or_, select
from openpyxl import Workbook
from backend.app.core.db import get_db
from backend.app.core.seguridad import require_roles
from backend.app.core.errores import (
    ErrorNoEncontrado,
    ErrorReglaNegocio,
)
from backend.app.core.auditoria import registrar_auditoria
from backend.app.core.almacenamiento import guardar_archivo
from backend.app.core.config import settings
from backend.app.core.tiempo import ahora
from backend.app.core.reparto import autorizar_parada
from backend.app.models import Cobro, ParadaRuta, Pedido, Cliente, Ruta, Planificacion, Usuario, Vehiculo
from backend.app.modules.cobranzas.saldos import validar_importe
from backend.app.modules.cobranzas.liquidaciones import validar_caja_abierta

router = APIRouter(prefix="/cobros", tags=["Cobranzas"])
lectura = require_roles(
    ["TESORERIA", "ASISTENTE", "JEFE", "ADMINISTRADOR", "REPARTIDOR"]
)
escritura = require_roles(["REPARTIDOR", "ASISTENTE", "JEFE"])


class DatosCobro(BaseModel):
    monto: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    medio_pago: str
    numero_operacion: str | None = Field(None, max_length=100)


class Registrar(DatosCobro):
    parada_id: int


class Contraste(BaseModel):
    resultado: str
    observacion: str | None = Field(None, max_length=1000)


def datos(req):
    medio = req.medio_pago.upper()
    if medio not in {"EFECTIVO", "YAPE", "PLIN", "TRANSFERENCIA", "DEPOSITO", "CHEQUE"}:
        raise ErrorReglaNegocio("MEDIO_INVALIDO", "Medio de pago no permitido.")
    if medio != "EFECTIVO" and not (req.numero_operacion or "").strip():
        raise ErrorReglaNegocio(
            "OPERACION_REQUERIDA", "Indique el número de operación."
        )
    if (req.numero_operacion or "").upper().strip().startswith("SIPRD-DEMO-"):
        raise ErrorReglaNegocio("OPERACION_INVALIDA", "La referencia está reservada.")
    return medio


async def editable(db, c, usuario):
    s = await autorizar_parada(db, c.id_parada, usuario)
    p = (
        (
            await db.execute(
                select(Pedido).where(Pedido.id_pedido == s.id_pedido).with_for_update()
            )
        )
        .scalars()
        .one()
    )
    await db.refresh(c)
    if p.cierre_financiero or c.estado == "CONFORME":
        raise ErrorReglaNegocio(
            "COBRO_NO_EDITABLE", "El cobro ya fue confirmado o el pedido está cerrado."
        )
    return p


@router.post("", status_code=201)
async def registrar(req: Registrar, db=Depends(get_db), usuario=Depends(escritura)):
    s = await autorizar_parada(db, req.parada_id, usuario)
    p = (
        (
            await db.execute(
                select(Pedido).where(Pedido.id_pedido == s.id_pedido).with_for_update()
            )
        )
        .scalars()
        .one()
    )
    if p.cierre_financiero:
        raise ErrorReglaNegocio(
            "PEDIDO_CERRADO", "El pedido ya tiene cierre financiero."
        )
    medio = datos(req)
    if medio == "EFECTIVO":
        await validar_caja_abierta(db, s, int(usuario["sub"]))
    await validar_importe(db, p, req.monto)
    c = Cobro(
        id_parada=s.id_parada,
        monto=req.monto,
        medio_pago=medio,
        numero_operacion=req.numero_operacion,
        estado="PENDIENTE",
        id_usuario_registro=int(usuario["sub"]),
    )
    db.add(c)
    await db.flush()
    await registrar_auditoria(
        db,
        "CREAR",
        "cobro",
        c.id_cobro,
        valores_despues={"monto": str(req.monto)},
        usuario_id=usuario["sub"],
    )
    await db.commit()
    return {
        "cobro_id": c.id_cobro,
        "mensaje": "Cobro registrado, pendiente de validación",
    }


@router.put("/{cobro_id}")
async def corregir(
    cobro_id: int, req: DatosCobro, db=Depends(get_db), usuario=Depends(escritura)
):
    c = await db.get(Cobro, cobro_id)
    if not c:
        raise ErrorNoEncontrado("Cobro", cobro_id)
    p = await editable(db, c, usuario)
    medio = datos(req)
    if "EFECTIVO" in (c.medio_pago, medio):
        await validar_caja_abierta(db, await db.get(ParadaRuta, c.id_parada), c.id_usuario_registro)
    await validar_importe(db, p, req.monto, c.id_cobro)
    previo = {"monto": str(c.monto), "estado": c.estado, "observacion": c.observacion}
    c.monto = req.monto
    c.medio_pago = medio
    c.numero_operacion = req.numero_operacion
    c.estado = "PENDIENTE"
    c.id_usuario_validador = None
    c.fecha_validacion = None
    await registrar_auditoria(
        db,
        "ACTUALIZAR",
        "cobro",
        cobro_id,
        previo,
        {"monto": str(c.monto), "estado": c.estado},
        usuario["sub"],
    )
    await db.commit()
    return {"mensaje": "Corrección enviada a Tesorería"}


@router.post("/{cobro_id}/comprobante")
async def comprobante(
    cobro_id: int,
    archivo: UploadFile = File(...),
    db=Depends(get_db),
    usuario=Depends(escritura),
):
    c = await db.get(Cobro, cobro_id)
    if not c:
        raise ErrorNoEncontrado("Cobro", cobro_id)
    await editable(db, c, usuario)
    if c.medio_pago == "EFECTIVO":
        await validar_caja_abierta(db, await db.get(ParadaRuta, c.id_parada), c.id_usuario_registro)
    info = await guardar_archivo(archivo, "comprobantes")
    c.url_comprobante = info["clave_archivo"]
    c.nombre_archivo = info["nombre_original"]
    c.tipo_mime = info["tipo_mime"]
    c.estado = "PENDIENTE"
    c.id_usuario_validador = None
    c.fecha_validacion = None
    await registrar_auditoria(
        db,
        "ACTUALIZAR",
        "cobro",
        cobro_id,
        valores_despues={"comprobante": True},
        usuario_id=usuario["sub"],
    )
    await db.commit()
    return {"mensaje": "Comprobante enviado"}


@router.get("")
async def listar(
    estado: str | None = None,
    fecha: date | None = None,
    db=Depends(get_db),
    usuario=Depends(lectura),
    medio: str | None = None,
    busca: str | None = None,
):
    q = (
        select(Cobro, ParadaRuta, Pedido, Cliente, Usuario, Vehiculo)
        .join(ParadaRuta, Cobro.id_parada == ParadaRuta.id_parada)
        .join(Pedido, ParadaRuta.id_pedido == Pedido.id_pedido)
        .join(Cliente, Pedido.id_cliente == Cliente.id_cliente)
        .join(Ruta, Ruta.id_ruta == ParadaRuta.id_ruta)
        .join(Planificacion, Planificacion.id_planificacion == Ruta.id_planificacion)
        .join(Usuario, Usuario.id_usuario == Cobro.id_usuario_registro)
        .join(Vehiculo, Vehiculo.id_vehiculo == Ruta.id_vehiculo)
    )
    if estado:
        q = q.where(Cobro.estado == estado.upper())
    if fecha:
        q = q.where(Planificacion.fecha_operacion == fecha)
    if medio:
        q = q.where(Cobro.medio_pago == medio.upper())
    if busca and busca.strip():
        termino = busca.strip().lower()
        q = q.where(or_(
            func.lower(Cliente.razon_social).contains(termino, autoescape=True),
            func.lower(Pedido.codigo_externo).contains(termino, autoescape=True),
            cast(Pedido.id_pedido, String).contains(termino, autoescape=True),
            func.lower(func.concat(Usuario.nombres, " ", Usuario.apellidos)).contains(termino, autoescape=True),
            func.lower(Vehiculo.placa).contains(termino, autoescape=True),
        ))
    if usuario["rol"] == "REPARTIDOR":
        q = q.where(
            Cobro.id_usuario_registro == int(usuario["sub"]),
            Planificacion.fecha_operacion == usuario["fecha_reparto"],
        )
    filas = (await db.execute(q.order_by(Cobro.id_cobro.desc()))).all()
    return [
        {
            "id": c.id_cobro,
            "parada_id": s.id_parada,
            "pedido_id": p.id_pedido,
            "pedido": p.codigo_externo,
            "cliente": cl.razon_social,
            "conductor": f"{u.nombres} {u.apellidos}",
            "ruta": v.placa,
            "monto": float(c.monto),
            "importe_pedido": float(p.importe_total),
            "medio_pago": c.medio_pago,
            "numero_operacion": c.numero_operacion,
            "simulado": (c.numero_operacion or "").startswith("SIPRD-DEMO-"),
            "estado": c.estado,
            "observacion": c.observacion,
            "tiene_comprobante": bool(c.url_comprobante),
            "fecha": c.fecha_registro,
            "entregado": p.estado == "ENTREGADO",
            "cierre_financiero": p.cierre_financiero,
        }
        for c, s, p, cl, u, v in filas
    ]


@router.get("/exportacion.xlsx")
async def exportar(
    fecha: date | None = None,
    db=Depends(get_db),
    usuario=Depends(require_roles(["TESORERIA", "JEFE"])),
    estado: str | None = None,
    medio: str | None = None,
    busca: str | None = None,
):
    filas = await listar(fecha=fecha, db=db, usuario=usuario, estado=estado, medio=medio, busca=busca)
    w = Workbook()
    sh = w.active
    sh.title = "Arqueo"
    sh.append(
        [
            "Cobro",
            "Pedido",
            "Cliente",
            "Fecha",
            "Medio de pago",
            "Operación",
            "Monto registrado",
            "Estado",
            "Observación",
            "Responsable",
            "Vehículo",
        ]
    )
    for c in filas:
        # Las cadenas externas se fuerzan a texto para evitar fórmulas al abrir Excel.
        vals = [
            c["id"],
            c["pedido"],
            c["cliente"],
            str(c["fecha"]),
            c["medio_pago"],
            c["numero_operacion"] or "",
            c["monto"],
            c["estado"],
            c["observacion"] or "",
            c["conductor"],
            c["ruta"],
        ]
        sh.append(vals)
        for cell in sh[sh.max_row]:
            if isinstance(cell.value, str):
                cell.data_type = "s"
    sh.freeze_panes = "A2"
    sh.auto_filter.ref = sh.dimensions
    for col in "ABCDEFGHIJK":
        sh.column_dimensions[col].width = 23
    resumen = w.create_sheet("Totales")
    resumen.append(["Medio", "Monto registrado (PEN)"])
    totales = {}
    for c in filas:
        medio = c["medio_pago"]
        totales[medio] = totales.get(medio, Decimal(0)) + Decimal(str(c["monto"]))
    for medio, total in sorted(totales.items()):
        resumen.append([medio, total])
    resumen.append(["Total general", sum(totales.values(), Decimal(0))])
    buf = BytesIO()
    w.save(buf)
    buf.seek(0)
    await registrar_auditoria(
        db,
        "VALIDAR",
        "exportacion_cobros",
        0,
        valores_despues={"filas": len(filas)},
        usuario_id=usuario["sub"],
    )
    await db.commit()
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": "attachment; filename=arqueo_siprd.xlsx",
            "Cache-Control": "no-store",
        },
    )


@router.post("/{cobro_id}/contraste")
async def contrastar(
    cobro_id: int,
    req: Contraste,
    db=Depends(get_db),
    usuario=Depends(require_roles(["TESORERIA"])),
):
    c = await db.get(Cobro, cobro_id)
    if not c:
        raise ErrorNoEncontrado("Cobro", cobro_id)
    s = await db.get(ParadaRuta, c.id_parada)
    p = (
        (
            await db.execute(
                select(Pedido).where(Pedido.id_pedido == s.id_pedido).with_for_update()
            )
        )
        .scalars()
        .one()
    )
    await db.refresh(c)
    if (c.numero_operacion or "").startswith("SIPRD-DEMO-"):
        raise ErrorReglaNegocio("COBRO_SIMULADO", "El resultado del pago simulado no se modifica manualmente.")
    if c.medio_pago == "EFECTIVO":
        await validar_caja_abierta(db, s, c.id_usuario_registro)
    if p.cierre_financiero:
        raise ErrorReglaNegocio("PEDIDO_CERRADO", "El pedido ya está cerrado.")
    resultado = req.resultado.upper()
    if resultado not in ("CONFORME", "OBSERVADO"):
        raise ErrorReglaNegocio(
            "RESULTADO_INVALIDO", "Seleccione CONFORME u OBSERVADO."
        )
    if resultado == "OBSERVADO" and not (req.observacion or "").strip():
        raise ErrorReglaNegocio(
            "OBSERVACION_REQUERIDA", "Escriba la observación para el repartidor."
        )
    if resultado == "CONFORME" and c.medio_pago != "EFECTIVO" and not c.url_comprobante:
        raise ErrorReglaNegocio(
            "COMPROBANTE_REQUERIDO", "Falta el comprobante del pago."
        )
    if resultado == "CONFORME":
        await validar_importe(db, p, c.monto, c.id_cobro)
    antes = {"estado": c.estado, "observacion": c.observacion}
    c.estado = resultado
    c.observacion = req.observacion if resultado == "OBSERVADO" else None
    c.id_usuario_validador = int(usuario["sub"])
    c.fecha_validacion = ahora()
    await registrar_auditoria(
        db,
        "VALIDAR" if resultado == "CONFORME" else "OBSERVAR",
        "cobro",
        cobro_id,
        antes,
        {"estado": resultado, "observacion": c.observacion},
        usuario["sub"],
    )
    await db.commit()
    return {"mensaje": "Validación registrada", "estado": resultado}


@router.get("/{cobro_id}/comprobante/archivo")
async def descargar(cobro_id: int, db=Depends(get_db), usuario=Depends(lectura)):
    c = await db.get(Cobro, cobro_id)
    if not c or not c.url_comprobante:
        raise ErrorNoEncontrado("Comprobante", cobro_id)
    if usuario["rol"] == "REPARTIDOR":
        await autorizar_parada(db, c.id_parada, usuario)
    ruta = (Path(settings.UPLOAD_DIR) / c.url_comprobante).resolve()
    if (
        not ruta.is_relative_to(Path(settings.UPLOAD_DIR).resolve())
        or not ruta.is_file()
    ):
        raise ErrorNoEncontrado("Archivo", cobro_id)
    await registrar_auditoria(
        db,
        "VALIDAR",
        "cobro",
        cobro_id,
        valores_despues={"acceso_comprobante": True},
        usuario_id=usuario["sub"],
    )
    await db.commit()
    return FileResponse(
        ruta, media_type=c.tipo_mime, headers={"Cache-Control": "no-store"}
    )
