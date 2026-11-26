import unittest
from motor.motor.modelos import Punto, VehiculoVRP, InstanciaVRP
from motor.motor.planificador import optimizar_vrp
from motor.motor.hibrido.construccion import construir_solucion_inicial
from motor.motor.hibrido.busqueda_local import optimizar_2opt


class InsercionRutasTest(unittest.TestCase):
    def setUp(self):
        self.deposito = Punto(id="d", lat=-12, lon=-77, tiempo_servicio_min=0)
        self.vehiculo = VehiculoVRP(id="v", placa="TEST", capacidad_peso_kg=100,
                                   capacidad_volumen_m3=10, inicio_jornada="08:00", fin_jornada="16:00")

    def punto(self, identificador, **datos):
        return Punto(id=identificador, lat=-12.05, lon=-77.01,
                     tiempo_servicio_min=5, **datos)

    def test_inserta_antes_de_una_espera_para_cumplir_la_ventana(self):
        tarde = self.punto("tarde", prioridad="Alta", intervalos_recepcion=[[600, 800]])
        temprano = self.punto("temprano", intervalos_recepcion=[[480, 550]])
        matriz = [[0, 10, 10], [10, 0, 10], [10, 10, 0]]
        rutas, fuera = construir_solucion_inicial(
            self.deposito, [tarde, temprano], [self.vehiculo], matriz, matriz,
            {"d": 0, "tarde": 1, "temprano": 2})
        self.assertFalse(fuera)
        self.assertEqual(["temprano", "tarde"], [p.pedido_id for p in rutas[0].paradas])

    def test_prefiltro_conserva_matriz_vial_del_pedido_aceptado(self):
        exceso = self.punto("exceso", peso_kg=200)
        valido = self.punto("valido", peso_kg=1)
        matriz = [[0, 20, 37], [20, 0, 20], [41, 20, 0]]
        solucion = optimizar_vrp(InstanciaVRP(
            fecha="2026-11-26", deposito=self.deposito, pedidos=[exceso, valido],
            vehiculos=[self.vehiculo], matriz_distancias=matriz, matriz_tiempos=matriz))
        self.assertEqual(78, solucion.total_distancia_km)
        self.assertEqual(["valido"], [p.pedido_id for p in solucion.rutas[0].paradas])
        self.assertEqual("exceso", solucion.no_asignados[0].pedido_id)

    def test_dos_paradas_se_optimizan_con_distancias_asimetricas(self):
        a, b = self.punto("a"), self.punto("b")
        matriz = [[0, 10, 1], [1, 0, 10], [10, 1, 0]]
        ruta = optimizar_2opt(self.deposito, [a, b], self.vehiculo,
                             matriz, matriz, {"d": 0, "a": 1, "b": 2})
        self.assertEqual(["b", "a"], [p.id for p in ruta])


if __name__ == "__main__":
    unittest.main()
