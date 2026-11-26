import { hhmm } from '../lib/planner'

export default function FleetTrial({
  escenarios,
  seleccion,
  onSeleccionar,
  recomendado,
  error,
  onRecalcular,
  calculando,
  jornadaMax,
}) {
  const limite = (jornadaMax || 8) * 60
  const peor = escenarios.length ? Math.max(1, ...escenarios.map((e) => e.jornadaMax || 0)) : 1
  const actual = escenarios.find((e) => e.n === seleccion) || escenarios[0]

  return (
    <section className="fleet">
      <div className="fleet-h">
        <div>
          <h3>Tanteo de flota</h3>
          <p>
            Compara la flota para la fecha seleccionada. Cada escenario muestra la jornada más larga
            y cuántos pedidos quedan sin asignar.
          </p>
        </div>
        <div className="btns">
          <button className="btn blue" onClick={onRecalcular} disabled={calculando}>
            {calculando ? 'Optimizando…' : '▶ Recalcular'}
          </button>
        </div>
      </div>
      {error && <p role="status" style={{padding: '8px 16px'}}>{error}</p>}
      {escenarios.length > 0 && recomendado === null &&
        <p role="status" style={{padding: '8px 16px'}}>Ninguna combinación probada cubre todos los pedidos dentro de la jornada.</p>}

      <div className="scn">
        {escenarios.map((e) => {
          const excede = !e.viable || e.jornadaMax > limite
          const clases = ['s', e.n === seleccion ? 'on' : '', excede ? 'bad' : ''].filter(Boolean).join(' ')
          const color = excede ? '#DC2626' : e.n === recomendado ? '#3B82F6' : '#16A34A'
          return (
            <button
              key={e.n}
              className={clases}
              onClick={() => onSeleccionar(e.n)}
              aria-pressed={e.n === seleccion}
            >
              {excede && <span className="tag r">Saturado</span>}
              {!excede && e.n === recomendado && <span className="tag b">Recomendado</span>}

              <div className="s-n">
                <b>{e.n}</b>
                <span>{e.n === 1 ? 'vehículo' : 'vehículos'}</span>
              </div>
              <div className="m"><span>Jornada máx.</span><b>{hhmm(e.jornadaMax)}</b></div>
              <div className="m"><span>Sin asignar</span><b>{e.sinAsignar.length} pedidos</b></div>
              <div className="m"><span>Distancia</span><b>{e.kmTotal.toFixed(1)} km</b></div>
              <div className="bar">
                <i style={{ width: `${Math.min(100, (e.jornadaMax / peor) * 100)}%`, background: color }} />
              </div>
            </button>
          )
        })}

      </div>

      {actual && (
        <div className="fleet-f">
          <p>
            Escenario aplicado: <b>{actual.n} vehículos</b> · {actual.rutas.reduce((s, r) => s + r.pedidos.length, 0)} pedidos
            repartidos en <b>{actual.rutas.length} rutas</b> · balance de carga <b>{actual.balance}%</b> · jornada más corta{' '}
            <b>{hhmm(actual.jornadaMin)}</b>
          </p>
        </div>
      )}
    </section>
  )
}
