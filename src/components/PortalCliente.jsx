import { useEffect, useState } from 'react'
import QRCode from 'qrcode'
import { apiFetch } from '../api/cliente'

const soles = valor => new Intl.NumberFormat('es-PE', {
  style: 'currency', currency: 'PEN',
}).format(Number(valor))
const hora = valor => new Intl.DateTimeFormat('es-PE', {
  hour: '2-digit', minute: '2-digit', timeZone: 'America/Lima',
}).format(new Date(valor))

export function EnlaceCliente({ paradaId }) {
  const [datos, setDatos] = useState(null)
  const [estado, setEstado] = useState('')
  const [ocupado, setOcupado] = useState(false)

  async function generar() {
    setOcupado(true)
    setEstado('')
    try {
      const { enlace, vence } = await apiFetch('/portal/enlaces/' + paradaId, { method: 'POST' })
      const url = location.origin + location.pathname + '#pedido=' + encodeURIComponent(enlace)
      setDatos({ url, vence, qr: await QRCode.toDataURL(url, { width: 256, margin: 2 }) })
    } catch (error) {
      setEstado(error.message)
    } finally {
      setOcupado(false)
    }
  }

  return <div className="reparto-detail-card">
    <h4>Seguimiento para el cliente</h4>
    {!datos && <button className="reparto-secondary" disabled={ocupado} onClick={generar}>Generar enlace y QR</button>}
    {datos && <>
      <img src={datos.qr} width="192" height="192" alt="QR para consultar el pedido" />
      <div><a href={datos.url} target="_blank" rel="noreferrer">Abrir enlace del cliente</a></div>
      <label>Enlace del pedido <input style={{ width: '100%' }} readOnly value={datos.url}
        onFocus={e => e.target.select()} /></label>
      <button className="reparto-secondary" onClick={async () => {
        try {
          await navigator.clipboard.writeText(datos.url)
          setEstado('Enlace copiado.')
        } catch {
          setEstado('Selecciona y copia el enlace del campo.')
        }
      }}>Copiar enlace</button>
      <small>Vence el {new Date(datos.vence).toLocaleString('es-PE', { timeZone: 'America/Lima' })}</small>
    </>}
    {estado && <p role="status">{estado}</p>}
  </div>
}

async function portal(accion, enlace) {
  const respuesta = await fetch('/api/v1/portal/' + accion, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enlace }), cache: 'no-store',
  })
  const data = await respuesta.json()
  if (!respuesta.ok) throw new Error(typeof data.detail === 'string' ? data.detail :
    data.mensaje || data.message || 'El enlace es inválido, venció o dejó de estar disponible.')
  return data
}

export default function PortalCliente({ enlace }) {
  const [pedido, setPedido] = useState(null)
  const [error, setError] = useState('')
  const [pago, setPago] = useState('')
  const [ocupado, setOcupado] = useState(false)

  useEffect(() => {
    let vigente = true
    async function consultar() {
      try {
        const datos = await portal('consulta', enlace)
        if (vigente) { setPedido(datos); setError('') }
      } catch (error) {
        if (vigente) { setPedido(null); setError(error.message) }
      }
    }
    consultar()
    const timer = setInterval(consultar, 15000)
    return () => { vigente = false; clearInterval(timer) }
  }, [enlace])

  async function simular() {
    setOcupado(true)
    setError('')
    try {
      const resultado = await portal('pago-demo', enlace)
      setPago('Pago de prueba registrado. Referencia ' + resultado.referencia)
      setPedido(await portal('consulta', enlace))
    } catch (error) {
      setError(error.message)
    } finally {
      setOcupado(false)
    }
  }

  return <main className="reparto-shell" style={{ maxWidth: 560, margin: '24px auto', padding: 16 }}>
    <section className="card" style={{ padding: 20 }}>
      <h2>Seguimiento de pedido</h2>
      {error && <p role="alert">{error}</p>}
      {!pedido && !error && <p>Consultando pedido…</p>}
      {pedido && <>
        <dl>
          <dt>Pedido</dt><dd>{pedido.pedido}</dd>
          <dt>Entrega</dt><dd>{pedido.fecha.split('-').reverse().join('/')} · {{
            ATENDIDA: 'Entregada', EN_CAMINO: 'En camino', NO_ATENDIDA: 'Entrega pendiente de atención',
          }[pedido.estado] || 'Programada'}</dd>
          {pedido.llegada_estimada && pedido.estado !== 'ATENDIDA' && <>
            <dt>Franja estimada de llegada</dt>
            <dd>{hora(pedido.llegada_estimada)}–{hora(new Date(pedido.llegada_estimada).getTime() + 30 * 60000)}</dd>
            {pedido.estimacion_actualizada && <dd>Actualizada el {new Date(pedido.estimacion_actualizada)
              .toLocaleString('es-PE', { timeZone: 'America/Lima' })}</dd>}
          </>}
          <dt>Saldo pendiente de confirmación</dt><dd>{soles(pedido.saldo)}</dd>
        </dl>
        {pedido.pago_demo && <p>Modo demostración. No procesa dinero.</p>}
        {pedido.puede_pagar && <button className="reparto-primary" disabled={ocupado} onClick={simular}>Simular pago</button>}
        {pago && <p role="status">{pago}</p>}
      </>}
    </section>
  </main>
}
