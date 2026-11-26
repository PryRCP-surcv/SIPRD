import { useEffect, useState } from 'react'
import { apiFetch } from '../api/cliente'

export default function PagosDigitales({ fecha, puedeValidar, onCambio }) {
  const [filas, setFilas] = useState([])
  const [error, setError] = useState('')
  const [ocupado, setOcupado] = useState(null)
  const url = '/portal/tesoreria?fecha=' + encodeURIComponent(fecha)

  useEffect(() => {
    let vigente = true
    async function cargar() {
      try {
        const datos = await apiFetch(url)
        if (vigente) { setFilas(datos); setError('') }
      } catch (error) {
        if (vigente) setError(error.message)
      }
    }
    cargar()
    const timer = setInterval(cargar, 30000)
    return () => { vigente = false; clearInterval(timer) }
  }, [url])

  async function verificar(referencia) {
    setOcupado(referencia)
    setError('')
    try {
      await apiFetch(`/portal/tesoreria/${referencia}/sincronizar`, { method: 'POST' })
      setFilas(await apiFetch(url))
      await onCambio()
    } catch (error) {
      setError(error.message)
    } finally {
      setOcupado(null)
    }
  }

  return <section className="card" style={{ padding: 16, marginBottom: 16 }}>
    <h3>Operaciones de Mercado Pago</h3>
    {error && <p role="alert">{error}</p>}
    {!filas.length && !error && <p>Sin operaciones integradas en esta jornada.</p>}
    {filas.length > 0 && <div className="tw"><table>
      <thead><tr><th>Pedido</th><th>Importe</th><th>Estado</th><th>Orden y observación</th><th>Acción</th></tr></thead>
      <tbody>{filas.map(p => <tr key={p.referencia}>
        <td>{p.pedido}{p.modo === 'pruebas' && ' · Prueba'}</td>
        <td>S/ {Number(p.importe).toFixed(2)}</td>
        <td>{p.estado}{p.estado_proveedor && ' · ' + p.estado_proveedor}</td>
        <td>{p.orden_proveedor || p.referencia}{p.detalle && <div>{p.detalle}</div>}</td>
        <td>{p.orden_proveedor && puedeValidar && <button className="btn out" disabled={!!ocupado}
          onClick={() => verificar(p.referencia)}>Consultar proveedor</button>}</td>
      </tr>)}</tbody>
    </table></div>}
  </section>
}
