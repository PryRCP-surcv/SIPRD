import { useEffect, useState } from 'react'
import { apiFetch } from '../api/cliente'

const moneda = importe => 'S/ ' + Number(importe).toFixed(2)

export default function LiquidacionEfectivo({ fecha, cobros, onGuardar, puedeGuardar }) {
  const [filas, setFilas] = useState([])
  const [seleccion, setSeleccion] = useState(null)
  const [recibido, setRecibido] = useState('')
  const [observacion, setObservacion] = useState('')
  const [error, setError] = useState('')
  const [ocupado, setOcupado] = useState(false)
  const [revision, setRevision] = useState(0)

  useEffect(() => {
    let activa = true
    apiFetch('/liquidaciones?fecha=' + fecha).then(datos => {
      if (activa) setFilas(datos)
    }).catch(error => {
      if (activa) setError(error.message)
    })
    return () => { activa = false }
  }, [fecha, revision, cobros])

  const fila = filas.find(f => f.repartidor_id === seleccion)
  const guardada = fila?.liquidacion
  const conforme = guardada && Number(guardada.esperado) === Number(guardada.recibido)
  const diferencia = fila && recibido !== '' ? Math.round(Number(recibido) * 100) -
    Math.round(Number(fila.esperado) * 100) : 0

  async function guardar(evento) {
    evento.preventDefault()
    setOcupado(true)
    setError('')
    try {
      await apiFetch(`/liquidaciones/${fecha}/${seleccion}`, {
        method: 'PUT', body: JSON.stringify({ recibido, observacion }),
      })
      setRevision(n => n + 1)
      await onGuardar()
    } catch (error) {
      setError(error.message)
    } finally {
      setOcupado(false)
    }
  }

  return <section className="card" style={{ padding: 16, marginBottom: 16 }}>
    <h3>Liquidación de efectivo</h3>
    {error && <p role="alert">{error}</p>}
    {!filas.length ? <p>Sin cobros en efectivo para esta jornada.</p> : <>
      <label>Responsable del cobro{' '}
        <select value={seleccion ?? ''} onChange={e => {
          const id = Number(e.target.value)
          const guardada = filas.find(f => f.repartidor_id === id)?.liquidacion
          setSeleccion(id)
          setRecibido(guardada?.recibido ?? '')
          setObservacion(guardada?.observacion ?? '')
          setError('')
        }}>
          <option value="">Selecciona un repartidor</option>
          {filas.map(f => <option key={f.repartidor_id} value={f.repartidor_id}>{f.repartidor}</option>)}
        </select>
      </label>
      {fila && <>
        <p>Efectivo esperado {moneda(fila.esperado)}</p>
        {guardada && <p>Liquidación {conforme ? 'conforme' : 'observada'} · Recibido {moneda(guardada.recibido)}
          {' · '}Diferencia {moneda(Number(guardada.recibido) - Number(guardada.esperado))}
          {guardada.observacion && ' · ' + guardada.observacion}</p>}
        {!conforme && puedeGuardar && <form onSubmit={guardar} style={{ display: 'flex', flexWrap: 'wrap', gap: 12 }}>
          <label>Efectivo contado <input type="number" min="0" step="0.01" required
            value={recibido} onChange={e => setRecibido(e.target.value)} /></label>
          <label>Observación <input maxLength={1000} required={diferencia !== 0}
            value={observacion} onChange={e => setObservacion(e.target.value)} /></label>
          {recibido !== '' && <span>Diferencia {moneda(diferencia / 100)}</span>}
          <button className="btn btn-primary" disabled={ocupado}>Guardar liquidación</button>
        </form>}
      </>}
    </>}
  </section>
}
